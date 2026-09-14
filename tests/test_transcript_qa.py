"""``run_qa``'s gap check — transcription that dropped audio leaves a hole.

Nothing else in the pipeline notices a minute of missing speech: the
segments are monotonic, they fit inside the duration, the row count looks
healthy. The only witness is the distance between one segment's end and the
next one's start.
"""
import pytest

from stapel_recordings.transcript_schema import (
    MAX_SEGMENT_GAP_MS,
    UnifiedSegment,
    UnifiedSpeaker,
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


def test_a_seven_second_hole_is_caught_and_named():
    result = _qa([_seg(0, 0, 2000), _seg(1, 9000, 12000)])
    check = result.checks["gap"]
    assert check.startswith("FAIL"), check
    # Says WHERE: both sides of the hole, and how big it is.
    assert "seg_0000" in check and "seg_0001" in check
    assert "7000" in check
    assert result.passed is False


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


def test_the_finding_counts_every_hole():
    """Two holes are two findings, and the first one is the one named."""
    result = _qa(
        [_seg(0, 0, 1000), _seg(1, 20_000, 21_000), _seg(2, 40_000, 41_000)]
    )
    check = result.checks["gap"]
    assert check.startswith("FAIL: 2 gap(s)"), check
    assert "seg_0000" in check and "seg_0001" in check


@pytest.mark.django_db
def test_a_built_transcript_carries_the_check(make_recording):
    """The check travels in the stored transcript, not only in a direct call."""
    from stapel_recordings.transcript_schema import from_db_segments

    r = make_recording(duration_seconds=10.0, diarization_enabled=False)
    transcript = from_db_segments(r)
    assert "gap" in transcript.qa.checks
    assert transcript.to_dict()["qa"]["checks"]["gap"] == "SKIP"
