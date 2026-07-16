"""Audio normalization seam.

The ``convert`` stage turns whatever was uploaded into the canonical STT
input (16 kHz mono PCM WAV) by calling the ``STAPEL_RECORDINGS["NORMALIZER"]``
callable: ``(src_path, dst_path) -> float | None`` (duration seconds, or
None when unknown). Raise :class:`NormalizeFatal` for unfixable input
(no audio stream, unreadable) so the driver DLQs instead of retrying.

Two implementations ship:

- :func:`ffmpeg_normalize` (default) — shells out to ffmpeg/ffprobe.
- :func:`passthrough_normalize` — copies the file unchanged; for
  environments without ffmpeg or when the upload is already normalized.
  Also the natural choice in tests.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from typing import Optional

FFMPEG_BIN = os.environ.get("FFMPEG_BIN", "ffmpeg")  # noqa: CFG001
FFPROBE_BIN = os.environ.get("FFPROBE_BIN", "ffprobe")  # noqa: CFG001
TARGET_SAMPLE_RATE = 16000
TARGET_CHANNELS = 1
SUBPROCESS_TIMEOUT_S = int(os.environ.get("FFMPEG_TIMEOUT_S", "1800"))  # noqa: CFG001


class NormalizeFatal(Exception):
    """Input can't be normalized — bad file, no audio stream, ffmpeg crash."""

    def __init__(self, reason: str, detail: Optional[str] = None):
        super().__init__(f"{reason}: {detail or ''}")
        self.reason = reason
        self.detail = detail


def passthrough_normalize(src_path: str, dst_path: str) -> Optional[float]:
    """Copy ``src`` to ``dst`` unchanged. Duration is unknown (None)."""
    shutil.copyfile(src_path, dst_path)
    return None


def ffmpeg_normalize(src_path: str, dst_path: str) -> Optional[float]:
    """Probe + transcode to 16 kHz mono PCM WAV. Returns duration seconds."""
    has_audio, duration = _probe_audio(src_path)
    if not has_audio:
        raise NormalizeFatal("no_audio_stream", "input has no decodable audio track")
    _run_ffmpeg(src_path, dst_path)
    return duration


def _run_ffmpeg(src: str, dst: str) -> None:
    cmd = [
        FFMPEG_BIN, "-hide_banner", "-loglevel", "error", "-y",
        "-i", src, "-vn",
        "-ac", str(TARGET_CHANNELS), "-ar", str(TARGET_SAMPLE_RATE),
        "-c:a", "pcm_s16le", "-f", "wav", dst,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=SUBPROCESS_TIMEOUT_S)
    except FileNotFoundError as exc:
        raise NormalizeFatal("ffmpeg_missing", str(exc)) from exc
    except subprocess.TimeoutExpired as exc:
        raise NormalizeFatal("ffmpeg_timeout", f"exceeded {SUBPROCESS_TIMEOUT_S}s") from exc
    if proc.returncode != 0:
        stderr = (proc.stderr or b"").decode(errors="replace")[:500]
        raise NormalizeFatal("ffmpeg_failed", f"rc={proc.returncode}: {stderr}")


def _probe_audio(path: str) -> tuple[bool, Optional[float]]:
    cmd = [
        FFPROBE_BIN, "-v", "error", "-print_format", "json",
        "-show_format", "-show_streams", "-select_streams", "a", path,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=30)
    except FileNotFoundError as exc:
        raise NormalizeFatal("ffprobe_missing", str(exc)) from exc
    except subprocess.TimeoutExpired as exc:
        raise NormalizeFatal("ffprobe_timeout", "exceeded 30s") from exc
    if proc.returncode != 0:
        stderr = (proc.stderr or b"").decode(errors="replace")[:300]
        raise NormalizeFatal("ffprobe_failed", f"rc={proc.returncode}: {stderr}")

    try:
        payload = json.loads(proc.stdout.decode("utf-8") or "{}")
    except (UnicodeDecodeError, ValueError) as exc:
        raise NormalizeFatal("ffprobe_unparseable", str(exc)) from exc

    audio_streams = [s for s in payload.get("streams", []) if s.get("codec_type") == "audio"]
    has_audio = bool(audio_streams)

    duration: Optional[float] = None
    fmt = payload.get("format") or {}
    if fmt.get("duration"):
        try:
            duration = float(fmt["duration"])
        except (TypeError, ValueError):
            duration = None
    if duration is None:
        for s in audio_streams:
            if s.get("duration"):
                try:
                    duration = float(s["duration"])
                    break
                except (TypeError, ValueError):
                    pass
    return has_audio, duration


__all__ = ["NormalizeFatal", "ffmpeg_normalize", "passthrough_normalize"]
