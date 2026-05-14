# Admin Features - Logs Viewer

## Overview

Added comprehensive administrative API endpoints and UI for retrieving and viewing system logs from Batocera, EmulationStation, and supported emulators.

## API Endpoints

### Get Logs Endpoint

**Request:**
```
GET /v1/api/admin/logs/{source}?lines=200
```

**Parameters:**
- `source` (path, required): Log source identifier (see supported sources below)
- `lines` (query, optional): Number of lines to return from the end of the log (default: 200, max: 10000)

**Response (200 OK):**
```json
{
  "source": "batocera",
  "path": "/userdata/system/logs/batocera.log",
  "lines": 200,
  "content": [
    "log line 1",
    "log line 2",
    "..."
  ]
}
```

**Error Responses:**
- `404`: Unknown log source or log file not found
- `500`: Failed to read log file

## Supported Log Sources

The following log sources are supported:

### System Logs
- `batocera` - Batocera system log
- `emulationstation` - EmulationStation log

### Emulator Logs
- `retroarch` - RetroArch
- `mame` - MAME
- `dolphin` - Dolphin (GameCube/Wii)
- `pcsx2` - PCSX2 (PlayStation 2)
- `rpcs3` - RPCS3 (PlayStation 3)
- `citra` - Citra (Nintendo 3DS)
- `yuzu` - Yuzu (Nintendo Switch)
- `cemu` - Cemu (Wii U)
- `xemu` - Xemu (Xbox)
- `xenia` - Xenia (Xbox 360)
- `ryujinx` - Ryujinx (Nintendo Switch)
- `melonDS` - melonDS (Nintendo DS)
- `flycast` - Flycast (Dreamcast)
- `ppsspp` - PPSSPP (PSP)
- `duckstation` - DuckStation (PlayStation)
- `mesen` - Mesen (NES/SNES)
- `snes9x` - Snes9x (SNES)
- `bsnes` - bsnes (SNES)
- `nestopia` - Nestopia (NES)
- `fceux` - FCEUX (NES)
- `mednafen` - Mednafen (Multi-system)
- `mgba` - mGBA (Game Boy Advance)
- `vbam` - VBA-M (Game Boy Advance)
- `scummvm` - ScummVM (Adventure games)
- `dosbox` - DOSBox (DOS games)
- `fs-uae` - FS-UAE (Amiga)
- `hatari` - Hatari (Atari ST)
- `vice` - VICE (Commodore 64/128)
- `fuse` - Fuse (ZX Spectrum)
- `oricutron` - Oricutron (Oric)
- `ti99sim` - TI99Sim (TI-99/4A)
- `simcoupe` - SimCoupe (Sam Coupe)
- `zesarux` - ZEsarUX (ZX Spectrum)
- `caprice32` - Caprice32 (Amstrad CPC)
- `cannonball` - Cannonball (OutRun)
- `openbor` - OpenBOR (Custom beat-em-ups)
- `solarus` - Solarus (2D action adventure)
- `easyrpg` - EasyRPG (RPG Maker 2000/2003)
- `supermodel` - Supermodel (Sega Model 3)
- `demul` - Demul (Sega Model 2)
- `nullDC` - nullDC (Dreamcast)
- `reicast` - Reicast (Dreamcast)
- `redream` - Redream (Dreamcast)
- `mupen64plus` - Mupen64Plus (Nintendo 64)
- `parallel-n64` - Parallel-N64 (Nintendo 64)
- `cxd4` - CXD4 (PlayStation)
- `play` - Play! (PlayStation 2)
- `ares` - Ares (Multi-system)
- `sameduck` - SameDuck (Game Boy)
- `gearboy` - Gearboy (Game Boy)
- `gearsystem` - Gearsystem (Master System/Game Gear)
- `freej2me` - FreeJ2ME (Java games)
- `bigpemu` - BigPEmu (Model 3)
- `model2` - Model 2 (Sega Model 2)
- `teknoParrot` - TeknoParrot (Arcade)
- `ruffle` - Ruffle (Flash)
- `lightspark` - Lightspark (Flash)
- `box86` - Box86 (x86 compatibility)
- `box64` - Box64 (x86_64 compatibility)
- `wine` - Wine (Windows)
- `proton` - Proton (Steam Play)

## UI Components

### Admin Menu
A new "Admin" menu button has been added to the main navigation bar, providing access to the admin panel.

### Admin Panel
The admin panel shows available administrative features:
- **Logs** - View system and emulator logs

### Logs Viewer
The logs viewer provides:
- **Sidebar Navigation**: List of all supported log sources with clickable links
- **Main Viewer**: Displays the selected log with:
  - Log source name and file path
  - Adjustable line count (1-10,000 lines)
  - Refresh button to reload logs
  - Monospace font for better readability
  - Scrollable container for long logs

## Usage Examples

### API Calls

Get last 200 lines of Batocera system log:
```bash
curl -u admin:password https://your-server/v1/api/admin/logs/batocera
```

Get last 500 lines of RetroArch log:
```bash
curl -u admin:password https://your-server/v1/api/admin/logs/retroarch?lines=500
```

Get last 100 lines of PCSX2 log:
```bash
curl -u admin:password https://your-server/v1/api/admin/logs/pcsx2?lines=100
```

### Web UI
1. Click the **Admin** button in the menu bar
2. Click **View Logs**
3. Select a log source from the left sidebar
4. Adjust the number of lines if desired
5. Click **Refresh** to reload the logs

## Implementation Details

### Backend (`drone_api.py`)
- Added `_handle_admin_logs(log_source, lines)` method to `RomRequestHandler` class
- Maps log source names to file paths in `/userdata/system/logs/`
- Uses `tail` command to efficiently retrieve last N lines
- Returns JSON response with log metadata and content
- Proper error handling for missing sources and files

### Routing (`api_routes.py`)
- Added route handler for `/admin/logs/{source}` path
- Parses `lines` query parameter with default of 200 lines
- Validates numeric input

### API Documentation
- Updated OpenAPI spec with new endpoint definition
- Includes parameter descriptions and response schemas

### Frontend (`index.html`)
- Added Admin menu button to navigation
- Created `renderAdminPage()` for the admin panel UI
- Created `renderLogsPage()` for the logs viewer UI
- Implemented `loadLog(source)` function to fetch and display logs
- Implemented `refreshCurrentLog()` to reload current log
- Full list of 60+ emulator sources with direct access buttons

## Security Notes

- Admin endpoints require HTTP Basic Authentication
- No new authentication mechanisms added; uses existing auth
- Log files are read-only; no sensitive operations exposed
- Access is restricted to authenticated users

## Future Enhancements

Possible future additions:
- Log filtering/search within the viewer
- Export logs to file
- Real-time log streaming
- Log rotation management
- Performance metrics display
- Error log analysis
