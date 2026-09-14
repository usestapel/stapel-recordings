"""Audio extraction and normalization — the seam that decides what is KEPT.

This is an audio service. Whatever a client uploads is transport: the
``convert`` stage extracts its audio track, downmixes it to mono at the
configured profile, stores that one object and deletes the source. A video
container is never an artifact — no field, no URL, no way to ask for it
back — and that holds for a 900 MB screen recording and a 3 MB voice memo
alike, so a host's storage bill scales with hours of speech instead of with
what someone happened to record on.

The ``convert`` stage calls the ``STAPEL_RECORDINGS["NORMALIZER"]``
callable: ``(src_path, dst_path) -> float | None`` (duration seconds, or
None when unknown). Raise :class:`NormalizeFatal` for unfixable input
(no audio stream, unreadable) so the driver DLQs instead of retrying, or
:class:`NormalizePaymentRequired` when the account cannot pay for this
recording — that one parks it in ``needs_payment`` rather than failing it.

The stored profile is :func:`audio_profile`, read from settings: mono,
16 kHz, Ogg/Opus at 24 kbps by default — about 10.8 MB per hour, against
~115 MB for the same hour as 16 kHz mono PCM WAV and several hundred MB for
the video it was extracted from. Every upload is re-encoded to it, including
one that arrives as audio already: a conditional "this one is fine as it is"
branch would have to be right about container, codec, channel layout and
sample rate at once, and it would make the stored bytes depend on what the
client happened to send — which is precisely the unpredictability the
profile removes. One generation of Opus at 24 kbps mono costs nothing an
ASR or diarization model can see.

Two implementations ship:

- :func:`ffmpeg_normalize` (default) — shells out to ffmpeg/ffprobe.
- :func:`passthrough_normalize` — copies the file unchanged; for
  environments without ffmpeg or when the upload is already normalized.
  Also the natural choice in tests. Note that selecting it disables ALL
  transcoding — whatever was uploaded is what every later stage opens — so
  a system check (W008) says so out loud at deploy time rather than letting
  it pass silently.

Which binaries this shells out to (``FFMPEG_BIN`` / ``FFPROBE_BIN``) and how
long a call may run (``FFMPEG_TIMEOUT_SECONDS``) are settings read at call
time, like everything else in this module. They used to be module-level
``os.environ`` reads, which both froze them at import and let any process
environment pick argv[0] of a subprocess run over user-supplied media.

:func:`ffmpeg_normalize` accepts an optional ``max_duration_seconds`` — a
duration cap for free-tier plans ("first N minutes of any recording"). The
cut happens HERE, at the pipeline entrance, so no later stage knows about
plans or can process (and pay a provider for) minutes the client didn't buy.
:func:`probe_duration` returns the source duration for an honest "first 10
of 47 minutes" label.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from typing import Optional

from .conf import flag, recordings_settings

TARGET_SAMPLE_RATE = 16000
TARGET_CHANNELS = 1

#: Stored-audio codecs this module knows how to write and name.
CODEC_OPUS = "opus"
CODEC_WAV = "wav"
CODECS = (CODEC_OPUS, CODEC_WAV)


@dataclass(frozen=True)
class AudioProfile:
    """What one stored recording is, byte for byte.

    Not just ffmpeg arguments: ``ext`` and ``content_type`` name the object
    the ``convert`` stage writes, and :attr:`bytes_per_hour` is the number a
    host sizes a bucket with and the upload-limits read quotes.
    """

    codec: str
    channels: int
    sample_rate: int
    bitrate_bps: int
    ext: str
    content_type: str

    @property
    def bytes_per_hour(self) -> int:
        """Stored bytes for one hour of audio at this profile.

        Opus is VBR, so this is its target bitrate — speech averages a
        little under it. PCM is exact: ``rate × channels × 2 bytes``.
        """
        if self.codec == CODEC_WAV:
            return self.sample_rate * self.channels * 2 * 3600
        return self.bitrate_bps // 8 * 3600


def audio_profile() -> AudioProfile:
    """The stored-audio profile, read from settings at call time.

    An unknown ``AUDIO_CODEC`` is a :class:`NormalizeFatal` rather than a
    silent fallback: writing a differently-shaped object than the one the
    deployment asked for is worse than refusing the conversion, and the
    ``stapel_recordings.E006`` system check says so at deploy time instead.
    """
    codec = str(recordings_settings.AUDIO_CODEC).strip().lower()
    channels = max(1, int(recordings_settings.AUDIO_CHANNELS))
    sample_rate = int(recordings_settings.AUDIO_SAMPLE_RATE)
    bitrate = int(recordings_settings.AUDIO_BITRATE_BPS)
    if codec == CODEC_OPUS:
        return AudioProfile(codec, channels, sample_rate, bitrate, ".opus", "audio/ogg")
    if codec == CODEC_WAV:
        return AudioProfile(codec, channels, sample_rate, 0, ".wav", "audio/wav")
    raise NormalizeFatal("unknown_audio_codec", f"AUDIO_CODEC={codec!r} is not one of {CODECS}")


def audio_only_ingest_active() -> bool:
    """True iff this deployment really does keep audio and nothing else.

    Three things have to hold, and a host can switch off any one of them:
    the policy itself (``AUDIO_ONLY_INGEST``), a ``convert`` stage in the
    ``PIPELINE`` to run the extraction, and a NORMALIZER that actually
    transcodes — ``passthrough_normalize`` copies the upload through, so
    with it the stored object IS the container.

    Read by ``services.accepted_upload_limit`` and by the ``convert`` stage,
    which is the point: the raised container ceiling exists because the
    container is discarded, so a deployment that has stopped discarding it
    must stop accepting the larger file too, without anyone remembering to
    lower a second setting. ``stapel_recordings.E007`` reports the
    half-configured state at deploy time.
    """
    if not flag("AUDIO_ONLY_INGEST"):
        return False
    if recordings_settings.NORMALIZER is passthrough_normalize:
        return False
    return "convert" in list(recordings_settings.PIPELINE or [])


def _ffmpeg_bin() -> str:
    """The ffmpeg executable, read at CALL time from settings.

    Not a module-level ``os.environ.get``: that is argv[0] of a subprocess
    this module runs on user-supplied media, so "which binary" is a trust
    decision, and it used to be answerable by any environment variable that
    happened to be named ``FFMPEG_BIN`` in the pod. It is also frozen at
    import that way, which the module's own conf policy forbids —
    everything else here is read lazily so a host can change it. Settings
    only, and ``no_env`` (see conf.py)."""
    return str(recordings_settings.FFMPEG_BIN)


def _ffprobe_bin() -> str:
    """The ffprobe executable — same reasoning as :func:`_ffmpeg_bin`."""
    return str(recordings_settings.FFPROBE_BIN)


def _subprocess_timeout() -> int:
    """Seconds an ffmpeg/ffprobe call may run before it is killed."""
    return int(recordings_settings.FFMPEG_TIMEOUT_SECONDS)


class NormalizeFatal(Exception):
    """Input can't be normalized — bad file, no audio stream, ffmpeg crash."""

    def __init__(self, reason: str, detail: Optional[str] = None):
        super().__init__(f"{reason}: {detail or ''}")
        self.reason = reason
        self.detail = detail


