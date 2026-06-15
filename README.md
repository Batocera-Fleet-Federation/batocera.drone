# Batocera Drone
![Batocera Fleet Federation](./main.jpeg)
Batocera Drone is a web control panel for your Batocera game system.

After it is installed, you open Drone from a browser on your computer, phone, or tablet. From there you can browse your Batocera library, search games, manage artwork, edit game details, inspect BIOS and theme files, and use admin tools without sitting at the Batocera machine itself.

## TL;DR

- Drone runs on each Batocera device and gives you a local web UI.
- Overmind is the central hub that knows about all your Drones.
- Each Drone checks in with Overmind, receives the current swarm list, and can test whether it can reach the other Drones directly.
- Drone-to-Drone API calls can use mTLS. Drone creates its own local certificate on startup, so you do not need a public domain.
- Drone reports live telemetry back to Overmind, including speed samples, filesystem changes, peer checks, gameplay, and ROM/library changes.
- Containers are supported for local swarm testing. The Drone container creates a Batocera-like `/userdata` tree and copies a varied set of ROMs from `.github/data/roms/<system>/<files>`.
- Drone checks in with Overmind every 30 seconds by default.
- Overmind integration now uses an Overmind-generated authorization token instead of an integration password.
- Drone caches approved peer certificates from Overmind before Drone-to-Drone mTLS calls.
- The Drone admin header uses the shared project mascot at `content/batocera-swarm-mascot.jpg`, matching Overmind's landing and header branding without interfering with core workflows.

## What You Can Do With It

- Browse systems, ROMs, BIOS files, artwork, videos, manuals, and theme assets.
- Search your whole game library from one page.
- View and edit game information stored in `gamelist.xml`.
- Upload and manage boxart, screenshots, thumbnails, fanart, and marquees.
- Import artwork and metadata from LaunchBox and TheGamesDB when available.
- Scrape, upload, and crop artwork from the admin artwork page or an individual ROM artwork page.
- Use admin tools for logs, configs, cleanup, system information, and troubleshooting.
- Link to Overmind to report status, telemetry, peer checks, and processed actions.
- Use the built-in API if you want to automate against your Batocera machine.

## Install On Batocera

You install Drone by running one command on the Batocera machine.

Open a terminal or SSH session to Batocera, then paste this:

```bash
curl -fsSL https://github.com/Batocera-Fleet-Federation/batocera.drone/releases/latest/download/batocera_install.sh | bash
```

The installer will ask for the username and password you want to use when opening Drone in your browser.

When it finishes, restart Batocera. If you do not want to restart yet, you can start Drone immediately with:

```bash
/userdata/system/services/DRONE_SERVER start
```

Then open Drone in your browser:

```text
https://<your-batocera-name>.local
```

Example:

```text
https://batocera.local
```

Drone also listens on `https://<your-batocera-name>.local:8443` for backwards compatibility with older installs and bookmarks.

Your browser may warn you about the certificate. That is expected because Drone creates a self-signed local certificate by default.

## Login

Drone is protected with a username and password.

Use the username and password you entered during installation. Do not use an easy password if your Batocera machine is reachable by other people on your network.

## Security

Drone is designed to avoid running as root for normal app work.

The installer creates a dedicated local user called `drone-app`. Drone runs as that limited user and only receives write access to the files it needs to manage:

- Artwork, videos, manuals, and `gamelist.xml`.
- Drone runtime files, local certificates, and Drone logs.

The app should only have read-only access to ROM files, Batocera system configuration, emulator configuration, and most other system files.

In plain language: Drone can update library metadata and media, but it is not supposed to freely modify or delete your ROM collection or Batocera system files.

## API

Drone also includes an API for advanced users and other tools.

The API starts here:

```text
https://<your-batocera-name>.local/v1/api
```

Interactive API documentation is here:

```text
https://<your-batocera-name>.local/v1/api/swagger
```

The admin UI also has an **API Access** page. It links to Swagger, shows certificate metadata, and lets you download the public certificate. It does not download or display the private key.

The machine-readable OpenAPI file is here:

