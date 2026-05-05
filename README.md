# ROMS API

HTTPS Basic-Auth API + web UI for browsing Batocera ROMs/BIOS/theme assets.

## Run

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
export ALLOW_CONTENT_DOWNLOAD=true
mkdir -p "$ROMS_ROOT" "$BIOS_ROOT" "$TLS_SELF_SIGNED_DIR" "$LOG_DIR"
python3 app/main.py
```

Open: `https://127.0.0.1:8443`

## Auth + TLS

- Auth: HTTP Basic Auth
- TLS: self-signed by default
- `curl` examples use `-k` for self-signed certs
- Public route without auth: `/public/systems/{system}/images/{image_file}`

## Download Toggle

- `ALLOW_CONTENT_DOWNLOAD=true` (default): enable ROM/BIOS downloads
- `ALLOW_CONTENT_DOWNLOAD=false`: disable download links/actions and block download routes
- Backward-compatible aliases: `DOWNLOAD`, `DOWNLOADS_ENABLED`

## Logging

- Rolling stdout/stderr logs in `LOG_DIR`
- Files: `stdout.log`, `stderr.log`
- Size/count controlled by `LOG_MAX_BYTES`, `LOG_BACKUP_COUNT`

## Key Endpoints

- `GET /systems` list systems
- `GET /systems/{system}` list ROMs for system
- `GET /systems/{system}/{unique_id}` download ROM (legacy)
- `GET /systems/{system}/roms/{unique_id}` download ROM
- `GET /search?q=<text>[&system=<name>]` search ROMs
- `GET /bios?limit=100&offset=0[&q=<text>][&systems=a,b]` paged BIOS list
- `GET /bios/{unique_id}` download BIOS
- `GET /theme/meta` active theme metadata
- `GET /theme/system/{system}` system theme metadata
- `GET /theme/backgrounds` theme background candidates
- `GET /theme/logos` theme logo candidates
- `GET /theme/images?limit=100&offset=0[&q=<text>][&systems=a,b]` paged theme assets
- `GET /downloads` HTML sitemap of downloadable ROM links
- `GET /swagger` Swagger UI
- `GET /openapi.json` OpenAPI spec

## API Notes

- ROM names are returned without file extension for file-based ROMs
- API still returns `byte_count` in bytes; UI displays MB
- BIOS list supports search across `name`, `path`, `system`, and `md5`
- Theme/BIOS paging + filtering is server-side (search runs across full dataset)

## Quick cURL

```bash
curl -k -u <u>:<p> "https://<host>/systems"
curl -k -u <u>:<p> "https://<host>/systems/snes"
curl -k -u <u>:<p> "https://<host>/search?q=zelda"
curl -k -u <u>:<p> "https://<host>/bios?limit=100&offset=0&q=firmware&systems=ps2,ps3"
curl -k -u <u>:<p> "https://<host>/theme/images?limit=100&offset=0&q=logo&systems=snes,ps2"
curl -k -u <u>:<p> "https://<host>/swagger"
```

## Scripts

- `scripts/download_all_roms.sh`
- `scripts/download_all_roms.ps1`
- `scripts/download_and_run_rom_api.sh`
- `scripts/deploy_to_target.sh`
