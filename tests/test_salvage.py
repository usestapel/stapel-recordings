"""A file cut inside its index keeps its audio: the readable head is processed."""
import shutil
import struct
import subprocess
import uuid

import pytest

from stapel_recordings import salvage, stages
from stapel_recordings.dto import partial_source
from stapel_recordings.models import Recording, RecordingStatus
from stapel_recordings.normalize import ffmpeg_normalize, probe_duration

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")

SECONDS = 20


def _make(path, *extra):
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
         f"anoisesrc=d={SECONDS}:c=pink:r=48000:a=0.3", *extra,
         "-ac", "1", "-c:a", "aac", "-profile:a", "aac_low", str(path)],
        check=True, timeout=60,
    )
    return path


def _cut_inside_sample_sizes(src, dst, keep: float):
    """Truncate *src* after ``keep`` of its sample-size table."""
    data = src.read_bytes()
    stsz = data.rindex(b"stsz")
    count = struct.unpack_from(">I", data, stsz + 12)[0]
    dst.write_bytes(data[:stsz + 16 + 4 * int(count * keep)])
    return dst


@pytest.fixture
def damaged(tmp_path):
    whole = _make(tmp_path / "whole.m4a")
    return _cut_inside_sample_sizes(whole, tmp_path / "cut.m4a", 0.4)


def test_the_readable_head_is_recovered(tmp_path, damaged):
    result = salvage.salvage_truncated_mp4(str(damaged), str(tmp_path / "head.m4a"))

    assert result is not None
    assert result.expected_seconds == pytest.approx(SECONDS, abs=0.1)
    assert result.recovered_seconds == pytest.approx(SECONDS * 0.4, abs=0.1)
    assert probe_duration(str(tmp_path / "head.m4a")) == pytest.approx(
        result.recovered_seconds, abs=0.1
    )
    assert list(tmp_path.glob("*.aac")) == []


def test_a_whole_file_is_not_touched(tmp_path):
    whole = _make(tmp_path / "whole.m4a")
    assert salvage.salvage_truncated_mp4(str(whole), str(tmp_path / "h.m4a")) is None


def test_offsets_that_do_not_hold_are_refused(tmp_path, damaged):
    """Same index, audio bytes that are not where the sizes say."""
    data = bytearray(damaged.read_bytes())
    mdat = data.index(b"mdat") + 4
    data[mdat + 100:mdat + 20_000] = bytes(range(256)) * 77 + bytes(188)
    broken = tmp_path / "broken.m4a"
    broken.write_bytes(bytes(data))
    assert salvage.salvage_truncated_mp4(str(broken), str(tmp_path / "h.m4a")) is None


@pytest.mark.parametrize("content", [b"", b"not a media file" * 100,
                                     struct.pack(">I4s", 8, b"ftyp")])
def test_garbage_is_refused_without_raising(tmp_path, content):
    path = tmp_path / "g.m4a"
    path.write_bytes(content)
    assert salvage.salvage_truncated_mp4(str(path), str(tmp_path / "h.m4a")) is None


def _convert(monkeypatch, settings, source, **extra):
    stored = {}

    class _Storage:
        def download_to_file(self, key, path):
            shutil.copyfile(source, path)

        def upload_from_file(self, key, path, content_type=None):
            stored[key] = path

        def delete_object(self, key):
            stored["deleted"] = key

    monkeypatch.setattr(stages, "get_storage", lambda: _Storage())
    settings.STAPEL_RECORDINGS = {
        **getattr(settings, "STAPEL_RECORDINGS", {}),
        "NORMALIZER": f"{ffmpeg_normalize.__module__}.ffmpeg_normalize",
        **extra,
    }
    recording = Recording.objects.create(
        id=uuid.uuid4(), workspace_id=uuid.uuid4(), title="t",
        file_storage_key="k", status=RecordingStatus.QUEUED,
    )
    stages.get_stage("convert").run(recording, {})
    recording.refresh_from_db()
    return recording, stored


@pytest.mark.django_db
def test_convert_processes_the_head_says_so_and_keeps_the_upload(
    monkeypatch, settings, damaged
):
    recording, stored = _convert(monkeypatch, settings, damaged)

    assert recording.duration_seconds == pytest.approx(SECONDS * 0.4, abs=0.2)
    block = partial_source(recording)
    assert block.reason == "truncated_index"
    assert block.expected_seconds == pytest.approx(SECONDS, abs=0.1)
    assert recording.file_storage_key == "k"
    assert "deleted" not in stored


@pytest.mark.django_db
def test_the_switch_leaves_the_file_failing(monkeypatch, settings, damaged):
    with pytest.raises(stages.StageFatal):
        _convert(monkeypatch, settings, damaged, SALVAGE_DAMAGED_SOURCE=False)


@pytest.mark.django_db
def test_a_healthy_file_carries_no_block(monkeypatch, settings, tmp_path):
    recording, stored = _convert(monkeypatch, settings, _make(tmp_path / "w.m4a"))
    assert partial_source(recording) is None
    assert stored["deleted"] == "k"
