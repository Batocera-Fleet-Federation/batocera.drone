"""Receiver-compatible movie delivery for Chromecast and AirPlay.

The default Google Cast receiver cannot play Matroska and many codecs commonly
found in a local movie/TV library.  Direct-compatible MP4/WebM files keep using
the small Range-aware cast listener.  Everything else is converted on demand by
Batocera's bundled FFmpeg into an HLS stream served by that same token-gated
listener.  No third-party Python package is required.

The cache is deliberately ephemeral.  FFmpeg is paced at approximately playback
speed, a janitor stops abandoned jobs, and completed/abandoned session folders
are removed after they have gone idle.  A cast token is part of the HLS path, so
relative segment URLs inherit the same single-movie authorization boundary as
the original direct stream URL.
"""

from __future__ import annotations

import atexit
import json
import os
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional


HLS_CONTENT_TYPE = "application/x-mpegurl"
HLS_SEGMENT_CONTENT_TYPE = "video/mp2t"

_DIRECT_MP4_SUFFIXES = {".mp4", ".m4v", ".mov", ".qt"}
_DIRECT_WEBM_SUFFIXES = {".webm"}
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{20,128}$")
_HLS_ASSET_RE = re.compile(r"^(?:index\.m3u8|media\.m3u8|segment-[0-9]{6}\.ts)$")

# A receiver normally asks for a new segment every few seconds.  One hour is
# intentionally generous for a paused movie while still bounding an abandoned
# encoder. Completed sessions can be discarded sooner once nothing is reading
# them; their final segments have already been delivered by then.
_RUNNING_IDLE_SECONDS = 60 * 60
_FINISHED_IDLE_SECONDS = 30 * 60
_STALE_DIRECTORY_SECONDS = 12 * 60 * 60
_JANITOR_INTERVAL_SECONDS = 60


@dataclass(frozen=True)
class MediaProbe:
    video_codec: str = ""
    audio_codec: str = ""
    width: int = 0
    height: int = 0
    level: int = 0


@dataclass
class _TranscodeSession:
    token: str
    entry_key: str
    directory: Path
    process: subprocess.Popen
    last_access: float


_LOCK = threading.Lock()
_SESSIONS: dict[str, _TranscodeSession] = {}
_JANITOR_STARTED = False
_STALE_DIRECTORIES_CLEANED = False


def _find_executable(configured: str) -> Optional[str]:
    value = str(configured or "").strip()
    if not value:
        return None
    if os.path.isabs(value):
        return value if os.path.isfile(value) and os.access(value, os.X_OK) else None
    return shutil.which(value)


def probe_media(settings: Any, path: Path) -> Optional[MediaProbe]:
    """Return just enough stream information to choose direct vs HLS.

    ffprobe is an optimization, not a hard dependency: if it is unavailable or
    cannot read a file, known web-native suffixes retain the old direct path and
    every other suffix is handed to FFmpeg's compatibility path.
    """

    ffprobe = _find_executable(settings.cast_ffprobe_bin)
    if not ffprobe:
        return None
    command = [
        ffprobe,
        "-v", "error",
        "-show_entries", "stream=codec_type,codec_name,width,height,level",
        "-of", "json",
        str(path),
    ]
    try:
        completed = subprocess.run(
            command,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=10,
        )
        streams = json.loads(completed.stdout or "{}").get("streams") or []
    except (OSError, subprocess.SubprocessError, ValueError, TypeError):
        return None

    video = next((item for item in streams if item.get("codec_type") == "video"), {})
    audio = next((item for item in streams if item.get("codec_type") == "audio"), {})

    def _integer(value: Any) -> int:
        try:
            return max(0, int(value or 0))
        except (TypeError, ValueError):
            return 0

    return MediaProbe(
        video_codec=str(video.get("codec_name") or "").lower(),
        audio_codec=str(audio.get("codec_name") or "").lower(),
        width=_integer(video.get("width")),
        height=_integer(video.get("height")),
        level=_integer(video.get("level")),
    )


def is_direct_compatible(path: Path, probe: Optional[MediaProbe]) -> bool:
    """Whether Google's default receiver can consume the file as-is."""

    suffix = path.suffix.lower()
    if probe is None:
        return suffix in (_DIRECT_MP4_SUFFIXES | _DIRECT_WEBM_SUFFIXES)
    if suffix in _DIRECT_MP4_SUFFIXES:
        return _can_copy_h264(probe) and probe.audio_codec in ("", "aac", "mp3")
    if suffix in _DIRECT_WEBM_SUFFIXES:
        # VP8 is shared across the old and new receiver generations; VP9 is
        # not, so normalize it when the selected receiver model is unknown.
        return probe.video_codec == "vp8" and probe.audio_codec in ("", "opus", "vorbis")
    return False


