# whatsapp-backup

A Docker container that uses [wacli](https://github.com/steipete/wacli) to back up WhatsApp messages locally.
Messages, media, and session data are stored in a persistent volume on the host.

## Requirements

- [Docker](https://docs.docker.com/get-docker/) 20.10+
- [Docker Compose](https://docs.docker.com/compose/) v2 (optional, but recommended)

## Quick start

### 1. Build the image

```sh
docker compose build
```

Or without Compose:

```sh
docker build -t whatsapp-backup .
```

### 2. Authenticate (first run only)

This step shows a QR code that you scan in WhatsApp → Settings → Linked Devices.

```sh
docker compose run --rm whatsapp-backup auth
```

Or without Compose:

```sh
docker run -it --rm -v "$(pwd)/data:/data" whatsapp-backup auth
```

### 3. Start continuous backup

Once authenticated, start the container to keep syncing messages in the background:

```sh
docker compose up -d
```

Or without Compose:

```sh
docker run -d --name whatsapp-backup -v "$(pwd)/data:/data" whatsapp-backup
```

The default command is `sync --follow`, which continuously syncs new messages.

### 4. Search messages

```sh
docker compose run --rm whatsapp-backup messages search "meeting"
```

### 5. Check status / diagnostics

```sh
docker compose run --rm whatsapp-backup doctor
```

## Data storage

All data (session credentials, SQLite message store, and downloaded media) is stored in
the `./data` directory on the host, mounted into the container at `/data`.
Back up this directory to preserve your message history.

## Configuration

The following environment variables can be set in `docker-compose.yml`:

| Variable              | Default           | Description                                            |
|-----------------------|-------------------|--------------------------------------------------------|
| `WACLI_STORE_DIR`     | `/data`           | Directory where wacli stores session and messages.     |
| `WACLI_DEVICE_LABEL`  | `whatsapp-backup` | Label shown in WhatsApp's Linked Devices list.         |
| `WACLI_DEVICE_PLATFORM` | *(unset)*       | Override linked device platform (default: `CHROME`).  |
| `WACLI_READONLY`      | *(unset)*         | Set to `1` to block any write commands.                |

## Advanced usage

Any `wacli` command can be passed directly:

```sh
# List recent chats
docker compose run --rm whatsapp-backup chats list

# Backfill older messages for a specific chat
docker compose run --rm whatsapp-backup history backfill \
  --chat 1234567890@s.whatsapp.net --requests 10 --count 50

# Download media for a message
docker compose run --rm whatsapp-backup media download \
  --chat 1234567890@s.whatsapp.net --id <message-id>

# Log out / unlink the device
docker compose run --rm whatsapp-backup auth logout
```

See the [wacli documentation](https://github.com/steipete/wacli#command-surface) for the
full command reference.

## Building a specific wacli version

Pass `--build-arg WACLI_VERSION=<tag>` to pin a release:

```sh
docker build --build-arg WACLI_VERSION=v0.6.0 -t whatsapp-backup .
```