```text
https://<your-batocera-name>.local/v1/api/openapi.json
```

## Overmind Integration

Batocera Drone is the local Batocera agent. Overmind owns the Overlord UI, Drone authorization tokens, action queue, swarm list, peer status, and per-Drone auto-sync policy.

Drone calls Overmind every 30 seconds by default. The alive payload includes the MAC-address `device_id`, discovered IPv4/IPv6 addresses, router/gateway IP when available, public IP when available, API port, protocol, local certificate metadata, ROM systems, and basic system information. During this poll Drone checks emulator configs and log sources and uploads only new or changed content, retrying until Overmind accepts it. When disconnection leaves more log output pending than one upload can hold, Drone sends the most recent output with an omission marker rather than replaying stale logs for many polls. Overmind validates `Authorization: Bearer <drone_token>` before accepting alive, action status, metadata, logs, telemetry events, peer checks, or speed samples.

Overmind responds with the current swarm list, including each peer's reported public IP and public peer API URL. Drone stores that list locally, skips itself, and uses the public endpoint first for health checks and peer-to-peer asset transfers. The admin Overmind page shows whether the last peer check passed or failed, when it was checked, and the failure reason if there was one.

Actions use a pull model because Overmind usually cannot connect inward to a Drone on a home network. The Overlord queues actions in Overmind, the Drone receives the current pending batch during alive polling, performs each action sequentially, then posts each result back to Overmind. Remote Restart is simulated in fake/demo mode. Kiosk mode actions set or remove EmulationStation's `UIMode` value and restart EmulationStation when Batocera's restart tool is available. Remote shutdown is not supported; legacy shutdown actions are rejected without execution.

Drone also reports live events to Overmind. These include filesystem create/update/delete events for watched ROM/config/artwork/log paths, ROM/library changes, gameplay activity, and speed samples. Gameplay start/stop detection polls Linux procfs for Batocera's active `emulatorlauncher -system ... -rom ...` command every two seconds by default, rather than parsing EmulationStation launch logs. Set `GAME_PROCESS_POLL_SECONDS` to adjust that interval. Speed samples measure the Drone's Internet connection through Cloudflare Speed Test endpoints (`speed.cloudflare.com`), rather than transferring probe data through Overmind; only the resulting sample is reported to Overmind. Once Drone is connected to Overmind, it runs a speed test during its initial startup check-in and every 10 minutes thereafter by default. Set `OVERMIND_SPEED_SAMPLE_SECONDS` to adjust that interval, or set `DRONE_SPEED_TEST_BASE_URL`, `DRONE_SPEED_TEST_BYTES` (default `1000000` per direction), or `DRONE_SPEED_TEST_TIMEOUT_SECONDS` when a different compatible endpoint or probe size is needed.

System information is collected at startup and refreshed occasionally. It can include hostname, OS/platform, Batocera version when available, Drone app version, architecture, CPU count, memory, disk, network addresses, uptime, and whether Drone is running in Docker.

Drone-owned durable state is stored in its local SQLite database: Overmind configuration, swarm and peer-check snapshots, upload cursors/fingerprints, credentials, small MD5 lookup results, peer certificate metadata, and processed action history. Existing JSON or action-log state files are imported on first access and removed after successful migration. `OVERMIND_ACTION_LOG_MAX_BYTES` is retained as a compatibility setting and bounds the number of completed-action records retained in SQLite. Normal Drone stdout/stderr logs remain rotating files controlled by `LOG_MAX_BYTES` and `LOG_BACKUP_COUNT`, because log collection tails those streams directly.

Local fake mode is opt-in with `USE_FAKE_DATA=true`. Normal local Compose starts Drones unapproved so Overmind can show the pending Psionic connection.

## Local Network Mode

Each Drone runs in exactly one control-plane mode:

- **Overmind Integration** keeps the existing heartbeat, action, telemetry, inventory upload, and Overmind-managed swarm behavior.
- **Local Network** suspends all Overmind communication and discovers nearby Drones with a local multicast announcement. Local asset caches and filesystem watchers continue running.

