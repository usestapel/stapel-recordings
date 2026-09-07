"""Audio-only ingest: what leaves as an answer, and what is kept as bytes.

Two findings, one release.

**A size refusal is an answer, not a 500.** A host's own view called
``services.start_multipart_upload`` with a declared 4 727 057 010 bytes
against a 2 GiB ceiling and the request ended in HTTP 500: the refusal was a
plain ``ValueError`` subclass, so it propagated through DRF as an unhandled
exception. Nothing in the host was wrong — the library gave it no way to be
right short of a ``try/except`` per view. The tests below drive a
host-shaped view with **no error handling of its own** through a plain DRF
test client and require the 413 (and its 400 siblings) to come back in the
standard envelope, with the numbers a client needs to phrase the refusal.

**This module stores audio.** A container is transport: its audio track is
extracted, downmixed to mono, stored, and the container is deleted — every
upload, whatever its size, whether or not it carried video. So the tests
also pin what is left behind afterwards: one audio object, no container, no
working copy on disk (on the success path AND on every failure path), no
key that could hand a video back, and a purge that is retried rather than
logged when the object store refuses the delete.
"""
import json
import os

import pytest
from django.test import override_settings
from django.urls import path
from rest_framework.views import APIView

from stapel_recordings import media, services
from stapel_recordings.models import RecordingStatus, UploadSession
from stapel_recordings.storage import get_storage
from stapel_recordings.tests import fakes

pytestmark = pytest.mark.django_db

#: A 2 GiB ceiling and the exact declared size from the production log.
LIMIT_2GIB = 2 * 1024 * 1024 * 1024
DECLARED_4_7GB = 4_727_057_010


# ── a host's view, written the way a host writes one ─────────────────────


class HostMultipartView(APIView):
    """The endpoint from the report — ``POST …/recordings/<id>/multipart``.

    Deliberately bare: it authorizes nothing, catches nothing and knows no
    error keys. Everything it answers with has to come from the library.
    """

    permission_classes = []
    authentication_classes = []

    def post(self, request, recording_id):
        from stapel_recordings.models import Recording

        recording = Recording.objects.get(pk=recording_id)
        session, parts, part_size = services.start_multipart_upload(
            recording=recording,
            file_size_bytes=request.data.get("file_size_bytes"),
            filename=request.data.get("filename", "meeting.mp4"),
        )
        from django.http import JsonResponse

        return JsonResponse({"session": str(session.id), "part_size": part_size})


urlpatterns = [
    path("host/recordings/<uuid:recording_id>/multipart", HostMultipartView.as_view()),
    path("recordings/", __import__("django.urls", fromlist=["include"]).include(
        "stapel_recordings.urls"
    )),
]

#: The one thing a host must configure for this to work — the same handler
#: ``stapel_core.django.settings`` ships and every deployed stapel service
#: already runs. Not a per-view try/except.
HOST_LIKE = dict(
    ROOT_URLCONF=__name__,
    REST_FRAMEWORK={
        "EXCEPTION_HANDLER": "stapel_core.django.api.errors.stapel_exception_handler",
    },
)


def _post(client, recording, **body):
    return client.post(
        f"/host/recordings/{recording.id}/multipart", body, format="json"
    )


def test_oversized_multipart_answers_413_not_500(api_client, use_fakes, make_recording):
    """The reported incident, end to end."""
    r = make_recording(status=RecordingStatus.CREATED)
    with override_settings(**HOST_LIKE):
        response = _post(
            api_client, r, file_size_bytes=DECLARED_4_7GB, filename="meeting.mp4"
        )
    assert response.status_code == 413
    body = json.loads(response.content)
    assert body["localizable_error"] == "error.413.recording_too_large"
    # The two numbers, so the client can say "4.4 GB — the limit is 2 GB"
    # instead of "something went wrong".
    assert body["params"]["size"] == DECLARED_4_7GB
    assert body["params"]["limit"] == LIMIT_2GIB
    assert not UploadSession.objects.filter(recording=r).exists()


