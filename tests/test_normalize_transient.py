"""A conversion that did not finish is retried; a bad file is not."""
import subprocess
import uuid

import pytest

from stapel_recordings import normalize, stages
from stapel_recordings.models import Recording, RecordingStatus


class _Exit:
    stdout = b""
    stderr = b"boom"

    def __init__(self, returncode):
        self.returncode = returncode


def _timeout(cmd, **kwargs):
    raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout"))


@pytest.mark.parametrize("call, tool", [
    (lambda: normalize._run_ffmpeg("in", "out.wav"), "ffmpeg"),
    (lambda: normalize._probe_audio("in"), "ffprobe"),
])
@pytest.mark.parametrize("fake, suffix", [
    (_timeout, "timeout"),
    (lambda cmd, **kw: _Exit(-9), "killed"),
])
def test_unfinished_process_is_transient(monkeypatch, call, tool, fake, suffix):
    monkeypatch.setattr(normalize.subprocess, "run", fake)
    with pytest.raises(normalize.NormalizeTransient) as caught:
        call()
    assert caught.value.reason == f"{tool}_{suffix}"


@pytest.mark.parametrize("call, reason", [
    (lambda: normalize._run_ffmpeg("in", "out.wav"), "ffmpeg_failed"),
    (lambda: normalize._probe_audio("in"), "ffprobe_failed"),
])
def test_error_exit_stays_fatal(monkeypatch, call, reason):
    monkeypatch.setattr(normalize.subprocess, "run", lambda cmd, **kw: _Exit(1))
    with pytest.raises(normalize.NormalizeFatal) as caught:
        call()
    assert not isinstance(caught.value, normalize.NormalizeTransient)
    assert caught.value.reason == reason


_RAISE: list = []


def failing_normalizer(src, dst, **kwargs):
    raise _RAISE[-1]


@pytest.mark.parametrize("raised, expected", [
    ("transient", stages.StageRetryable),
    ("fatal", stages.StageFatal),
])
def test_convert_stage_maps_the_two_kinds(monkeypatch, settings, raised, expected):
    kinds = {
        "transient": ("NormalizeTransient", "ffmpeg_timeout"),
        "fatal": ("NormalizeFatal", "ffmpeg_failed"),
    }
    cls, reason = kinds[raised]
    _RAISE.append(getattr(normalize, cls)(reason, "detail"))

    class _Storage:
        def download_to_file(self, key, path):
            open(path, "wb").close()

    monkeypatch.setattr(stages, "get_storage", lambda: _Storage())
    settings.STAPEL_RECORDINGS = {
        **getattr(settings, "STAPEL_RECORDINGS", {}),
        "NORMALIZER": f"{__name__}.failing_normalizer",
    }
    convert = stages.get_stage("convert")
    monkeypatch.setattr(convert, "_store_probe", lambda *a: None)
    recording = Recording(
        id=uuid.uuid4(), workspace_id=uuid.uuid4(), title="t",
        file_storage_key="k", status=RecordingStatus.QUEUED,
    )
    with pytest.raises(expected) as caught:
        convert.run(recording, {})
    assert caught.value.reason == reason
