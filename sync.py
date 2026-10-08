#!/usr/bin/env python3

import argparse
import logging
import mimetypes
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlparse

import requests


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

ICLOUD_TOKEN = os.environ["ICLOUD_SHARED_ALBUM_TOKEN"]

IMMICH_URL = os.environ["IMMICH_URL"].rstrip("/")
IMMICH_API_KEY = os.environ["IMMICH_API_KEY"]

IMMICH_ALBUM_NAME = os.getenv("IMMICH_ALBUM_NAME", "My Shared Album")

SYNC_INTERVAL = int(os.getenv("SYNC_INTERVAL", "1800"))

STATE_DB = Path(os.getenv("STATE_DB", "/data/state.db"))
TMP_DIR = Path(os.getenv("TMP_DIR", "/tmp/icloud"))

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

DRY_RUN = os.getenv("DRY_RUN", "true").lower() in (
    "1",
    "true",
    "yes",
    "on",
)

REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "60"))

# Initial Shared Streams endpoint.
# Apple may answer with HTTP 330 and X-Apple-MMe-Host.
INITIAL_ICLOUD_HOST = os.getenv(
    "ICLOUD_SHAREDSTREAMS_HOST",
    "p23-sharedstreams.icloud.com",
)


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)-5s %(message)s",
)

log = logging.getLogger("icloud-immich-sync")


# ---------------------------------------------------------------------------
# HTTP sessions
# ---------------------------------------------------------------------------

icloud_session = requests.Session()
icloud_session.headers.update(
    {
        "User-Agent": (
            "icloud-immich-sync/1.0 "
            "(Shared Album synchronization)"
        ),
        "Cache-Control": "no-cache",
    }
)

immich_session = requests.Session()
immich_session.headers.update(
    {
        "Accept": "application/json",
        "x-api-key": IMMICH_API_KEY,
    }
)


# ---------------------------------------------------------------------------
# SQLite
# ---------------------------------------------------------------------------

def db_connect():
    STATE_DB.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(STATE_DB)
    conn.row_factory = sqlite3.Row

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS assets (
            photo_guid TEXT PRIMARY KEY,
            checksum TEXT NOT NULL,
            filename TEXT,
            media_type TEXT,
            immich_asset_id TEXT,
            synced_at TEXT NOT NULL
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )

    conn.commit()

    return conn


def get_meta(conn, key):
    row = conn.execute(
        "SELECT value FROM meta WHERE key = ?",
        (key,),
    ).fetchone()

    return row["value"] if row else None


def set_meta(conn, key, value):
    conn.execute(
        """
        INSERT INTO meta(key, value)
        VALUES (?, ?)
        ON CONFLICT(key)
        DO UPDATE SET value = excluded.value
        """,
        (key, value),
    )
    conn.commit()


def get_asset(conn, photo_guid):
    return conn.execute(
        """
        SELECT *
        FROM assets
        WHERE photo_guid = ?
        """,
        (photo_guid,),
    ).fetchone()


def save_asset(
    conn,
    photo_guid,
    checksum,
    filename,
    media_type,
    immich_asset_id,
):
    conn.execute(
        """
        INSERT INTO assets(
            photo_guid,
            checksum,
            filename,
            media_type,
            immich_asset_id,
            synced_at
        )
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(photo_guid)
        DO UPDATE SET
            checksum = excluded.checksum,
            filename = excluded.filename,
            media_type = excluded.media_type,
            immich_asset_id = excluded.immich_asset_id,
            synced_at = excluded.synced_at
        """,
        (
            photo_guid,
            checksum,
            filename,
            media_type,
            immich_asset_id,
            datetime.now(timezone.utc).isoformat(),
        ),
    )

    conn.commit()


# ---------------------------------------------------------------------------
# iCloud Shared Streams API
# ---------------------------------------------------------------------------

def icloud_url(host, endpoint):
    return (
        f"https://{host}/{ICLOUD_TOKEN}"
        f"/sharedstreams/{endpoint}"
    )