@pytest.mark.parametrize("declared", [None, 0, -1, "banana"])
def test_a_size_that_is_not_a_size_answers_400(
    api_client, use_fakes, make_recording, declared
):
    """Missing / zero / negative / non-numeric is the client's mistake about
    the request, not a file that is too big — 400, and still carrying the
    limit so the message never has to guess."""
    r = make_recording(status=RecordingStatus.CREATED)
    with override_settings(**HOST_LIKE):
        response = _post(api_client, r, file_size_bytes=declared)
    assert response.status_code == 400
    body = json.loads(response.content)
    assert body["localizable_error"] == "error.400.recording_upload_size_invalid"
    assert body["params"]["limit"] == LIMIT_2GIB


def test_too_many_parts_answers_400_with_the_cap(api_client, use_fakes, make_recording):
    r = make_recording(status=RecordingStatus.CREATED)
    session = services.create_upload_session(recording=r, filename="take.mp3")
    session.is_multipart = True
    session.multipart_upload_id = "u1"
    session.save()
    with override_settings(STAPEL_RECORDINGS={"MAX_MULTIPART_PARTS": 2}):
        with pytest.raises(services.InvalidMultipartParts) as excinfo:
            services._validated_parts(session, [{"part_number": n} for n in range(1, 5)])
    exc = excinfo.value
    assert (exc.http_status, exc.error_key) == (
        400, "error.400.recording_multipart_parts_invalid",
    )
    assert exc.error_params == {"max_parts": 2}


def test_a_misconfigured_part_budget_blames_the_deployment(use_fakes, make_recording):
    """``MAX_UPLOAD_BYTES / MULTIPART_PART_SIZE`` over the part cap is the
    operator's mistake, so it is a 500 — a 4xx would tell the client to fix
    a request that was fine."""
    r = make_recording(status=RecordingStatus.CREATED)
    with override_settings(STAPEL_RECORDINGS={
        "STORAGE": "stapel_recordings.tests.fakes.FakeStorage",
        "NORMALIZER": "stapel_recordings.normalize.passthrough_normalize",
        "MULTIPART_PART_SIZE": 1024,
        "MAX_MULTIPART_PARTS": 4,
    }):
        with pytest.raises(services.MultipartMisconfigured) as excinfo:
            services.start_multipart_upload(
                recording=r, file_size_bytes=10 * 1024, filename="take.mp3"
            )
    assert excinfo.value.http_status == 500
    assert excinfo.value.error_key == "error.500.internal"
    # Still an InvalidMultipartParts, so a caller that already caught that
    # keeps catching it.
    assert isinstance(excinfo.value, services.InvalidMultipartParts)


def test_the_plain_exceptions_stay_plain_exceptions():
    """Nothing here stops being usable outside HTTP: a worker, a management
    command or a script still catches ``ValueError``."""
    assert issubclass(services.UploadTooLarge, ValueError)
    assert issubclass(services.InvalidUploadSize, services.UploadTooLarge)
    assert issubclass(services.UploadNotStored, ValueError)
    assert issubclass(services.UploadContentUncheckable, RuntimeError)
    exc = services.UploadTooLarge(9, 8)
    assert "9" in str(exc) and "8" in str(exc)


# ── the limits are readable BEFORE the first byte ────────────────────────


def test_upload_limits_read_carries_the_numbers(api_client, use_fakes, user):
    api_client.force_authenticate(user=user)
    with override_settings(**HOST_LIKE):
        response = api_client.get("/recordings/api/v1/recordings/upload-limits")
    assert response.status_code == 200
    body = response.json()
    assert body["max_upload_bytes"] == LIMIT_2GIB  # passthrough → nothing extracted
    assert body["max_stored_bytes"] == 512 * 1024 * 1024
    assert body["audio_only_ingest"] is False
    assert body["multipart_part_size"] == 10 * 1024 * 1024
    assert "mp3" in body["allowed_extensions"]


def test_upload_limits_follow_the_ingest_policy(extracting):
    """With extraction on, the accepted ceiling is the CONTAINER one — the
    container is discarded, so what it costs is bandwidth, not storage."""
    limits = services.upload_limits()
    assert limits["audio_only_ingest"] is True
    assert limits["max_upload_bytes"] == 16 * 1024 * 1024 * 1024
    assert limits["stored_audio_channels"] == 1
    assert limits["stored_audio_codec"] == "opus"
    assert limits["stored_audio_sample_rate"] == 16000
    # 24 kbps mono ≈ 10.8 MB/hour. The number a UI multiplies.
    assert limits["stored_bytes_per_hour"] == 24000 // 8 * 3600


