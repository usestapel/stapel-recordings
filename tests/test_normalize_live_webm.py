"""THE GATE: a browser-recorded .webm carries no duration in its header, and
a caller reading that as "unknown length forever" parks a paying customer.

The fixture is built IN this test, from a REAL ffmpeg invocation, never
mocked: ``ffmpeg ... -f webm -live 1 -`` is exactly the shape of a live
MediaRecorder mux — the header streams out before the muxer knows how long
the recording will be, so it never carries a Matroska Duration element.
Mocking this away would prove nothing about the actual defect.

Ordering matters and is asserted in order:
  (a) the fixture self-certifies — a header-only read must return None, or
      the rest of this module is testing a file that never had the defect;
  (b) probe_duration recovers the true duration via the packet-scan fallback;
  (c) ffmpeg_normalize returns that duration too, not None;
  (d) a generous cap does not leak into the returned duration — the answer
      is what was actually written (~4s), not the cap (600s).

A separate, fully mocked unit test (in the style of test_normalize_cap.py's
``run`` fixture) proves the scan command is issued ONLY after a header
miss: a file with a good header must not pay for a demux-only packet walk.

Needs a real ffmpeg/ffprobe on PATH. A MISSING binary must fail this module
(RED), never skip silently — the only sanctioned skip is the explicit
``STAPEL_RECORDINGS_SKIP_FFMPEG=1`` opt-out (CI installs ffmpeg and never
sets it).
"""
import io
import os
import subprocess

import pytest

from stapel_recordings import normalize

pytestmark = pytest.mark.skipif(
    os.environ.get("STAPEL_RECORDINGS_SKIP_FFMPEG") == "1",
    reason="STAPEL_RECORDINGS_SKIP_FFMPEG=1 — explicit opt-out only; a "
    "MISSING ffmpeg binary must fail this module, not skip it.",
)


@pytest.fixture(scope="module")
def live_webm(tmp_path_factory):
    """A live-muxed webm, captured from ffmpeg's own stdout — never
    hand-built, never mocked, so this really is the defect being closed.
    """
    path = tmp_path_factory.mktemp("live-webm") / "recording.webm"
    cmd = [
        "ffmpeg", "-f", "lavfi", "-i", "sine=frequency=440:duration=4",
        "-c:a", "libopus", "-f", "webm", "-live", "1", "-",
    ]
    with open(path, "wb") as out:
        proc = subprocess.run(cmd, stdout=out, stderr=subprocess.PIPE, timeout=60)
    assert proc.returncode == 0, (
        f"fixture build failed (rc={proc.returncode}): "
        f"{proc.stderr.decode(errors='replace')[:500]}"
    )
    return str(path)


def test_live_webm_duration_recovery(tmp_path, live_webm):
    # (a) — the fixture self-certifies. Without this, everything below could
    # be reading a duration off a header that had one all along.
    has_audio, header_duration = normalize._probe_audio(live_webm)
    assert has_audio
    assert header_duration is None, "fixture no longer duration-less"

    # (b) — the public, authoritative probe recovers it via the packet scan.
    assert normalize.probe_duration(live_webm) == pytest.approx(4.0, abs=0.1)

    # (c) — ffmpeg_normalize returns a real duration, not None.
    dst = tmp_path / "out.opus"
    duration = normalize.ffmpeg_normalize(live_webm, str(dst))
    assert duration is not None
    assert duration == pytest.approx(4.0, abs=0.1)

    # (d) — a generous cap does not leak into the answer: the written file
    # is still ~4s, not the 600s that was merely permitted.
    dst_capped = tmp_path / "out_capped.opus"
    capped_duration = normalize.ffmpeg_normalize(
        live_webm, str(dst_capped), max_duration_seconds=600
    )
    assert capped_duration == pytest.approx(4.0, abs=0.1)
    assert capped_duration != 600


# ── the scan is issued ONLY after a header miss ───────────────────────────


class _FakePopen:
    """Minimal ``Popen`` stand-in — a header-miss fallback with a scripted
    packet stream, no real ffprobe involved."""

    def __init__(self, cmd, **kwargs):
        self.args = list(cmd)
        self.stdout = io.StringIO("0.000000,1.000000\n1.500000,0.500000\n")
        self.stderr = io.StringIO("")
        self.returncode = 0

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        self.returncode = -9


def test_packet_scan_runs_only_on_a_header_miss(monkeypatch):
    """A good header must not pay for a demux-only scan of every packet —
    real cost for an hours-long recording."""
    calls = []

    def fake_popen(cmd, **kwargs):
        calls.append(list(cmd))
        return _FakePopen(cmd)

    monkeypatch.setattr(normalize.subprocess, "Popen", fake_popen)

    # Header HIT: probe_duration must return straight from _probe_audio.
    monkeypatch.setattr(normalize, "_probe_audio", lambda path: (True, 42.0))
    assert normalize.probe_duration("good-header.mp4") == 42.0
    assert calls == [], "a good header must not pay for a packet scan"

    # Header MISS: the scan runs, with exactly the documented command.
    monkeypatch.setattr(normalize, "_probe_audio", lambda path: (True, None))
    result = normalize.probe_duration("live.webm")

    assert len(calls) == 1, "header miss must trigger the packet-scan fallback"
    cmd = calls[0]
    assert cmd[0] == normalize._ffprobe_bin()
    assert cmd[1:] == [
        "-v", "error", "-select_streams", "a:0",
        "-show_entries", "packet=pts_time,duration_time", "-of", "csv=p=0",
        "live.webm",
    ]
    # 1.5 + 0.5 from the fake's last packet line.
    assert result == pytest.approx(2.0, abs=1e-6)