def post_icloud(host, endpoint, payload):
    url = icloud_url(host, endpoint)

    response = icloud_session.post(
        url,
        headers={"Content-Type": "text/plain"},
        json=payload,
        timeout=REQUEST_TIMEOUT,
        allow_redirects=False,
    )

    return response


def get_icloud_host(current_host):
    """
    Ask the current Shared Streams host for webstream.

    Apple commonly responds with HTTP 330 and:
        X-Apple-MMe-Host: p164-sharedstreams.icloud.com

    If so, return the new host.

    Otherwise return the current host.
    """

    response = post_icloud(
        current_host,
        "webstream",
        {"streamCtag": None},
    )

    if response.status_code == 330:
        new_host = response.headers.get("X-Apple-MMe-Host")

        if not new_host:
            try:
                new_host = response.json().get("X-Apple-MMe-Host")
            except Exception:
                new_host = None

        if not new_host:
            raise RuntimeError(
                "Apple returned HTTP 330 but no "
                "X-Apple-MMe-Host was supplied."
            )

        log.info(
            "Apple redirected Shared Album to %s",
            new_host,
        )

        return new_host

    if response.status_code != 200:
        raise RuntimeError(
            f"iCloud webstream host discovery failed: "
            f"HTTP {response.status_code}: "
            f"{response.text[:500]}"
        )

    return current_host


def get_webstream(host, stream_ctag=None):
    """
    Fetch the current Shared Album state.

    If the server responds with HTTP 330, automatically switch
    to the X-Apple-MMe-Host supplied by Apple and retry.
    """

    payload = {
        "streamCtag": stream_ctag,
    }

    response = post_icloud(
        host,
        "webstream",
        payload,
    )

    if response.status_code == 330:
        new_host = response.headers.get("X-Apple-MMe-Host")

        if not new_host:
            try:
                new_host = response.json().get("X-Apple-MMe-Host")
            except Exception:
                new_host = None

        if not new_host:
            raise RuntimeError(
                "Apple returned HTTP 330 but no "
                "X-Apple-MMe-Host was supplied."
            )

        log.info(
            "Apple redirected Shared Album to %s",
            new_host,
        )

        response = post_icloud(
            new_host,
            "webstream",
            payload,
        )

        host = new_host

    if response.status_code != 200:
        raise RuntimeError(
            f"iCloud webstream failed: "
            f"HTTP {response.status_code}: "
            f"{response.text[:1000]}"
        )

    return host, response.json()


def get_webasseturls(host, photo_guids):
    """
    Request signed download URLs for a list of photoGuid values.
    """

    response = post_icloud(
        host,
        "webasseturls",
        {
            "photoGuids": photo_guids,
        },
    )

    if response.status_code == 330:
        new_host = response.headers.get("X-Apple-MMe-Host")

        if not new_host:
            try:
                new_host = response.json().get("X-Apple-MMe-Host")
            except Exception:
                new_host = None

        if new_host:
            host = new_host

            response = post_icloud(
                host,
                "webasseturls",
                {
                    "photoGuids": photo_guids,
                },
            )

    if response.status_code != 200:
        raise RuntimeError(
            f"iCloud webasseturls failed: "
            f"HTTP {response.status_code}: "
            f"{response.text[:1000]}"
        )

    return host, response.json()


# ---------------------------------------------------------------------------
# Shared Album helpers
# ---------------------------------------------------------------------------

def largest_derivative(photo):
    derivatives = photo.get("derivatives") or {}

    if not derivatives:
        raise RuntimeError(
            f"No derivatives available for "
            f"{photo.get('photoGuid')}"
        )

    def size_key(item):
        key, derivative = item

        try:
            width = int(derivative.get("width", 0))
        except (TypeError, ValueError):
            width = 0

        try:
            height = int(derivative.get("height", 0))
        except (TypeError, ValueError):
            height = 0

        try:
            file_size = int(derivative.get("fileSize", 0))
        except (TypeError, ValueError):
            file_size = 0

        return (
            width * height,
            file_size,
            int(key) if str(key).isdigit() else 0,
        )

    _, derivative = max(
        derivatives.items(),
        key=size_key,
    )

    return derivative


