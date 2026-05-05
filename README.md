# ROMS API

Use this API to browse systems, list ROMs, and download files.

## Run Locally

From the repo root:

```bash
export ROM_API_USERNAME="admin"
export ROM_API_PASSWORD="changeme"
export HTTPS_PORT=8443
export ROMS_ROOT="./local-data/roms"
export BIOS_ROOT="./local-data/bios"
export TLS_SELF_SIGNED_DIR="./local-data/certs"
export LOG_DIR="./local-data/logs"
export LOG_MAX_BYTES=5242880
export LOG_BACKUP_COUNT=5
mkdir -p "$ROMS_ROOT" "$BIOS_ROOT" "$TLS_SELF_SIGNED_DIR" "$LOG_DIR"
python3 app/main.py
```

Then open `https://127.0.0.1:8443` and sign in with the same username/password.
Stdout/stderr are written to rolling files in `LOG_DIR` as `stdout.log` and `stderr.log`.

## Base URL And Auth

- Base URL example: `https://72.176.228.250`
- Authentication: HTTP Basic Auth (`<username>` / `<password>`)
- TLS uses a self-signed certificate
- Use `-k` with `curl` to skip certificate verification
- Most endpoints require auth
- Public image route does not require auth

## Common API Calls

### List systems

```bash
curl -k -u <username>:<password> "https://72.176.228.250/systems"
```

### List ROMs for a system

```bash
curl -k -u <username>:<password> "https://72.176.228.250/systems/snes"
```

### Download a ROM by `unique_id`

1. Get a `unique_id` from `/systems/snes`
2. Download:

```bash
curl -k -u <username>:<password> -OJ "https://72.176.228.250/systems/snes/<unique_id>"
```

### List images/videos for a system

```bash
curl -k -u <username>:<password> "https://72.176.228.250/systems/snes/images"
curl -k -u <username>:<password> "https://72.176.228.250/systems/snes/videos"
```

### List BIOS folders/files recursively

```bash
curl -k -u <username>:<password> "https://72.176.228.250/bios"
```

Each BIOS entry includes:

- `entry_type` (`folder` or `file`)
- `name`
- `path` (relative path under `/userdata/bios`)
- `unique_id`
- `byte_count`

### Download a BIOS file by `unique_id`

```bash
curl -k -u <username>:<password> -OJ "https://72.176.228.250/bios/<unique_id>"
```

### Image routes

- Private image by unique id or file reference (auth required):  
  `/systems/{system}/images/{image_ref}`
- Public image (no auth):  
  `/public/systems/{system}/images/{image_file}`

## View The UI

Open the root URL in your browser:

```text
https://72.176.228.250/
```

Sign in with the same `<username>` and `<password>` (browser Basic Auth prompt).  
The UI lets you:

- View available systems
- Open a system to see ROM cards and artwork
- Download ROMs from the **Download** button

## Download All ROMs Script

Use [`download_all_roms.sh`](/Users/Jerrod/Documents/gitlab/roms-api/scripts/download_all_roms.sh) to iterate through all systems and download every ROM.

- The script calls `/systems` then `/systems/{system}`
- It creates a folder per system under `--output-dir`
- Each ROM is saved into its system folder
- Optionally target a single system with `--system`

Example:

```bash
./scripts/download_all_roms.sh \
  --base-url "https://72.176.228.250" \
  --username "<username>" \
  --password "<password>" \
  --output-dir "./rom_downloads"
```

Single-system example:

```bash
./scripts/download_all_roms.sh \
  --base-url "https://72.176.228.250" \
  --username "<username>" \
  --password "<password>" \
  --system "snes" \
  --output-dir "./rom_downloads"
```

Notes:

- TLS verification is off by default for self-signed certs
- Add `--verify-tls` if you want certificate validation
- Existing files are skipped by default; add `--overwrite` to replace
- Requires `curl` and `jq`

### PowerShell Version

Use [`download_all_roms.ps1`](/Users/Jerrod/Documents/gitlab/roms-api/scripts/download_all_roms.ps1) for a native PowerShell workflow:

```powershell
.\scripts\download_all_roms.ps1 `
  -BaseUrl "https://72.176.228.250" `
  -Username "<username>" `
  -Password "<password>" `
  -OutputDir ".\rom_downloads"
```

Single-system example:

```powershell
.\scripts\download_all_roms.ps1 `
  -BaseUrl "https://72.176.228.250" `
  -Username "<username>" `
  -Password "<password>" `
  -System "snes" `
  -OutputDir ".\rom_downloads"
```

Notes:

- TLS verification is off by default for self-signed certs
- Add `-VerifyTls` to enforce certificate validation
- Existing files are skipped by default; add `-Overwrite` to replace

## Bootstrap Script (Download And Run)

Download [`download_and_run_rom_api.sh`](/Users/Jerrod/Documents/gitlab/roms-api/scripts/download_and_run_rom_api.sh), then run:

```bash
ROM_API_BASE_URL="<raw-base-url-containing-app-folder>" ./scripts/download_and_run_rom_api.sh
```

One-shot with `curl`:

```bash
curl -fsSL "<raw-url-to-scripts/download_and_run_rom_api.sh>" | \
ROM_API_BASE_URL="<raw-base-url-containing-app-folder>" bash
```

One-shot with `wget`:

```bash
wget -qO- "<raw-url-to-scripts/download_and_run_rom_api.sh>" | \
ROM_API_BASE_URL="<raw-base-url-containing-app-folder>" bash
```

The script downloads `app/rom_api.py` and `app/templates/index.html`, prompts for credentials if not already set, and starts the API with:

```bash
ROM_API_USERNAME=<your username> ROM_API_PASSWORD='<your password>' HTTPS_PORT=8443 ROMS_ROOT=~/.rom-api/local-data/roms BIOS_ROOT=~/.rom-api/local-data/bios TLS_SELF_SIGNED_DIR=~/.rom-api/local-data/certs LOG_DIR=~/.rom-api/logs LOG_MAX_BYTES=5242880 LOG_BACKUP_COUNT=5 python3 app/main.py
```
