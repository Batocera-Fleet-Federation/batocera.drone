---
name: drone-rgs-api-token
description: Use this when finding, inspecting, rotating, or replacing the RGS (Pixel Nostalgia / rgs-retro) API token on a Batocera device — the API_TOKEN.txt file read by the RGS download service under /userdata/system/rgs/download_service, its backups, permissions, and the service restart/host caveats. Also use when a user says "API_KEY.txt" for RGS, since no file by that name exists.
---

# RGS API token on a Batocera device

The RGS download service is **not Drone code**. It is a third-party service installed
under `/userdata/system/rgs/` on the Batocera box. This skill covers only the
credential file it reads. For general live-device access (SSH, logs, the Drone state DB)
see `drone-live-debugging`.

## Where the token lives

- Live token: `/userdata/system/rgs/download_service/API_TOKEN.txt`
  (one 64-char hex token plus a trailing newline, 65 bytes, mode `644`, owner `root`).
- Backups in the same folder: `API_TOKEN.txt.bak` (mode `600`) and any
  `API_TOKEN.txt.pre-update-<timestamp>.bak` created by a previous rotation.
- The startup check that reads it is in `/userdata/system/services/rgs_download`
  (`$SERVICE_DIR/API_TOKEN.txt`, warns if missing).
- The service's default backend is `API_URL=https://rgs-retro.ddns.net` (set in the
  same script). A token issued by a different platform will not authenticate there.

**Name gotcha:** users often say `API_KEY.txt`. No file with that name exists on the
device (checked with `find /userdata -iname "*api*key*"`). The file is `API_TOKEN.txt`.
Confirm with the user before rotating if they mean a different file.

## Credentials

SSH access follows `drone-live-debugging` and `.github/.credentials`. Do not write the
password into this skill, into a script, or into git. Use:

```bash
sshpass -p "$BATOCERA_PASSWORD" ssh -o StrictHostKeyChecking=no root@batocera.local '<cmd>'
```

## Rotating the token (safe procedure)

1. Confirm the target device and the file name (see above).
2. Check current permissions and size; do not print the token itself:
   `stat -c "%a %U:%G %s" API_TOKEN.txt`
3. Back up with a timestamp, preserving mode and ownership:
   `cp -p API_TOKEN.txt API_TOKEN.txt.pre-update-$(date +%Y%m%dT%H%M%S).bak`
4. Write the new token with a trailing newline, keeping the existing mode:
   `printf "%s\n" "<new-token>" > API_TOKEN.txt`
5. Verify: size is 65 bytes, mode is still `644`, and `grep -c "<new-token>"` returns 1.
   Do not echo the token back in logs or chat.
6. Do not restart the service unless asked. If the service caches the token at startup,
   a restart is needed before the new value takes effect. Say so in the report.

## Gotchas

- Never `cat` the token into chat or commits. Verify by `grep -c` or length only.
- `API_TOKEN.txt` is world-readable (`644`) today. Don't widen that, and don't copy the
  token into the repo, `.github/.credentials`, or any other tracked file.
- `*.local` is mDNS. Confirm the device with `hostname; uname -a` before writing.
  A different device answering the same name is a real possibility.
- A token that works for `rgs-retro.ddns.net` may not be valid for another platform
  (for example `pixelnostalgia.github.io`). Check which backend the key belongs to
  before assuming the service will accept it.
