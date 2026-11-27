"""Recover the readable head of an MP4/M4A whose index was cut off.

A recorder writes ``mdat`` (the audio) first and ``moov`` (the index) last. A
file cut inside ``moov`` keeps all its audio and part of the sample-size
table, but loses the chunk offsets, so no demuxer can open it. For a single
AAC track the samples sit back to back from the start of ``mdat``, so the
surviving sizes locate every frame they cover.

That layout is an assumption, so it is verified rather than trusted: the
rebuilt stream must decode strictly with no error at all. A file with a second
track interleaved, or anything else that does not fit, yields ``None`` and
fails as before.
"""
from __future__ import annotations

import logging
import mmap
import os
import struct
import subprocess
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

#: ``partial_source["reason"]`` for what this module recovers.
REASON_TRUNCATED_INDEX = "truncated_index"

_AAC_LC = 2
_SAMPLES_PER_FRAME = 1024
_RATES = (96000, 88200, 64000, 48000, 44100, 32000, 24000, 22050, 16000,
          12000, 11025, 8000, 7350)
#: ADTS stores the frame length, header included, in 13 bits.
_MAX_FRAME = 0x1FFF - 7


@dataclass(frozen=True)
class Salvage:
    """What was recovered, against what the file says it held."""

    recovered_seconds: float
    expected_seconds: float


@dataclass(frozen=True)
class _Track:
    rate_index: int
    channels: int
    data_start: int
    data_end: int
    sizes: list
    declared_samples: int


def _top_level_boxes(data):
    offset = 0
    while offset + 8 <= len(data):
        size, kind = struct.unpack_from(">I4s", data, offset)
        header = 8
        if size == 1:
            if offset + 16 > len(data):
                return
            size, header = struct.unpack_from(">Q", data, offset + 8)[0], 16
        elif size == 0:
            size = len(data) - offset
        if size < header:
            return
        yield kind, offset, header, size
        offset += size


def _descriptor(data, offset):
    tag, length = data[offset], 0
    offset += 1
    for _ in range(4):
        byte = data[offset]
        offset += 1
        length = (length << 7) | (byte & 0x7F)
        if not byte & 0x80:
            break
    return tag, length, offset


def _audio_config(data, start):
    """``(object_type, rate_index, channels)`` from the ``esds`` box."""
    esds = data.find(b"esds", start)
    if esds < 0:
        return None
    tag, _, offset = _descriptor(data, esds + 8)  # ES_Descriptor
    if tag != 0x03:
        return None
    flags = data[offset + 2]
    if flags:  # optional ES fields: not written by the recorders this serves
        return None
    tag, _, offset = _descriptor(data, offset + 3)  # DecoderConfig
    if tag != 0x04:
        return None
    tag, length, offset = _descriptor(data, offset + 13)  # AudioSpecificConfig
    if tag != 0x05 or length < 2:
        return None
    bits = int.from_bytes(data[offset:offset + 2], "big")
    return bits >> 11, (bits >> 7) & 0x0F, (bits >> 3) & 0x0F


def _read_track(data) -> Optional[_Track]:
    mdat = moov = None
    for kind, offset, header, size in _top_level_boxes(data):
        if kind == b"mdat" and offset + size <= len(data):
            mdat = (offset + header, offset + size)
        elif kind == b"moov":
            # Only a moov that runs past the end of the file is salvage's
            # business; a whole one is the demuxer's.
            if offset + size <= len(data):
                return None
            moov = offset
    if mdat is None or moov is None:
        return None
    stsd = data.find(b"stsd", moov)
    stsz = data.find(b"stsz", moov)
    if stsd < 0 or stsz < 0 or stsz + 16 > len(data):
        return None
    config = _audio_config(data, stsd)
    if config is None:
        return None
    object_type, rate_index, channels = config
    if object_type != _AAC_LC or rate_index >= len(_RATES) or not 1 <= channels <= 7:
        return None
    uniform, declared = struct.unpack_from(">II", data, stsz + 8)
    if uniform or not declared:
        return None
    sizes, used = [], 0
    for index in range(min(declared, (len(data) - stsz - 16) // 4)):
        size = struct.unpack_from(">I", data, stsz + 16 + 4 * index)[0]
        if not 0 < size <= _MAX_FRAME or mdat[0] + used + size > mdat[1]:
            return None
        sizes.append(size)
        used += size
    if not sizes:
        return None
    return _Track(rate_index, channels, mdat[0], mdat[1], sizes, declared)


def _adts_header(rate_index: int, channels: int, payload: int) -> bytes:
    length = payload + 7
    return bytes((
        0xFF, 0xF1,
        ((_AAC_LC - 1) << 6) | (rate_index << 2) | (channels >> 2),
        ((channels & 3) << 6) | (length >> 11),
        (length >> 3) & 0xFF,
        ((length & 7) << 5) | 0x1F,
        0xFC,
    ))


def _ffmpeg(args: list) -> bool:
    """True only for a clean run: exit 0 and not one error line."""
    from .normalize import _ffmpeg_bin, _subprocess_timeout

    try:
        proc = subprocess.run(
            [_ffmpeg_bin(), "-hide_banner", "-nostdin", "-v", "error", *args],
            capture_output=True, timeout=_subprocess_timeout(),
        )
    except (OSError, subprocess.TimeoutExpired):
        logger.warning("salvage: ffmpeg did not finish", exc_info=True)
        return False
    return proc.returncode == 0 and not proc.stderr.strip()


def salvage_truncated_mp4(src_path: str, dst_path: str) -> Optional[Salvage]:
    """Write the recoverable head of *src_path* to *dst_path* (an M4A).

    ``None`` when the file is not this kind of damage, or the rebuilt audio
    does not decode cleanly. Never raises for a bad input.
    """
    raw = dst_path + ".aac"
    try:
        with open(src_path, "rb") as handle:
            with mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ) as data:
                track = _read_track(data)
                if track is None:
                    return None
                with open(raw, "wb") as out:
                    offset = track.data_start
                    for size in track.sizes:
                        out.write(_adts_header(track.rate_index, track.channels, size))
                        out.write(data[offset:offset + size])
                        offset += size
        # Remuxed so the duration is exact: a bare ADTS stream only has an
        # estimate from its bitrate, and a plan cap is computed from it.
        if not _ffmpeg(["-y", "-i", raw, "-c:a", "copy", "-f", "mp4", dst_path]):
            return None
        if not _ffmpeg(["-xerror", "-err_detect", "explode", "-i", dst_path,
                        "-f", "null", "-"]):
            return None
    except (OSError, ValueError, IndexError, struct.error):
        logger.warning("salvage: could not read %s", src_path, exc_info=True)
        return None
    finally:
        if os.path.exists(raw):
            os.unlink(raw)
    frame = _SAMPLES_PER_FRAME / _RATES[track.rate_index]
    return Salvage(
        recovered_seconds=round(len(track.sizes) * frame, 3),
        expected_seconds=round(track.declared_samples * frame, 3),
    )


__all__ = ["REASON_TRUNCATED_INDEX", "Salvage", "salvage_truncated_mp4"]
