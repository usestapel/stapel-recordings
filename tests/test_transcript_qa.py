"""``run_qa``'s gap check — long pauses between segments, reported, not failed.

Timing alone cannot tell a silent stretch from lost audio, so a pause over
the threshold is a WARN that says where and never clears ``passed``; the
audio-aware verdict lives under ``untranscribed_audio``.
"""
import pytest

from stapel_recordings.transcript_schema import (
    CHECK_UNTRANSCRIBED_AUDIO,
    MAX_SEGMENT_GAP_MS,
    NO_AUDIO_EVIDENCE,
    UnifiedSegment,
    UnifiedSpeaker,
    max_segment_gap_ms,
    recompute_qa,
    run_qa,
)


def _seg(idx, start_ms, end_ms):
    return UnifiedSegment(
        id=f"seg_{idx:04d}",
        start_ms=start_ms,
        end_ms=end_ms,
        speaker_id="spk_0",
        text="hello",
    )


def _qa(segments, duration_ms=600_000):
    return run_qa(
        segments=segments,
        speakers=[UnifiedSpeaker(speaker_id="spk_0")],
        duration_ms=duration_ms,
        diarization_requested=False,
    )


def test_the_threshold_is_a_named_constant():
    """An operator reads the number here, not out of a comparison."""
    assert MAX_SEGMENT_GAP_MS == 5000


def test_a_clean_transcript_passes():
    result = _qa([_seg(0, 0, 2000), _seg(1, 2100, 4000), _seg(2, 4500, 9000)])
    assert result.checks["gap"].startswith("PASS")
    assert result.passed is True


def test_a_seven_second_pause_is_a_warning_that_names_where():
    result = _qa([_seg(0, 0, 2000), _seg(1, 9000, 12000)])
    check = result.checks["gap"]
    assert check.startswith("WARN"), check
    # Says WHERE: both sides of the pause, and how big it is.
    assert "seg_0000" in check and "seg_0001" in check
    assert "7000" in check
    # Timing alone is not a failure: the pause may be silence.
    assert result.passed is True


def test_a_four_second_hole_is_not_a_finding():
    """Under the threshold is a pause, not dropped audio."""
    result = _qa([_seg(0, 0, 2000), _seg(1, 6000, 8000)])
    assert result.checks["gap"].startswith("PASS")
    assert result.passed is True


def test_a_gap_exactly_at_the_threshold_is_not_a_finding():
    result = _qa([_seg(0, 0, 2000), _seg(1, 2000 + MAX_SEGMENT_GAP_MS, 9000)])
    assert result.checks["gap"].startswith("PASS")
    assert result.passed is True


def test_back_to_back_segments_pass():
    result = _qa([_seg(0, 0, 2000), _seg(1, 2000, 4000), _seg(2, 4000, 6000)])
    assert result.checks["gap"] == "PASS: largest 0ms"
    assert result.passed is True


def test_overlapping_segments_are_not_gaps():
    """A negative delta must not read as a large one."""
    result = _qa([_seg(0, 0, 5000), _seg(1, 4950, 9000)])
    assert result.checks["gap"] == "PASS: largest 0ms"
    assert result.passed is True

    # A deep overlap is somebody else's finding (monotonicity), never a gap.
    deep = _qa([_seg(0, 0, 5000), _seg(1, 3000, 9000)])
    assert deep.checks["gap"] == "PASS: largest 0ms"


def test_an_empty_transcript_has_no_gaps():
    result = _qa([])
    assert result.checks["gap"] == "SKIP"
    assert result.passed is True


def test_a_single_segment_transcript_has_no_gaps():
    result = _qa([_seg(0, 0, 2000)])
    assert result.checks["gap"] == "SKIP"
    assert result.passed is True


def test_the_finding_counts_every_pause_and_names_the_longest():
    """Two pauses are counted, the first is named, the longest is measured."""
    result = _qa(
        [_seg(0, 0, 1000), _seg(1, 20_000, 21_000), _seg(2, 60_000, 61_000)]
    )
    check = result.checks["gap"]
    assert check.startswith("WARN: 2 gap(s)"), check
    assert "seg_0000" in check and "seg_0001" in check
    assert "longest 39000ms" in check
    assert result.passed is True


def test_audio_was_not_evaluated_and_says_so():
    """Without a speech-activity map the audio check is never a silent PASS."""
    result = _qa([_seg(0, 0, 2000), _seg(1, 2100, 4000)])
    assert result.checks[CHECK_UNTRANSCRIBED_AUDIO] == NO_AUDIO_EVIDENCE
    assert CHECK_UNTRANSCRIBED_AUDIO == "untranscribed_audio"


def test_other_failures_still_clear_passed():
    """Only the gap stopped failing; an overlap still fails monotonicity."""
    result = _qa([_seg(0, 0, 5000), _seg(1, 1000, 9000)])
    assert result.checks["monotonicity"].startswith("FAIL")
    assert result.passed is False


def test_the_threshold_follows_the_agent_setting(settings):
    """One threshold source: the agent's STT_QA, read by both packages."""
    pytest.importorskip("stapel_agent")
    settings.STAPEL_AGENT = {"STT_QA": {"MAX_GAP_SECONDS": 10}}
    assert max_segment_gap_ms() == 10_000
    result = _qa([_seg(0, 0, 2000), _seg(1, 9000, 12000)])
    assert result.checks["gap"].startswith("PASS"), result.checks["gap"]

    settings.STAPEL_AGENT = {"STT_QA": {"MAX_GAP_SECONDS": 0}}
    assert _qa([_seg(0, 0, 2000), _seg(1, 90_000, 92_000)]).checks["gap"] == "SKIP"


def test_a_stored_verdict_is_recomputed_under_the_current_rules():
    """A transcript stored with the old FAIL is served with today's verdict."""
    stored = {
        "duration_ms": 30_000,
        "segments": [
            {"id": "seg_0000", "start_ms": 0, "end_ms": 2000, "speaker_id": "spk_0", "text": "a"},
            {"id": "seg_0001", "start_ms": 9000, "end_ms": 12000, "speaker_id": "spk_0", "text": "b"},
        ],
        "speakers": [{"speaker_id": "spk_0"}],
        "qa": {"passed": False, "checks": {"gap": "FAIL: 1 gap(s) > 5000ms"}},
    }
    qa = recompute_qa(stored, diarization_requested=True)
    assert qa["passed"] is True
    assert qa["checks"]["gap"].startswith("WARN: 1 gap(s)")
    assert qa["checks"]["diarization_present"] == "PASS: 1"
    assert qa["checks"][CHECK_UNTRANSCRIBED_AUDIO] == NO_AUDIO_EVIDENCE


def test_recompute_tolerates_a_bare_stored_transcript():
    qa = recompute_qa({}, diarization_requested=False)
    assert qa["passed"] is True
    assert qa["checks"]["gap"] == "SKIP"


@pytest.mark.django_db
def test_a_built_transcript_carries_the_check(make_recording):
    """The check travels in the stored transcript, not only in a direct call."""
    from stapel_recordings.transcript_schema import from_db_segments

    r = make_recording(duration_seconds=10.0, diarization_enabled=False)
    transcript = from_db_segments(r)
    assert "gap" in transcript.qa.checks
    assert transcript.to_dict()["qa"]["checks"]["gap"] == "SKIP"
