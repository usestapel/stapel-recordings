"""System checks: storage E-level, pipeline/normalizer/threshold W-level."""
import pytest
from django.test import override_settings

from stapel_recordings.checks import (
    check_pipeline_stages,
    check_reconcile_threshold,
    check_storage_backend,
    check_transcribe_audio_url_ttl,
)

pytestmark = pytest.mark.django_db


def test_defaults_are_clean():
    assert check_storage_backend(None) == []
    assert check_pipeline_stages(None) == []
    assert check_reconcile_threshold(None) == []
    assert check_transcribe_audio_url_ttl(None) == []


def test_stuck_threshold_at_or_below_stage_timeout_is_warning():
    """Reconcile must not consider a still-running stage 'stuck' — the
    threshold has to exceed the longest stage duration."""
    with override_settings(STAPEL_RECORDINGS={"STUCK_THRESHOLD_SECONDS": 600}):
        warnings = check_reconcile_threshold(None)
    assert any(w.id == "stapel_recordings.W005" for w in warnings)


def test_audio_url_ttl_below_the_stage_timeout_is_warning():
    """With a private bucket the presigned audio URL is the provider's only
    way in — if it dies before the stage can, transcription fails on an
    expired signature and nothing says why."""
    with override_settings(STAPEL_RECORDINGS={"TRANSCRIBE_AUDIO_URL_TTL_SECONDS": 60}):
        warnings = check_transcribe_audio_url_ttl(None)
    assert any(w.id == "stapel_recordings.W007" for w in warnings)


def test_bad_storage_is_error():
    with override_settings(STAPEL_RECORDINGS={"STORAGE": "stapel_recordings.models.Recording"}):
        errors = check_storage_backend(None)
    assert any(e.id == "stapel_recordings.E002" for e in errors)


def test_unimportable_storage_is_error():
    with override_settings(STAPEL_RECORDINGS={"STORAGE": "nope.NoSuch"}):
        errors = check_storage_backend(None)
    assert any(e.id == "stapel_recordings.E001" for e in errors)


def test_unknown_pipeline_stage_is_warning():
    with override_settings(STAPEL_RECORDINGS={"PIPELINE": ["convert", "ghost"]}):
        warnings = check_pipeline_stages(None)
    assert any(w.id == "stapel_recordings.W002" for w in warnings)


def test_missing_taskstore_is_error():
    """No task store means the service cannot run, and this is caught at startup.

    The id is pinned: hosts silence and search checks by it, so changing it
    is a public contract change.
    """
    from stapel_recordings.checks import check_taskstore_installed

    with override_settings(INSTALLED_APPS=["stapel_recordings"]):
        errors = check_taskstore_installed(None)
    assert any(e.id == "stapel_recordings.E004" for e in errors)


def test_passthrough_normalizer_is_a_warning():
    """Selecting the passthrough normalizer disables ALL transcoding, and the
    seam check only ever asked whether NORMALIZER was callable — so turning
    conversion off passed `manage.py check` in silence."""
    from stapel_recordings.checks import check_normalizer_is_not_passthrough

    with override_settings(
        STAPEL_RECORDINGS={"NORMALIZER": "stapel_recordings.normalize.passthrough_normalize"}
    ):
        warnings = check_normalizer_is_not_passthrough(None)
    assert any(w.id == "stapel_recordings.W008" for w in warnings)
    # The seam check still says nothing — passthrough is callable.
    with override_settings(
        STAPEL_RECORDINGS={"NORMALIZER": "stapel_recordings.normalize.passthrough_normalize"}
    ):
        assert check_pipeline_stages(None) == []


def test_default_normalizer_is_not_warned_about():
    from stapel_recordings.checks import check_normalizer_is_not_passthrough

    assert check_normalizer_is_not_passthrough(None) == []