def test_the_413_quotes_the_ceiling_the_read_published(extracting, make_recording):
    """The refusal and the read cannot disagree: both come from
    ``accepted_upload_limit``."""
    published = services.upload_limits()["max_upload_bytes"]
    r = make_recording(status=RecordingStatus.CREATED)
    with pytest.raises(services.UploadTooLarge) as excinfo:
        services.start_multipart_upload(
            recording=r, file_size_bytes=published + 1, filename="meeting.mp4"
        )
    assert excinfo.value.error_params["limit"] == published


def test_the_ceiling_falls_back_when_extraction_is_switched_off(extracting):
    """The container ceiling exists *because* the container is discarded.
    Drop ``convert`` from the pipeline and the accepted size follows it down
    — nobody has to remember to lower a second setting."""
    assert services.accepted_upload_limit() == 16 * 1024 * 1024 * 1024
    with override_settings(STAPEL_RECORDINGS=dict(
        _EXTRACTING_SETTINGS, PIPELINE=["transcribe", "merge"],
    )):
        assert services.accepted_upload_limit() == LIMIT_2GIB


# ── video in, audio out, nothing else left ───────────────────────────────

#: A normalizer that behaves like ffmpeg without needing it: reads the
#: source, writes a much smaller "audio" object, reports a duration.
EXTRACTED_AUDIO = b"OggS\x00extracted-mono-audio"


def fake_extract_audio(src_path, dst_path):
    with open(src_path, "rb") as fh:
        assert fh.read(4) == b"\x1a\x45\xdf\xa3"  # the container really is here
    with open(dst_path, "wb") as fh:
        fh.write(EXTRACTED_AUDIO)
    return 600.0


def exploding_normalizer(src_path, dst_path):
    with open(dst_path, "wb") as fh:
        fh.write(b"half-written")
    raise RuntimeError("ffmpeg died halfway")


_EXTRACTING_SETTINGS = {
    "STORAGE": "stapel_recordings.tests.fakes.FakeStorage",
    "NORMALIZER": "stapel_recordings.tests.test_audio_only_ingest.fake_extract_audio",
}


@pytest.fixture
def extracting():
    """A deployment that really does extract audio: fake storage, a
    transcoding normalizer, ``convert`` in the pipeline."""
    from stapel_recordings import storage

    with override_settings(STAPEL_RECORDINGS=dict(_EXTRACTING_SETTINGS)):
        storage.reset_storage_cache()
        yield
    storage.reset_storage_cache()


@pytest.fixture
def uploaded_video(extracting, make_recording):
    """A recording whose uploaded object is a Matroska container."""
    r = make_recording(status=RecordingStatus.QUEUED)
    key = f"recordings/{r.workspace_id}/{r.id}/audio.mkv"
    r.file_storage_key = key
    r.file_size_bytes = 4_727_057_010
    r.save(update_fields=["file_storage_key", "file_size_bytes"])
    get_storage().put_bytes(key, b"\x1a\x45\xdf\xa3" + b"video-payload" * 64)
    return r


def _run_convert(recording):
    from stapel_recordings.stages import ConvertStage

    return ConvertStage().run(recording, {})


def test_video_in_audio_only_object_out(uploaded_video):
    container_key = uploaded_video.file_storage_key
    _run_convert(uploaded_video)
    uploaded_video.refresh_from_db()

    # One object, and it is the extracted audio.
    assert uploaded_video.normalized_storage_key.endswith("audio.normalized.opus")
    assert get_storage().get_bytes(uploaded_video.normalized_storage_key) == EXTRACTED_AUDIO
    # The container is gone from the store AND from the row: a key left
    # behind is a key some later read hands back.
    assert get_storage().head_object(container_key) == (False, None)
    assert uploaded_video.file_storage_key is None
    # What was received and what is kept are two different numbers now.
    assert uploaded_video.file_size_bytes == 4_727_057_010
    assert uploaded_video.stored_size_bytes == len(EXTRACTED_AUDIO)


def test_no_api_path_hands_the_container_back(uploaded_video):
    """Before the container is purged there is still a window; nothing may
    serve it in that window either."""
    assert media.media_storage_key(uploaded_video) is None
    _run_convert(uploaded_video)
    uploaded_video.refresh_from_db()
    assert media.media_storage_key(uploaded_video) == uploaded_video.normalized_storage_key


