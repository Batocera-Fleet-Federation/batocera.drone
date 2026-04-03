# ROMS API

Use this API to browse systems, list ROMs, and download files.

## Base URL And Auth

- Base URL example: `https://192.168.0.205:8000`
- Authentication: HTTP Basic Auth (`<username>` / `<password>`)
- TLS uses a self-signed certificate
- Use `-k` with `curl` to skip certificate verification
- Most endpoints require auth
- Public image route does not require auth

## Common API Calls

### List systems

```bash
curl -k -u <username>:<password> "https://192.168.0.205:8000/systems"
```

### List ROMs for a system

```bash
curl -k -u <username>:<password> "https://192.168.0.205:8000/systems/snes"
```

### Download a ROM by `unique_id`

1. Get a `unique_id` from `/systems/snes`
2. Download:

```bash
curl -k -u <username>:<password> -OJ "https://192.168.0.205:8000/systems/snes/<unique_id>"
```

### List images/videos for a system

```bash
curl -k -u <username>:<password> "https://192.168.0.205:8000/systems/snes/images"
curl -k -u <username>:<password> "https://192.168.0.205:8000/systems/snes/videos"
```

### Image routes

- Private image by unique id or file reference (auth required):  
  `/systems/{system}/images/{image_ref}`
- Public image (no auth):  
  `/public/systems/{system}/images/{image_file}`

## View The UI

Open the root URL in your browser:

```text
https://192.168.0.205:8000/
```

Sign in with the same `<username>` and `<password>` (browser Basic Auth prompt).  
The UI lets you:

- View available systems
- Open a system to see ROM cards and artwork
- Download ROMs from the **Download** button
