from pathlib import Path


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def seed_mock_userdata(userdata_root: Path) -> None:
    userdata_root.mkdir(parents=True, exist_ok=True)

    # ROMs + artwork
    roms_root = userdata_root / "roms"
    _write_bytes(roms_root / "snes" / "Chrono Trigger (USA).zip", b"FAKE-SNES-ROM-1")
    _write_bytes(roms_root / "snes" / "Super Mario World (USA).zip", b"FAKE-SNES-ROM-2")
    _write_bytes(roms_root / "snes" / "images" / "Chrono Trigger (USA)-image.png", b"\x89PNG\r\n")
    _write_bytes(roms_root / "snes" / "images" / "Super Mario World (USA)-image.png", b"\x89PNG\r\n")
    _write_bytes(roms_root / "gba" / "Metroid Fusion (USA).zip", b"FAKE-GBA-ROM-1")
    _write_bytes(roms_root / "gba" / "Mario Kart Super Circuit (USA).zip", b"FAKE-GBA-ROM-2")
    _write_text(roms_root / "snes" / "gamelist.xml", "<gameList><game><name>Chrono Trigger</name></game></gameList>\n")
    _write_text(roms_root / "gba" / "gamelist.xml", "<gameList><game><name>Metroid Fusion</name></game></gameList>\n")

    # BIOS
    bios_root = userdata_root / "bios"
    _write_bytes(bios_root / "scph1001.bin", b"BIOS-DATA-PSX")
    _write_bytes(bios_root / "gba_bios.bin", b"BIOS-DATA-GBA")

    # Logs used by admin endpoint
    logs_root = userdata_root / "system" / "logs"
    _write_text(logs_root / "es_launch_stdout.log", "INFO launch emulator=snes\nINFO rom=Chrono Trigger\n")
    _write_text(logs_root / "es_launch_stderr.log", "WARN no joystick hotplug event\n")

    # Core system config files
    _write_text(
        userdata_root / "system" / "batocera.conf",
        "system.language=en_US\nsnes.emulator=libretro\n",
    )
    _write_text(
        userdata_root / "system" / "configs" / "emulationstation" / "es_settings.cfg",
        "<bool name=\"ScrapeRatings\" value=\"true\" />\n<string name=\"ThemeSet\" value=\"carbon\" />\n",
    )
    _write_text(
        userdata_root / "system" / "configs" / "emulationstation" / "es_input.cfg",
        "<inputConfig type=\"joystick\" deviceName=\"Mock Gamepad\" />\n",
    )
    _write_text(
        userdata_root / "system" / "configs" / "retroarch" / "retroarchcustom.cfg",
        "video_driver = \"gl\"\nmenu_driver = \"ozone\"\n",
    )

    # Emulator config files commonly used for debugging.
    _write_text(userdata_root / "system" / "configs" / "PCSX2" / "inis" / "PCSX2_ui.ini", "UIFullscreen=true\n")
    _write_text(userdata_root / "system" / "configs" / "duckstation" / "settings.ini", "[Main]\nRenderer=Vulkan\n")
    _write_text(userdata_root / "system" / "configs" / "citra-emu" / "qt-config.ini", "[UI]\nfirstStart=false\n")
    _write_text(userdata_root / "system" / "configs" / "yuzu" / "qt-config.ini", "[UI]\nfullscreen=true\n")
    _write_text(userdata_root / "system" / "configs" / "Ryujinx" / "Config.json", "{\n  \"enable_discord_integration\": false\n}\n")
    _write_text(userdata_root / "system" / "configs" / "xenia" / "xenia.config.toml", "gpu = \"any\"\n")
    _write_text(userdata_root / "system" / "configs" / "dosbox" / "dosbox.conf", "[dosbox]\nmemsize=64\n")
    _write_text(userdata_root / "system" / "configs" / "scummvm" / "scummvm.ini", "[scummvm]\nfullscreen=true\n")
    _write_text(userdata_root / "system" / "configs" / "snes9x" / "snes9x.conf", "Fullscreen:TRUE\n")
    _write_text(userdata_root / "system" / "configs" / "bsnes" / "settings.bml", "video/driver: gl\n")
    _write_text(userdata_root / "system" / "configs" / "fceux" / "fceux.cfg", "PAL=0\n")
    _write_text(userdata_root / "system" / "configs" / "mednafen" / "mednafen.cfg", "video.driver opengl\n")
    _write_text(userdata_root / "system" / "configs" / "mgba" / "config.ini", "[ports.qt]\nshowFps=false\n")
    _write_text(userdata_root / "system" / "configs" / "wine" / "user.reg", "REGEDIT4\n")
    _write_text(userdata_root / "system" / "configs" / "shadps4" / "config.toml", "renderer = \"vulkan\"\n")

    # Theme assets
    theme_root = userdata_root / "themes" / "carbon"
    _write_text(theme_root / "theme.xml", "<theme></theme>\n")
    _write_bytes(theme_root / "_inc" / "logo.png", b"\x89PNG\r\n")
    _write_bytes(theme_root / "_inc" / "background.jpg", b"\xff\xd8\xff")