def test_the_container_is_purged_for_a_small_audio_upload_too(extracting, make_recording):
    """Not a rescue path for oversized video: the policy is the same for a
    3 MB voice memo."""
    r = make_recording(status=RecordingStatus.QUEUED)
    key = f"recordings/{r.workspace_id}/{r.id}/audio.m4a"
    r.file_storage_key = key
    r.save(update_fields=["file_storage_key"])
    get_storage().put_bytes(key, b"\x1a\x45\xdf\xa3" + b"tiny")
    _run_convert(r)
    r.refresh_from_db()
    assert r.file_storage_key is None
    assert get_storage().head_object(key) == (False, None)


def test_the_working_copy_does_not_outlive_the_stage(uploaded_video, monkeypatch):
    seen = []
    import tempfile as _tempfile

    real_mkdtemp = _tempfile.mkdtemp

    def spy(*args, **kwargs):
        d = real_mkdtemp(*args, **kwargs)
        seen.append(d)
        return d

    monkeypatch.setattr("stapel_recordings.stages.tempfile.mkdtemp", spy)
    _run_convert(uploaded_video)
    assert seen and not any(os.path.exists(d) for d in seen)


def test_the_working_copy_does_not_outlive_a_FAILED_stage(uploaded_video, monkeypatch):
    """The half-written temp file is the one that matters: a normalizer that
    dies mid-write leaves a partial copy of somebody's video on the worker's
    disk unless the stage cleans up on the failure path too."""
    seen = []
    import tempfile as _tempfile

    real_mkdtemp = _tempfile.mkdtemp

    def spy(*args, **kwargs):
        d = real_mkdtemp(*args, **kwargs)
        seen.append(d)
        return d

    monkeypatch.setattr("stapel_recordings.stages.tempfile.mkdtemp", spy)
    with override_settings(STAPEL_RECORDINGS=dict(
        _EXTRACTING_SETTINGS,
        NORMALIZER="stapel_recordings.tests.test_audio_only_ingest.exploding_normalizer",
    )):
        with pytest.raises(RuntimeError):
            _run_convert(uploaded_video)
    assert seen and not any(os.path.exists(d) for d in seen)
    # And nothing was recorded as stored, because nothing was.
    uploaded_video.refresh_from_db()
    assert uploaded_video.normalized_storage_key in (None, "")


def test_a_purge_that_fails_is_retried_not_logged(uploaded_video, monkeypatch):
    """A warning in a log is not a deletion. The stage asks to be re-driven,
    and the re-drive finishes the purge."""
    from stapel_recordings.stages import StageRetryable

    calls = {"n": 0}
    real_delete = fakes.FakeStorage.delete_object

    def flaky(self, key):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("object store said no")
        return real_delete(self, key)

    monkeypatch.setattr(fakes.FakeStorage, "delete_object", flaky)
    container_key = uploaded_video.file_storage_key
    with pytest.raises(StageRetryable):
        _run_convert(uploaded_video)

    uploaded_video.refresh_from_db()
    # The audio is stored, the container is still there, the key still
    # points at it — the row and the bucket agree, which is what makes the
    # retry safe.
    assert uploaded_video.normalized_storage_key
    assert uploaded_video.file_storage_key == container_key
    assert get_storage().head_object(container_key)[0] is True

    _run_convert(uploaded_video)  # the re-drive
    uploaded_video.refresh_from_db()
    assert uploaded_video.file_storage_key is None
    assert get_storage().head_object(container_key) == (False, None)


def test_extracted_audio_over_the_stored_ceiling_is_fatal(uploaded_video):
    from stapel_recordings.stages import StageFatal

    with override_settings(STAPEL_RECORDINGS=dict(_EXTRACTING_SETTINGS, MAX_STORED_BYTES=4)):
        with pytest.raises(StageFatal) as excinfo:
            _run_convert(uploaded_video)
    assert excinfo.value.reason == "stored_audio_too_large"