def _can_copy_h264(probe: Optional[MediaProbe]) -> bool:
    # Level 4.1 is the broad compatibility ceiling shared by first/second-gen
    # Chromecast hardware. Level 4.2 commonly means 1080p60 and is normalized
    # to 30fps by the transcode path for those older receivers.
    return bool(
        probe
        and probe.video_codec == "h264"
        and (not probe.width or probe.width <= 1920)
        and (not probe.height or probe.height <= 1080)
        and (not probe.level or probe.level <= 41)
    )


def build_ffmpeg_command(ffmpeg: str, source: Path, destination: Path, probe: Optional[MediaProbe]) -> list[str]:
    """Build the real-time HLS compatibility command (kept pure for tests)."""

    copy_video = _can_copy_h264(probe)
    copy_audio = bool(probe and probe.audio_codec == "aac")
    playlist = destination / "media.m3u8"
    segments = destination / "segment-%06d.ts"
    command = [
        ffmpeg,
        "-hide_banner", "-loglevel", "warning", "-nostdin", "-y",
        # Do not race through an entire multi-gigabyte movie into the cache.
        # HLS is generated at playback pace and the janitor stops abandoned jobs.
        "-re", "-i", str(source),
        "-map", "0:v:0", "-map", "0:a:0?", "-dn", "-sn",
    ]
    if copy_video:
        command += ["-c:v", "copy"]
    else:
        command += [
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "22",
            "-pix_fmt", "yuv420p",
            "-vf", "scale=w='min(1920,iw)':h=-2:force_original_aspect_ratio=decrease,fps=30",
            "-profile:v", "high", "-level:v", "4.1",
            "-force_key_frames", "expr:gte(t,n_forced*4)",
        ]
    if copy_audio:
        command += ["-c:a", "copy"]
    else:
        command += ["-c:a", "aac", "-b:a", "192k", "-ac", "2", "-ar", "48000"]
    command += [
        "-max_muxing_queue_size", "2048",
        "-f", "hls", "-hls_time", "4", "-hls_list_size", "0",
        "-hls_flags", "independent_segments+temp_file",
        "-hls_segment_filename", str(segments),
        str(playlist),
    ]
    return command


def _session_directory(settings: Any, token: str) -> Path:
    if not _TOKEN_RE.fullmatch(str(token or "")):
        raise ValueError("invalid cast token")
    root = Path(settings.cast_cache_root).resolve()
    return root / token


def _remove_directory(path: Path) -> None:
    try:
        shutil.rmtree(path)
    except FileNotFoundError:
        pass
    except OSError:
        # Cache cleanup must never take down the movie API or cast listener.
        pass


def _stop_session(session: _TranscodeSession) -> None:
    process = session.process
    if process.poll() is None:
        try:
            process.terminate()
            process.wait(timeout=3)
        except (OSError, subprocess.TimeoutExpired):
            try:
                process.kill()
            except OSError:
                pass
    _remove_directory(session.directory)


def _cleanup_stale_directories(settings: Any) -> None:
    global _STALE_DIRECTORIES_CLEANED
    with _LOCK:
        if _STALE_DIRECTORIES_CLEANED:
            return
        _STALE_DIRECTORIES_CLEANED = True
    root = Path(settings.cast_cache_root)
    try:
        children = list(root.iterdir())
    except OSError:
        return
    cutoff = time.time() - _STALE_DIRECTORY_SECONDS
    for child in children:
        try:
            if child.is_dir() and child.stat().st_mtime < cutoff:
                _remove_directory(child)
        except OSError:
            continue


def cleanup_idle_sessions() -> None:
    now = time.monotonic()
    expired: list[_TranscodeSession] = []
    with _LOCK:
        for token, session in list(_SESSIONS.items()):
            idle = now - session.last_access
            finished = session.process.poll() is not None
            if (finished and idle >= _FINISHED_IDLE_SECONDS) or idle >= _RUNNING_IDLE_SECONDS:
                expired.append(_SESSIONS.pop(token))
    for session in expired:
        _stop_session(session)


def _janitor() -> None:
    while True:
        time.sleep(_JANITOR_INTERVAL_SECONDS)
        cleanup_idle_sessions()


def _ensure_janitor_started() -> None:
    global _JANITOR_STARTED
    with _LOCK:
        if _JANITOR_STARTED:
            return
        _JANITOR_STARTED = True
    threading.Thread(target=_janitor, name="drone-cast-cache-janitor", daemon=True).start()


