# iCloud Shared Album → Immich Sync

A lightweight Docker-based synchronizer that copies photos from an **iCloud Shared Album** to an **Immich** server.

The project uses Apple's iCloud Shared Streams API and the Immich HTTP API. It is designed to run continuously in Docker and keeps a local SQLite database to remember which assets have already been synchronized.

## Features

- Sync photos from an iCloud Shared Album to Immich
- No Apple ID password required
- Uses the iCloud Shared Streams API
- Automatically discovers the current iCloud Shared Streams host
- Downloads the largest available photo derivative
- Uploads photos to Immich
- Automatically creates the target Immich album when necessary
- Configurable target album name
- Persistent SQLite state
- Uses iCloud `streamCtag` to detect album changes efficiently
- Avoids unnecessary duplicate uploads
- Temporary downloaded files are removed after processing
- Configurable synchronization interval
- Dry-run mode for testing
- Docker healthcheck

## How it works

The synchronization is one-way:

    iCloud Shared Album
            |
            v
    iCloud Shared Streams API
            |
            v
    temporary local file
            |
            v
    Immich API
            |
            v
    Immich album

During a synchronization run, the application:

1. Connects to the configured iCloud Shared Album.
2. Checks the current album state and `streamCtag`.
3. Determines which assets are new or have changed.
4. Requests the required download information from iCloud.
5. Downloads the best available photo derivative.
6. Uploads the asset to Immich.
7. Adds the asset to the configured Immich album.
8. Stores synchronization state in SQLite.
9. Removes the temporary file.

The current iCloud Shared Streams API host is stored in the local state database. If Apple moves the shared album to another host, the application can discover and use the new host.

## Requirements

- Docker
- Docker Compose
- An accessible Immich server
- An Immich API key
- An iCloud Shared Album
- The token from the iCloud Shared Album URL

The application does **not** require an Apple ID password or access to the local Apple Photos library.

## Configuration

Create a local `.env` file from `.env.example`:

    cp .env.example .env

Edit `.env` and provide the required values:

    ICLOUD_SHARED_ALBUM_TOKEN=your_shared_album_token
    IMMICH_URL=http://your-immich-server:2283
    IMMICH_API_KEY=your_immich_api_key
    IMMICH_ALBUM_NAME=My Shared Album

### Environment variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `ICLOUD_SHARED_ALBUM_TOKEN` | Yes | — | Token identifying the iCloud Shared Album |
| `IMMICH_URL` | Yes | — | Base URL of the Immich server |
| `IMMICH_API_KEY` | Yes | — | Immich API key |
| `IMMICH_ALBUM_NAME` | No | `My Shared Album` | Target Immich album |
| `SYNC_INTERVAL` | No | `1800` | Synchronization interval in seconds |
| `STATE_DB` | No | `/data/state.db` | SQLite state database |
| `TMP_DIR` | No | `/tmp/icloud` | Temporary download directory |
| `LOG_LEVEL` | No | `INFO` | Logging level |
| `DRY_RUN` | No | `true` | Prevent uploads while testing |
| `REQUEST_TIMEOUT` | No | `60` | HTTP request timeout in seconds |
| `ICLOUD_SHAREDSTREAMS_HOST` | No | `p23-sharedstreams.icloud.com` | Initial iCloud API host |

## iCloud Shared Album token

The application uses the token belonging to an iCloud Shared Album.

The token can be obtained from the public/shared album URL.

Keep the token secret. It must only be stored in the local `.env` file.

**Never commit `.env` to Git.**

The repository's `.gitignore` already excludes the file.

## Immich API key

Create an API key in Immich and provide it through:

    IMMICH_API_KEY=your_api_key

The key is used for communication with the Immich API, including uploading assets and managing the target album.

Keep the API key secret and never commit it to Git.

## Docker Compose

Build and start the container:

    docker compose up -d --build

Check the container:

    docker compose ps

View the logs:

    docker compose logs -f icloud-immich-sync

By default, synchronization runs every 30 minutes.

The interval can be changed with:

    SYNC_INTERVAL=900

For example, `900` means 15 minutes.

## Dry-run mode

The default configuration uses:

    DRY_RUN=true

Dry-run mode is intended for initial testing.

It allows the application to inspect the iCloud Shared Album without uploading assets to Immich.

Once the configuration has been verified, set:

    DRY_RUN=false

Then restart the container:

    docker compose up -d

## Persistent state

Synchronization state is stored in:

    /data/state.db

The provided Docker Compose configuration persists this database in:

    ./data/state.db

The database allows the application to remember previously synchronized assets and the current iCloud Shared Streams state.

The `data/` directory is intentionally excluded from Git.

## Temporary files

iCloud assets are downloaded temporarily to:

    /tmp/icloud

The Docker Compose configuration maps this to:

    ./tmp:/tmp/icloud

Temporary files are removed after processing and are not intended to be persistent storage.

## Healthcheck

The Docker container includes a healthcheck:

    python /app/sync.py --healthcheck

This allows Docker to monitor the application independently of the normal synchronization loop.

## Command line

The application supports command-line operation for testing and maintenance.

Run one synchronization pass:

    python /app/sync.py --once

Run the healthcheck:

    python /app/sync.py --healthcheck

Dry-run behavior can be enabled through the `DRY_RUN` environment variable.

## Data flow and privacy

The application communicates with:

### Apple iCloud

The application uses:

- the Shared Album token
- iCloud Shared Streams API endpoints
- download URLs returned by Apple's API

### Immich

The application uses:

- the configured Immich server URL
- the Immich API key
- Immich API endpoints for assets and albums

The application does not require:

- an Apple ID password
- access to an Apple Photos library
- direct access to the Immich database

Downloaded media is only stored temporarily during synchronization.

## Limitations

This project is specifically intended for **iCloud Shared Albums**.

It does not synchronize:

- the complete iCloud Photos library
- the Apple Photos Camera Roll directly
- private iCloud Photos that are not available through a Shared Album

Synchronization is one-way:

    iCloud Shared Album → Immich

Changes made in Immich are not propagated back to iCloud.

## Security

Treat the following values as secrets:

    ICLOUD_SHARED_ALBUM_TOKEN
    IMMICH_API_KEY

Never commit them to Git.

The real `.env` file and the local SQLite state database are excluded from the repository by `.gitignore`.

## Project structure

    icloud-immich-sync/
    ├── .env.example
    ├── .gitignore
    ├── Dockerfile
    ├── docker-compose.yaml
    ├── README.md
    ├── requirements.txt
    └── sync.py

Runtime data is intentionally kept outside Git:

    data/
    └── state.db

    tmp/
    └── temporary iCloud downloads

## License

No license has currently been specified.

If this project is intended for public reuse, adding an open-source license such as MIT is recommended.

## Disclaimer

This is an independent project and is not affiliated with or endorsed by Apple or Immich.

Apple may change the behavior or availability of the iCloud Shared Streams API at any time. Such changes may require updates to this project.