Choose the mode with the toggle on **Admin > Integration**. The page shows only the controls for the active integration. In Local Network mode, a discovered Drone is not trusted automatically. Open the same page on the other Drone, enter its short-lived eight-digit pairing code, and confirm the pairing. Pairing exchanges and pins each Drone's public certificate; private keys never leave their Drone.

After pairing, the page shows peer health and lets an administrator browse and copy ROMs, BIOS, artwork, and saves directly from the other Drone. Transfers use the existing recipient-pull queue, run one at a time, verify the advertised fingerprint or MD5 when available, and appear in the normal Downloads panel.

Local mode state, discovered peers, paired peers, pairing codes, and health snapshots are stored in Drone's existing SQLite state database. The saved Overmind configuration is retained while suspended, so switching back to Overmind mode resumes the existing integration without reconfiguration.

## Drone-to-Drone Security

Drone can protect peer API routes with mTLS. In plain language, one Drone must show its local certificate before another Drone answers peer API calls.

Drone creates or reuses a local self-signed certificate on startup. It does not need Let's Encrypt and does not need a public domain name. The certificate metadata is sent to Overmind so the swarm UI can show which certificate a Drone is using. The private key stays on the Drone.

Useful settings:

```bash
DRONE_MTLS_ENABLED=true
DRONE_CERT_FILE=/userdata/system/drone-app/certs/drone.crt
DRONE_KEY_FILE=/userdata/system/drone-app/certs/drone.key
DRONE_CERT_DAYS=825
```

If you use your own certificate authority, set `DRONE_MTLS_CA_FILE` so Drone can ask the TLS layer to verify peer certificates.

In Overmind mode, Drone fetches approved peer public certificates from Overmind and caches them in `/userdata/system/drone-app/peer-certs/`. In Local Network mode, an administrator-approved pairing exchanges and pins peer certificates separately in `/userdata/system/drone-app/local-peer-certs/`. Local pairing trust is inactive in Overmind mode unless Overmind independently approved the same certificate. Discovery alone never grants access to peer health, inventory, or files.

For API clients that need mTLS, use your client certificate and key from a trusted system:

```bash
curl --cert client.crt --key client.key -k "https://<drone-host>/health"
```

Keep private keys private. If a key is exposed, recreate or rotate the certificate.

## Docker

Build the local image:

```bash
docker build -t ghcr.io/batocera-fleet-federation/batocera-drone:local .
```

The container entrypoint creates the folders, configs, logs, and ROM mount points Drone expects on Batocera. For swarm testing, run it through the shared Compose setup in the `.github` repo so each Drone gets a different identity and a copied subset of ROM files.

The shared Compose swarm runs four lightweight Drones with unique hostnames, device ids, MAC addresses, ports, and volumes. Fake data is disabled unless `USE_FAKE_DATA=true` is set.

## ROM Sync

ROM sync is requested from Overmind, not by choosing a source Drone manually. The target Drone receives a `sync_rom` or `sync_system` action, checks its stored swarm list, picks a healthy source that Overmind has verified as publicly resolvable and that has the requested ROM, downloads one file at a time through the peer API, and reports sync activity back to Overmind. Drones without a verified public peer endpoint are never selected as ROM, BIOS, or artwork download sources.

Publish a multi-arch GHCR image:

```bash
gh auth login
echo "$GITHUB_TOKEN" | docker login ghcr.io -u <github-user> --password-stdin
./scripts/docker-publish.sh --push
```

The publish script targets `linux/amd64` and `linux/arm64`, tags the next patch version, and updates `latest`. Use `--dry-run` to see the version and command without building.

## Advanced Users

This section is for people who are comfortable with terminals, environment variables, local testing, and API tools.

### Set Username And Password Manually

Drone reads these values when starting:

```bash
DRONE_APP_USERNAME="admin"
DRONE_APP_PASSWORD="change-this-password"
```

The installer and `run_now.sh` use these if they are already set. If they are not set, the scripts prompt you.

### Disable Admin Features

To hide and block admin routes:

```bash
ALLOW_ADMIN=false
```

### Disable Downloads