def filename_from_url(url):
    path = urlparse(url).path

    filename = unquote(
        Path(path).name
    )

    if filename:
        return filename

    return None


def safe_filename(filename):
    """
    Prevent path traversal and problematic filenames.
    """

    filename = Path(filename).name

    filename = re.sub(
        r"[^A-Za-z0-9._() -]",
        "_",
        filename,
    )

    if not filename:
        filename = "icloud-asset"

    return filename


def media_type_for_filename(filename):
    media_type, _ = mimetypes.guess_type(filename)

    if media_type:
        return media_type

    return "application/octet-stream"


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------

def download_asset(url, destination):
    destination.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    log.info(
        "Downloading %s",
        destination.name,
    )

    with icloud_session.get(
        url,
        stream=True,
        timeout=REQUEST_TIMEOUT,
    ) as response:

        response.raise_for_status()

        with open(destination, "wb") as output:
            for chunk in response.iter_content(
                chunk_size=1024 * 1024
            ):
                if chunk:
                    output.write(chunk)

    return destination


# ---------------------------------------------------------------------------
# Immich API
# ---------------------------------------------------------------------------

def immich_request(method, endpoint, **kwargs):
    url = f"{IMMICH_URL}/api/{endpoint.lstrip('/')}"

    response = immich_session.request(
        method,
        url,
        timeout=REQUEST_TIMEOUT,
        **kwargs,
    )

    if not response.ok:
        raise RuntimeError(
            f"Immich API failed: "
            f"{method} {endpoint} "
            f"HTTP {response.status_code}: "
            f"{response.text[:1000]}"
        )

    return response


def get_immich_albums():
    response = immich_request(
        "GET",
        "albums",
    )

    return response.json()


def get_or_create_immich_album(name):
    albums = get_immich_albums()

    for album in albums:
        if album.get("albumName") == name:
            log.info(
                "Using existing Immich album '%s' (%s)",
                name,
                album.get("id"),
            )
            return album["id"]

    if DRY_RUN:
        log.info(
            "DRY RUN: would create Immich album '%s'",
            name,
        )
        return None

    response = immich_request(
        "POST",
        "albums",
        json={
            "albumName": name,
            "assetIds": [],
        },
    )

    album = response.json()

    log.info(
        "Created Immich album '%s' (%s)",
        name,
        album["id"],
    )

    return album["id"]


def upload_to_immich(
    file_path,
    filename,
):
    """
    Upload one asset to Immich.

    Immich's upload endpoint performs server-side
    deduplication as well.
    """

    if DRY_RUN:
        log.info(
            "DRY RUN: would upload %s",
            filename,
        )
        return "DRY-RUN-ASSET-ID"

    now = datetime.now(
        timezone.utc
    ).isoformat()

    mime_type = media_type_for_filename(
        filename
    )

    data = {
        "deviceAssetId": (
            f"icloud-shared-{filename}"
        ),
        "deviceId": "icloud-shared-album",
        "fileCreatedAt": now,
        "fileModifiedAt": now,
        "isFavorite": "false",
    }

    with open(file_path, "rb") as file_handle:

        files = {
            "assetData": (
                filename,
                file_handle,
                mime_type,
            )
        }

        response = immich_request(
            "POST",
            "assets",
            data=data,
            files=files,
        )

    result = response.json()

    asset_id = result.get("id")

    if not asset_id:
        raise RuntimeError(
            f"Immich upload returned no asset ID: "
            f"{result}"
        )

    if result.get("duplicate"):
        log.info(
            "Immich reports duplicate for %s: %s",
            filename,
            asset_id,
        )
    else:
        log.info(
            "Uploaded %s → %s",
            filename,
            asset_id,
        )

    return asset_id


