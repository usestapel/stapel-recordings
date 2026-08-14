"""Duration cap at the pipeline entrance — the basis for free-tier plans.

"First N minutes of any recording, free" is implemented exactly here and
nowhere else: cut the audio at normalization time, and every later stage
(transcription, diarization, summary, embeddings) only ever sees the paid
minutes.

The tests guard three things, each of which can break silently:
  - with no cap, the ffmpeg command must stay byte-for-byte the same;
  - ``-t`` must sit AFTER ``-i``: before ``-i`` it limits input decode time,
    which is a different duration for streaming containers;
  - the duration returned is that of what was ACTUALLY WRITTEN, not the
    source — otherwise the interface promises minutes that don't exist.
"""
import pytest

from stapel_recordings import normalize


class _Done:
    returncode = 0
    stdout = b""
    stderr = b""


@pytest.fixture
def run(monkeypatch):
    """Intercepts subprocess: verifies the REAL command, not a paraphrase.

    Patches ``subprocess.run`` itself rather than ``_run_ffmpeg``, so the
    test doesn't just check its own stand-in. ``captured["cmd"]`` ends up
    holding exactly what would have gone to ffmpeg.
    """
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = list(cmd)
        return _Done()

    def fake_probe(path):
        return True, captured.get("source_duration", 2820.0)  # 47 minutes

    monkeypatch.setattr(normalize.subprocess, "run", fake_run)
    monkeypatch.setattr(normalize, "_probe_audio", fake_probe)
    return captured


def test_no_cap_command_unchanged(run):
    normalize.ffmpeg_normalize("in.mp4", "out.wav")
    assert "-t" not in run["cmd"], (
        "cap leaked into a call that didn't ask for one"
    )


def test_cap_placed_after_input(run):
    normalize.ffmpeg_normalize("in.mp4", "out.wav", max_duration_seconds=600)
    cmd = run["cmd"]
    assert "-t" in cmd
    assert cmd[cmd.index("-t") + 1] == "600.000"
    assert cmd.index("-t") > cmd.index("in.mp4"), (
        "-t must come after -i: this caps OUTPUT, not input"
    )


def test_returns_written_duration(run):
    assert normalize.ffmpeg_normalize("in.mp4", "out.wav", max_duration_seconds=600) == 600.0


def test_short_recording_is_not_stretched(run):
    run["source_duration"] = 120.0
    assert normalize.ffmpeg_normalize("in.mp4", "out.wav", max_duration_seconds=600) == 120.0


def test_zero_and_negative_cap_are_ignored(run):
    for bad_value in (0, -1, -600):
        normalize.ffmpeg_normalize("in.mp4", "out.wav", max_duration_seconds=bad_value)
        assert "-t" not in run["cmd"], (
            f"cap {bad_value} was taken seriously — that would produce a zero-length file"
        )


def test_unknown_duration_with_cap(monkeypatch, run):
    """ffprobe returned no duration, but a cap was requested.

    The cap is the best information we have about the file on disk; None
    would mean "we know nothing", even though we ourselves capped it.
    """
    monkeypatch.setattr(normalize, "_probe_audio", lambda path: (True, None))
    assert normalize.ffmpeg_normalize("in.mp4", "out.wav", max_duration_seconds=600) == 600.0


# ── which binary gets executed is settings, not the environment ──────────
#
# FFMPEG_BIN / FFPROBE_BIN are argv[0] of a subprocess run over user-supplied
# media. They used to be module-level ``os.environ.get`` reads: frozen at
# import (so a host could not change them at all) and answerable by whatever
# happened to be exported in the pod (so anything could).


def test_ffmpeg_binary_comes_from_settings_at_call_time(run):
    from django.test import override_settings

    from stapel_recordings.conf import recordings_settings

    with override_settings(STAPEL_RECORDINGS={"FFMPEG_BIN": "/opt/media/bin/ffmpeg"}):
        recordings_settings.reload()
        normalize.ffmpeg_normalize("in.mp4", "out.wav")
    recordings_settings.reload()
    assert run["cmd"][0] == "/opt/media/bin/ffmpeg"


def test_ffprobe_binary_comes_from_settings_at_call_time(monkeypatch):
    from django.test import override_settings

    from stapel_recordings.conf import recordings_settings

    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = list(cmd)
        return _Done()

    monkeypatch.setattr(normalize.subprocess, "run", fake_run)
    with override_settings(STAPEL_RECORDINGS={"FFPROBE_BIN": "/opt/media/bin/ffprobe"}):
        recordings_settings.reload()
        normalize.probe_duration("in.mp4")
    recordings_settings.reload()
    assert captured["cmd"][0] == "/opt/media/bin/ffprobe"


def test_a_stray_environment_variable_does_not_choose_the_binary(monkeypatch, run):
    from stapel_recordings.conf import recordings_settings

    monkeypatch.setenv("FFMPEG_BIN", "/tmp/not-really-ffmpeg")
    recordings_settings.reload()
    normalize.ffmpeg_normalize("in.mp4", "out.wav")
    recordings_settings.reload()
    assert run["cmd"][0] == "ffmpeg"


def test_probe_duration_does_not_transcode(monkeypatch):
    """The public probe exists for an honest "first 10 of 47 minutes" label."""
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        return _Done()

    monkeypatch.setattr(normalize.subprocess, "run", fake_run)
    monkeypatch.setattr(normalize, "_probe_audio", lambda path: (True, 2820.0))
    assert normalize.probe_duration("in.mp4") == 2820.0
    assert calls == [], "the probe triggered a transcode"