class NormalizePaymentRequired(NormalizeFatal):
    """The wallet cannot buy this recording — park it, don't fail it.

    The normalizer is the one seam that sees the audio before anything has
    been spent on it: it knows the source duration, the plan's cap and, in a
    host that wires one, the balance. A host gate ("this account has 3 free
    minutes left and this is a 47-minute file") therefore has to be able to
    say *stop, but not broken* through the same
    ``(src, dst) -> duration`` signature, without a second seam and without
    the driver importing anything of the host's.

    A subclass of :class:`NormalizeFatal` so an existing handler that only
    knows the base class still stops the pipeline rather than transcoding
    minutes nobody paid for. The ``convert`` stage catches THIS first and
    re-raises ``stages.StageNeedsPayment``, which parks the recording in the
    ``needs_payment`` status instead of DLQ'ing it.
    """


def passthrough_normalize(src_path: str, dst_path: str) -> Optional[float]:
    """Copy ``src`` to ``dst`` unchanged. Duration is unknown (None)."""
    shutil.copyfile(src_path, dst_path)
    return None


def probe_duration(src_path: str) -> Optional[float]:
    """Source duration in seconds, without transcoding.

    Public because a host has a legitimate reason to know the SOURCE
    duration separately from the resulting one: if a recording was capped
    by a plan, it needs to say "first 10 of 47 minutes", not just "10
    minutes". Without this, a host would reach into the private
    ``_probe_audio`` or add its own ffprobe call — a second copy of this
    logic that would drift from it on the first change.
    """
    _, duration = _probe_audio(src_path)
    return duration