def add_asset_to_album(
    album_id,
    asset_id,
):
    if not album_id:
        return

    if DRY_RUN:
        log.info(
            "DRY RUN: would add %s to album %s",
            asset_id,
            album_id,
        )
        return

    immich_request(
        "PUT",
        f"albums/{album_id}/assets",
        json={
            "ids": [asset_id],
        },
    )

    log.info(
        "Added asset %s to Immich album",
        asset_id,
    )


# ---------------------------------------------------------------------------
# One synchronization pass
# ---------------------------------------------------------------------------

def sync_once(conn):
    TMP_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    stored_host = get_meta(
        conn,
        "icloud_host",
    )

    host = (
        stored_host
        or INITIAL_ICLOUD_HOST
    )

    stored_ctag = get_meta(
        conn,
        "stream_ctag",
    )

    log.info(
        "Checking iCloud Shared Album '%s'",
        IMMICH_ALBUM_NAME,
    )

    log.info(
        "Using Shared Streams host: %s",
        host,
    )

    host, stream = get_webstream(
        host,
        stored_ctag,
    )

    # Store the discovered host.
    if host != stored_host:
        set_meta(
            conn,
            "icloud_host",
            host,
        )

    stream_name = stream.get(
        "streamName",
        IMMICH_ALBUM_NAME,
    )

    photos = stream.get(
        "photos",
        [],
    )

    new_ctag = stream.get(
        "streamCtag",
    )

    log.info(
        "iCloud album: '%s'",
        stream_name,
    )

    log.info(
        "Assets reported by iCloud: %d",
        len(photos),
    )

    # If ctag is unchanged, Apple has no changes.
    if (
        stored_ctag
        and new_ctag
        and stored_ctag == new_ctag
    ):
        log.info(
            "streamCtag unchanged; nothing to synchronize."
        )
        return

    # Use the configured Immich album name rather than trusting
    # Apple's album name. This makes renaming on the Immich side
    # possible without changing the iCloud source.
    album_id = get_or_create_immich_album(
        IMMICH_ALBUM_NAME
    )

    candidates = []

    for photo in photos:

        photo_guid = photo.get(
            "photoGuid"
        )

        if not photo_guid:
            log.warning(
                "Skipping asset without photoGuid"
            )
            continue

        derivative = largest_derivative(
            photo
        )

        checksum = derivative.get(
            "checksum"
        )

        if not checksum:
            log.warning(
                "Skipping %s: derivative has no checksum",
                photo_guid,
            )
            continue

        existing = get_asset(
            conn,
            photo_guid,
        )

        # Same photoGuid + checksum = already synced.
        if (
            existing
            and existing["checksum"] == checksum
            and existing["immich_asset_id"]
        ):
            continue

        candidates.append(
            {
                "photo": photo,
                "photo_guid": photo_guid,
                "derivative": derivative,
                "checksum": checksum,
            }
        )

    log.info(
        "Already synchronized: %d",
        len(photos) - len(candidates),
    )

    log.info(
        "New/changed assets: %d",
        len(candidates),
    )

    if not candidates:
        if new_ctag and not DRY_RUN:
            set_meta(
                conn,
                "stream_ctag",
                new_ctag,
            )

        return

    if DRY_RUN:
        log.info(
            "DRY RUN is enabled - no files will be uploaded."
        )

    # Request signed URLs for all candidates.
    photo_guids = [
        candidate["photo_guid"]
        for candidate in candidates
    ]

    host, url_data = get_webasseturls(
        host,
        photo_guids,
    )

    items = url_data.get(
        "items",
        {},
    )

    successful = 0

    for candidate in candidates:

        photo = candidate["photo"]
        photo_guid = candidate["photo_guid"]
        derivative = candidate["derivative"]
        checksum = candidate["checksum"]

        item = items.get(
            checksum
        )

        if not item:
            log.error(
                "No download URL for %s / checksum %s",
                photo_guid,
                checksum,
            )
            continue

        url_location = item.get(
            "url_location"
        )

        url_path = item.get(
            "url_path"
        )

        if not url_location or not url_path:
            log.error(
                "Incomplete download information for %s",
                photo_guid,
            )
            continue

        # Build the actual signed URL.
        locations = url_data.get(
            "locations",
            {},
        )

        location = locations.get(
            url_location
        )

        if not location:
            log.error(
                "No location definition for %s",
                url_location,
            )
            continue

        scheme = location.get(
            "scheme",
            "https",
        )

        hosts = location.get(
            "hosts",
            [],
        )

        if not hosts:
            log.error(
                "No hosts for location %s",
                url_location,
            )
            continue

        download_url = (
            f"{scheme}://{hosts[0]}"
            f"{url_path}"
        )

        filename = (
            filename_from_url(
                download_url
            )
            or f"{photo_guid}.jpg"
        )

        filename = safe_filename(
            filename
        )

        local_file = (
            TMP_DIR
            / f"{photo_guid}-{filename}"
        )

        try:

            if DRY_RUN:
                log.info(
                    "DRY RUN: %s "
                    "(%sx%s, %s bytes)",
                    filename,
                    derivative.get("width"),
                    derivative.get("height"),
                    derivative.get("fileSize"),
                )

                # Don't actually download in dry-run.
                successful += 1
                continue

            download_asset(
                download_url,
                local_file,
            )

            asset_id = upload_to_immich(
                local_file,
                filename,
            )

            add_asset_to_album(
                album_id,
                asset_id,
            )

            save_asset(
                conn,
                photo_guid,
                checksum,
                filename,
                media_type_for_filename(
                    filename
                ),
                asset_id,
            )

            successful += 1

        except Exception as exc:

            log.exception(
                "Failed to synchronize %s: %s",
                filename,
                exc,
            )

        finally:

            if local_file.exists():
                try:
                    local_file.unlink()
                except OSError:
                    log.warning(
                        "Could not delete temporary file %s",
                        local_file,
                    )

    # Only advance the ctag when everything was successful.
    if successful == len(candidates):

        if not DRY_RUN:
            if new_ctag:
                set_meta(
                    conn,
                    "stream_ctag",
                    new_ctag,
                )

            set_meta(
                conn,
                "icloud_host",
                host,
            )

            log.info(
                "Sync completed successfully: %d asset(s)",
                successful,
            )

    else:

        log.warning(
            "Sync incomplete: %d/%d assets succeeded. "
            "streamCtag will NOT be advanced.",
            successful,
            len(candidates),
        )