def _failure_detail(log_path: Path) -> str:
    try:
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return "FFmpeg exited before producing a stream"
    detail = " ".join(line.strip() for line in lines[-3:] if line.strip())
    return detail[-500:] or "FFmpeg exited before producing a stream"


def prepare(settings: Any, source: Path, entry_key: str, token: str) -> dict:
    """Prepare and describe the delivery URL path for one cast request."""

    probe = probe_media(settings, source)
    if is_direct_compatible(source, probe):
        return {
            "delivery": "direct",
            "url_path": f"/public/movies/{entry_key}/cast-stream?token={token}",
            "content_type": None,
            "transcoded": False,
        }
    if not settings.cast_transcode_enabled:
        raise ValueError(
            "this movie needs cast compatibility streaming; enable DRONE_CAST_TRANSCODE_ENABLED or use Download"
        )
    ffmpeg = _find_executable(settings.cast_ffmpeg_bin)
    if not ffmpeg:
        raise ValueError(
            "this movie format needs FFmpeg for casting, but FFmpeg was not found on this Drone"
        )

    _cleanup_stale_directories(settings)
    _ensure_janitor_started()
    directory = _session_directory(settings, token)
    directory.mkdir(parents=True, exist_ok=False)
    log_path = directory / "ffmpeg.log"
    command = build_ffmpeg_command(ffmpeg, source, directory, probe)
    try:
        with log_path.open("wb") as log_handle:
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=log_handle,
                start_new_session=True,
            )
    except OSError as error:
        _remove_directory(directory)
        raise ValueError(f"could not start FFmpeg compatibility streaming: {error}") from error

    session = _TranscodeSession(token, entry_key, directory, process, time.monotonic())
    with _LOCK:
        _SESSIONS[token] = session

    media_playlist = directory / "media.m3u8"
    deadline = time.monotonic() + settings.cast_hls_start_timeout_seconds
    while time.monotonic() < deadline:
        try:
            if media_playlist.is_file() and media_playlist.stat().st_size > 0:
                break
        except OSError:
            pass
        if process.poll() is not None:
            detail = _failure_detail(log_path)
            with _LOCK:
                _SESSIONS.pop(token, None)
            _remove_directory(directory)
            raise ValueError(f"FFmpeg could not prepare this movie for casting: {detail}")
        time.sleep(0.1)
    else:
        with _LOCK:
            _SESSIONS.pop(token, None)
        _stop_session(session)
        raise ValueError("timed out while preparing this movie for casting")

    # Give the receiver a standards-shaped master playlist with explicit
    # codecs. FFmpeg's growing media playlist is referenced from it and is
    # refreshed by the receiver every target duration while encoding runs.
    # Both copy and transcode paths are constrained to H.264 + AAC here.
    (directory / "index.m3u8").write_text(
        "#EXTM3U\n"
        "#EXT-X-VERSION:3\n"
        '#EXT-X-STREAM-INF:BANDWIDTH=10000000,CODECS="avc1.640029,mp4a.40.2"\n'
        "media.m3u8\n",
        encoding="utf-8",
    )

    return {
        "delivery": "hls",
        "url_path": f"/public/movies/{entry_key}/cast-hls/{token}/index.m3u8",
        "content_type": HLS_CONTENT_TYPE,
        "transcoded": not (_can_copy_h264(probe) and probe and probe.audio_codec == "aac"),
    }


def resolve_hls_asset(settings: Any, entry_key: str, token: str, asset_name: str) -> tuple[Path, str]:
    """Resolve one playlist/segment without allowing arbitrary cache reads."""

    if not _HLS_ASSET_RE.fullmatch(str(asset_name or "")):
        raise FileNotFoundError()
    directory = _session_directory(settings, token)
    with _LOCK:
        session = _SESSIONS.get(token)
        if session is not None:
            if session.entry_key != entry_key:
                raise FileNotFoundError()
            session.last_access = time.monotonic()
    target = (directory / asset_name).resolve()
    if target.parent != directory.resolve() or not target.is_file():
        raise FileNotFoundError()
    content_type = HLS_CONTENT_TYPE if target.suffix == ".m3u8" else HLS_SEGMENT_CONTENT_TYPE
    return target, content_type


def cancel(token: str) -> None:
    """Stop and discard a compatibility stream after token preparation fails."""

    with _LOCK:
        session = _SESSIONS.pop(token, None)
    if session is not None:
        _stop_session(session)


def cancel_all() -> None:
    """Stop child encoders during a normal Drone service shutdown/restart."""

    with _LOCK:
        sessions = list(_SESSIONS.values())
        _SESSIONS.clear()
    for session in sessions:
        _stop_session(session)


atexit.register(cancel_all)