def ffmpeg_normalize(
    src_path: str,
    dst_path: str,
    *,
    max_duration_seconds: Optional[float] = None,
) -> Optional[float]:
    """Probe + extract the audio track at :func:`audio_profile`. Returns
    duration seconds.

    Video is decoded by nothing here (``-vn``) and no stream but audio
    reaches *dst_path*; the caller deletes the source container.

    ``max_duration_seconds`` caps the result's duration (ffmpeg ``-t``).
    Needed for free-tier plans: "first N minutes of any recording" is cut
    RIGHT HERE, at the pipeline entrance, not later. With the cut applied at
    this step, everything downstream — transcription, diarization, summary,
    embeddings — works on the capped audio without knowing about plans at
    all, and can't accidentally process (and pay a provider for) minutes the
    client didn't buy.

    Returns the duration of WHAT WAS WRITTEN, not the source: the caller
    stores it as the recording's duration, and it must describe the file
    that actually exists. Use :func:`probe_duration` for the source duration
    when an honest label is needed.
    """
    has_audio, duration = _probe_audio(src_path)
    if not has_audio:
        raise NormalizeFatal("no_audio_stream", "input has no decodable audio track")
    cap = None
    if max_duration_seconds is not None and max_duration_seconds > 0:
        cap = float(max_duration_seconds)
    _run_ffmpeg(src_path, dst_path, max_duration_seconds=cap)
    if cap is not None and duration is not None:
        return min(duration, cap)
    # Duration unknown (ffprobe didn't return one), but a cap was requested
    # and applied — the cap is the best we know about the file on disk.
    return cap if duration is None else duration


def _run_ffmpeg(src: str, dst: str, *, max_duration_seconds: Optional[float] = None) -> None:
    profile = audio_profile()
    # -vn is the whole audio-only promise in one flag: no video stream is
    # decoded, so none can reach the output object, and the container that
    # carried it is deleted by the caller.
    cmd = [
        _ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-y",
        "-i", src, "-vn", "-map_metadata", "-1",
    ]
    if max_duration_seconds is not None:
        # After -i: the limit applies to OUTPUT. Before -i it would cap
        # input decode time instead — a different duration for streaming
        # containers.
        cmd += ["-t", f"{max_duration_seconds:.3f}"]
    cmd += ["-ac", str(profile.channels), "-ar", str(profile.sample_rate)]
    if profile.codec == CODEC_OPUS:
        cmd += [
            "-c:a", "libopus", "-b:a", str(profile.bitrate_bps),
            # Speech at 16 kHz: VOIP mode spends the bitrate on
            # intelligibility rather than on music-grade high end, which is
            # what an ASR and a diarizer read.
            "-application", "voip", "-vbr", "on", "-f", "ogg", dst,
        ]
    else:
        cmd += ["-c:a", "pcm_s16le", "-f", "wav", dst]
    timeout = _subprocess_timeout()
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except FileNotFoundError as exc:
        raise NormalizeFatal("ffmpeg_missing", str(exc)) from exc
    except subprocess.TimeoutExpired as exc:
        raise NormalizeFatal("ffmpeg_timeout", f"exceeded {timeout}s") from exc
    if proc.returncode != 0:
        stderr = (proc.stderr or b"").decode(errors="replace")[:500]
        raise NormalizeFatal("ffmpeg_failed", f"rc={proc.returncode}: {stderr}")


def _probe_audio(path: str) -> tuple[bool, Optional[float]]:
    cmd = [
        _ffprobe_bin(), "-v", "error", "-print_format", "json",
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


__all__ = [
    "AudioProfile",
    "CODECS",
    "CODEC_OPUS",
    "CODEC_WAV",
    "NormalizeFatal",
    "NormalizePaymentRequired",
    "audio_only_ingest_active",
    "audio_profile",
    "ffmpeg_normalize",
    "passthrough_normalize",
    "probe_duration",
]