def test_check_ids_are_unique():
    """Two checks sharing one id is a silent trap, not cosmetics.

    Before 2026-08-08, ``stapel_recordings.E001`` was raised by TWO different
    checks: "STORAGE not importable" and "task store app missing". Silencing
    E001 for the first would have silently disabled the second too — which
    blocks startup for a service whose transcription cannot run at all.

    This guard reads the SOURCE, not a live run: a check that returns nothing
    under the current configuration still owns its id.
    """
    import ast
    import pathlib

    from stapel_recordings import checks as checks_module

    tree = ast.parse(pathlib.Path(checks_module.__file__).read_text())
    used: dict[str, list[str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        for inner in ast.walk(node):
            if not isinstance(inner, ast.Call):
                continue
            for kw in inner.keywords:
                if kw.arg == "id" and isinstance(kw.value, ast.Constant):
                    used.setdefault(str(kw.value.value), []).append(node.name)

    duplicates = {i: fns for i, fns in used.items() if len(set(fns)) > 1}
    assert not duplicates, f"one id shared by multiple checks: {duplicates}"


# ── audio-only ingest: the tooling and the wiring ────────────────────────


def test_ffmpeg_missing_is_an_error(monkeypatch):
    """ffmpeg's presence is an environment fact, and it used to be
    discovered at the first upload — as a failed recording someone was
    waiting for, not as a red deploy."""
    import shutil

    from stapel_recordings.checks import check_audio_extraction_tooling

    monkeypatch.setattr(shutil, "which", lambda name: None)
    errors = check_audio_extraction_tooling(None)
    assert [e.id for e in errors] == [
        "stapel_recordings.E006", "stapel_recordings.E006",
    ]
    assert "ffmpeg" in errors[0].msg


def test_an_ffmpeg_without_libopus_is_an_error(monkeypatch):
    """A slim image with a stripped ffmpeg build fails at exactly the same
    late moment as a missing binary, and for a reason nothing states."""
    import shutil
    import subprocess

    from stapel_recordings.checks import check_audio_extraction_tooling

    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")

    class Proc:
        stdout = b" V..... libx264\n A..... aac\n"
        stderr = b""
        returncode = 0

    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: Proc())
    errors = check_audio_extraction_tooling(None)
    assert [e.id for e in errors] == ["stapel_recordings.E006"]
    assert "libopus" in errors[0].msg


def test_the_tooling_check_is_silent_without_the_ffmpeg_normalizer(monkeypatch):
    """A host with its own NORMALIZER shells out to whatever it likes; this
    check has no opinion about that."""
    import shutil

    from stapel_recordings.checks import check_audio_extraction_tooling

    monkeypatch.setattr(shutil, "which", lambda name: None)
    with override_settings(
        STAPEL_RECORDINGS={"NORMALIZER": "stapel_recordings.normalize.passthrough_normalize"}
    ):
        assert check_audio_extraction_tooling(None) == []


def test_an_unwritable_audio_codec_is_an_error(monkeypatch):
    import shutil

    from stapel_recordings.checks import check_audio_extraction_tooling

    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
    with override_settings(STAPEL_RECORDINGS={"AUDIO_CODEC": "mp3"}):
        errors = check_audio_extraction_tooling(None)
    assert [e.id for e in errors] == ["stapel_recordings.E006"]


def test_audio_only_ingest_without_a_convert_stage_is_an_error():
    """The accepted-upload ceiling is eight times the stored one BECAUSE the
    container is discarded. Take the discarding away and the promise
    silently inverts."""
    from stapel_recordings.checks import check_audio_only_ingest_wiring

    assert check_audio_only_ingest_wiring(None) == []
    with override_settings(STAPEL_RECORDINGS={"PIPELINE": ["transcribe", "merge"]}):
        errors = check_audio_only_ingest_wiring(None)
    assert [e.id for e in errors] == ["stapel_recordings.E007"]
    assert "convert" in errors[0].msg


def test_audio_only_ingest_with_a_passthrough_normalizer_is_an_error():
    from stapel_recordings.checks import check_audio_only_ingest_wiring

    with override_settings(
        STAPEL_RECORDINGS={"NORMALIZER": "stapel_recordings.normalize.passthrough_normalize"}
    ):
        errors = check_audio_only_ingest_wiring(None)
    assert [e.id for e in errors] == ["stapel_recordings.E007"]


def test_the_wiring_check_is_silent_when_the_policy_is_deliberately_off():
    """Storing originals is a documented exception, not a misconfiguration —
    a host that states it should not be nagged."""
    from stapel_recordings.checks import check_audio_only_ingest_wiring

    with override_settings(STAPEL_RECORDINGS={
        "AUDIO_ONLY_INGEST": False,
        "NORMALIZER": "stapel_recordings.normalize.passthrough_normalize",
    }):
        assert check_audio_only_ingest_wiring(None) == []


def test_the_part_budget_is_checked_against_the_ACCEPTED_ceiling():
    """Checking MAX_UPLOAD_BYTES would pass a deployment whose every real
    upload fails: with extraction on, uploads are bounded by the container
    ceiling, which is eight times larger."""
    from stapel_recordings.checks import check_multipart_part_budget

    assert check_multipart_part_budget(None) == []
    with override_settings(STAPEL_RECORDINGS={"MAX_MULTIPART_PARTS": 1000}):
        errors = check_multipart_part_budget(None)
    assert [e.id for e in errors] == ["stapel_recordings.E005"]