To prevent ROM and BIOS downloads through Drone:

```bash
ALLOW_CONTENT_DOWNLOAD=false
```

### Overmind Heartbeat and ROM Metadata

The Drone admin Overmind Integration page supports two onboarding paths. The token flow uses an authorization token generated in Overmind. The Claim Ownership flow asks for Overmind URL, email, and password, sends them to Overmind over HTTPS, and stores only the returned Drone bearer token. Passwords are not logged or persisted by Drone.

Drone heartbeats are intentionally lightweight. They report Drone identity, name, reachable network details, certificate metadata, downloads, and basic system health/status. Heartbeats do not scan ROM folders and do not calculate ROM MD5 hashes.

Each heartbeat logs the send start, Overmind heartbeat endpoint, success or failure, response status when available, and duration.

ROM inventory is handled by a separate low-priority poller. Its initial run waits 60 seconds after startup so Batocera services can settle, and subsequent polls default to every 5 minutes. Configure it with:

```bash
ROM_METADATA_POLL_SECONDS=300
ROM_METADATA_INITIAL_DELAY_SECONDS=60
ROM_METADATA_HASH_IO_YIELD_SECONDS=0.05
```

The poller and other Drone-owned durable state share the local SQLite database at:

```text
/userdata/system/drone-app/rom_metadata_cache.sqlite3
```

The database filename is retained for in-place compatibility with existing ROM caches; it now also contains keyed application state, completed action records, and an outbound asset-change queue. Existing JSON caches are migrated on first use. On each poll Drone scans file size and modified time first, upserts only added or changed rows, deletes rows for removed assets, hashes only new or changed ROM files, and sends Overmind only pending asset upserts and deletions. A full catalog replacement is sent only by the queued Rebuild Asset Metadata action. Local collection and caching continue even when Drone is not connected to Overmind or Overmind is temporarily unavailable; unsent changes remain queued for retry. Discovery and MD5 work are checkpointed during progress so a restarted Drone resumes from completed hashes instead of starting the metadata build over. MD5 reads yield between chunks to avoid monopolizing slower storage, and heartbeat filesystem scanning pauses while metadata work is active.

ROM metadata logs show cache load, scan, checkpoint, MD5 hashing, cache update, upload/skip, counts, and durations. The checkpoint cadence defaults to 250 assets or 30 seconds and can be changed with `ROM_METADATA_PROGRESS_FILES` and `ROM_METADATA_PROGRESS_SECONDS`; `ROM_METADATA_HASH_IO_YIELD_SECONDS` controls the storage-friendly pause after each 1 MB hashing read. During metadata activity Drone defers the emulator config crawl and duplicate filesystem telemetry walk so those tasks do not contend for the same drive. Individual ROM paths are not logged by default.

### API Example

```bash
curl -k -u <username>:<password> "https://<your-batocera-name>.local/v1/api/systems"
```

Common API areas include systems, ROM lists, search, BIOS, themes, downloads, artwork/admin tools, logs, configs, and system information.

ROM downloads use the ROM `unique_id` from the API, not the display title. If you see `{"error": "not found"}`, check that the ROM file exists under the configured ROM root and that special characters in the URL are encoded.

### Local Mock Server

For non-Batocera development or a quick preview:

```bash
python3 scripts/run_mock_server.py
```

Then open:

```text
http://127.0.0.1:8080
```

Default mock login:

```text
admin / changeme
```

## Uninstall

```bash
wget -qO- https://github.com/Batocera-Fleet-Federation/batocera.drone/releases/latest/download/batocera_uninstall.sh | bash
```

The uninstaller automatically detects and removes either installation style:

- Batocera v43+ service installs in `/userdata/system/services/DRONE_SERVER` or `/userdata/system/services/DRONE_APP`.
- Legacy Batocera installs that start Drone from `/userdata/system/custom.sh`.

It stops Drone, removes its startup configuration, app files, logs, any legacy game-event hook, and the `drone-app` account. It does not delete ROMs, artwork, videos, manuals, or `gamelist.xml` files. Any permissions previously applied to those Batocera content files remain unchanged.
