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
https://<your-batocera-name>.local:8443
```

Example:

```text
https://batocera.local:8443
```

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
https://<your-batocera-name>.local:8443/v1/api
```

Interactive API documentation is here:

```text
https://<your-batocera-name>.local:8443/v1/api/swagger
```

The machine-readable OpenAPI file is here:

```text
https://<your-batocera-name>.local:8443/v1/api/openapi.json
```

## Overmind Integration

Batocera Drone is the local Batocera agent. Overmind owns the Overlord UI, Drone authorization tokens, action queue, swarm list, peer status, and per-Drone auto-sync policy.

Drone calls Overmind every 30 seconds by default. The alive payload includes the MAC-address `device_id`, discovered IPv4/IPv6 addresses, router/gateway IP when available, public IP when available, API port, protocol, local certificate metadata, and ROM systems. Overmind validates `Authorization: Bearer <drone_token>` before accepting alive, action status, metadata, logs, telemetry events, peer checks, or speed samples.

Overmind responds with the current swarm list. Drone stores that list locally, skips itself, and checks each other Drone with a short API health request. The admin Overmind page shows whether the last peer check passed or failed, when it was checked, and the failure reason if there was one.

Actions use a pull model because Overmind usually cannot connect inward to a Drone on a home network. The Overlord queues an action in Overmind, the Drone receives it during alive polling, performs the local work, then posts the result back to Overmind. Restart, shutdown, and update are simulated in fake/demo mode.

Drone also reports live events to Overmind. These include filesystem create/update/delete events for watched ROM/config/artwork/log paths, ROM/library changes, gameplay activity when available, and speed samples. The first speed sample is sent soon after startup, then every 5 minutes by default.

Drone action processing state is not stored in a persistent database. Processed action history is written to a separate rotating log file, configurable with `OVERMIND_ACTION_LOG_FILE` and `OVERMIND_ACTION_LOG_MAX_BYTES`. Normal Drone logs also rotate with `LOG_MAX_BYTES` and `LOG_BACKUP_COUNT`, and log APIs default to a reasonable recent tail.

Local fake mode is configured for `demo@example.com` with `OVERMIND_DRONE_TOKEN=demo-local-drone-token`, matching the local MAC-address Drone registered in Overmind fake data.

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

### API Example

```bash
curl -k -u <username>:<password> "https://<your-batocera-name>.local:8443/v1/api/systems"
```

Common API areas include systems, ROM lists, search, BIOS, themes, downloads, artwork/admin tools, logs, configs, and system information.

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

### Uninstall

```bash
userdel drone-app 2>/dev/null || true
rm -f /userdata/system/services/DRONE_SERVER
```

If the installer changed ownership on `/userdata/roms/*/{images,videos,manuals}/` or `gamelist.xml`, those ownership changes remain until you manually change them back.