# ---------------------------------------------------------------------------
# Healthcheck
# ---------------------------------------------------------------------------

def healthcheck():
    conn = db_connect()
    conn.close()

    if not IMMICH_API_KEY:
        raise RuntimeError(
            "IMMICH_API_KEY is empty"
        )

    log.info("Healthcheck OK")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--once",
        action="store_true",
        help="Run one synchronization pass and exit.",
    )

    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Override DRY_RUN=true for this invocation.",
    )

    parser.add_argument(
        "--healthcheck",
        action="store_true",
        help="Run healthcheck and exit.",
    )

    args = parser.parse_args()

    global DRY_RUN

    if args.dry_run:
        DRY_RUN = True

    if args.healthcheck:
        healthcheck()
        return

    conn = db_connect()

    log.info(
        "Starting iCloud → Immich sync"
    )

    log.info(
        "Album: %s",
        IMMICH_ALBUM_NAME,
    )

    log.info(
        "Immich: %s",
        IMMICH_URL,
    )

    log.info(
        "Dry run: %s",
        DRY_RUN,
    )

    try:

        while True:

            started = time.monotonic()

            try:
                sync_once(conn)

            except Exception as exc:
                log.exception(
                    "Synchronization failed: %s",
                    exc,
                )

            if args.once:
                break

            elapsed = (
                time.monotonic()
                - started
            )

            sleep_for = max(
                1,
                SYNC_INTERVAL - int(elapsed),
            )

            log.info(
                "Next synchronization in %d seconds",
                sleep_for,
            )

            time.sleep(
                sleep_for
            )

    finally:
        conn.close()


if __name__ == "__main__":
    main()