def test_a_host_that_wants_its_originals_keeps_them(uploaded_video):
    """The documented exception, off by default: with AUDIO_ONLY_INGEST off
    nothing is purged, and the accepted ceiling drops to the stored one."""
    container_key = uploaded_video.file_storage_key
    with override_settings(STAPEL_RECORDINGS=dict(
        _EXTRACTING_SETTINGS, AUDIO_ONLY_INGEST=False,
    )):
        _run_convert(uploaded_video)
        assert services.accepted_upload_limit() == LIMIT_2GIB
    uploaded_video.refresh_from_db()
    assert uploaded_video.file_storage_key == container_key
    assert get_storage().head_object(container_key)[0] is True


# ── the profile is what it says it is ────────────────────────────────────


def test_the_profile_is_mono_speech_audio():
    from stapel_recordings.normalize import audio_profile

    profile = audio_profile()
    assert (profile.channels, profile.sample_rate, profile.codec) == (1, 16000, "opus")
    assert (profile.ext, profile.content_type) == (".opus", "audio/ogg")
    assert profile.bytes_per_hour == 10_800_000

    with override_settings(STAPEL_RECORDINGS={"AUDIO_CODEC": "wav"}):
        pcm = audio_profile()
    assert pcm.ext == ".wav"
    # 16 kHz × 1ch × 2 bytes × 3600 — about 10.7x the Opus profile, which is
    # the whole reason Opus is the default.
    assert pcm.bytes_per_hour == 115_200_000


def test_ffmpeg_is_told_to_drop_video_and_downmix(monkeypatch):
    """The audio-only promise is one flag (``-vn``) and one number
    (``-ac 1``) — pin both, in the command that actually runs."""
    from stapel_recordings import normalize

    captured = {}

    class Proc:
        returncode = 0
        stdout = json.dumps(
            {"streams": [{"codec_type": "audio", "duration": "600"}], "format": {}}
        ).encode()
        stderr = b""

    def fake_run(cmd, **kwargs):
        captured.setdefault("cmds", []).append(cmd)
        return Proc()

    monkeypatch.setattr(normalize.subprocess, "run", fake_run)
    normalize.ffmpeg_normalize("in.mkv", "out.opus")
    cmd = captured["cmds"][-1]
    assert "-vn" in cmd
    assert cmd[cmd.index("-ac") + 1] == "1"
    assert cmd[cmd.index("-ar") + 1] == "16000"
    assert cmd[cmd.index("-c:a") + 1] == "libopus"
    assert cmd[cmd.index("-b:a") + 1] == "24000"


def test_two_channels_stay_two_when_a_host_asks(monkeypatch):
    """Mono is the default, not a hard-coded truth: a host whose diarizer
    separates speakers BY CHANNEL says so and pays for it in bytes."""
    from stapel_recordings import normalize

    captured = {}

    class Proc:
        returncode = 0
        stdout = json.dumps(
            {"streams": [{"codec_type": "audio", "duration": "60"}], "format": {}}
        ).encode()
        stderr = b""

    monkeypatch.setattr(
        normalize.subprocess, "run",
        lambda cmd, **kw: (captured.setdefault("cmds", []).append(cmd), Proc())[1],
    )
    with override_settings(STAPEL_RECORDINGS={"AUDIO_CHANNELS": 2}):
        normalize.ffmpeg_normalize("in.mkv", "out.opus")
    cmd = captured["cmds"][-1]
    assert cmd[cmd.index("-ac") + 1] == "2"


# ── the census reads and reports; it changes nothing ─────────────────────


def test_census_reports_stored_containers_without_touching_them(uploaded_video):
    from io import StringIO

    from django.core.management import call_command

    key = uploaded_video.file_storage_key
    uploaded_video.duration_seconds = 3600.0
    uploaded_video.save(update_fields=["duration_seconds"])

    out = StringIO()
    call_command("recordings_audio_census", "--json", stdout=out)
    report = json.loads(out.getvalue())

    assert report["containers"] == 1
    assert report["video_containers"] == 1
    assert report["container_bytes"] == 4_727_057_010
    assert report["projected_audio_bytes"] == 10_800_000  # one hour of Opus
    assert report["reclaimable_bytes"] == 4_727_057_010 - 10_800_000
    # Read-only: the object and the row are exactly as they were.
    assert get_storage().head_object(key)[0] is True
    uploaded_video.refresh_from_db()
    assert uploaded_video.file_storage_key == key
