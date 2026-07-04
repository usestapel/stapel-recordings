"""Model + transcript-schema sanity."""
import pytest

from stapel_recordings.models import RecordingStatus, Speaker

pytestmark = pytest.mark.django_db


def test_recording_str(make_recording):
    r = make_recording(title="Weekly")
    assert "Weekly" in str(r)
    assert r.status in RecordingStatus.values


def test_speaker_palette_wraps():
    assert Speaker.color_for_index(0) == Speaker.SPEAKER_PALETTE[0]
    assert Speaker.color_for_index(len(Speaker.SPEAKER_PALETTE)) == Speaker.SPEAKER_PALETTE[0]


def test_from_db_segments_builds_transcript(make_recording):
    from stapel_recordings.models import Segment
    from stapel_recordings.transcript_schema import from_db_segments, render_markdown

    r = make_recording(language="en", duration_seconds=4.0, diarization_enabled=False)
    sp = Speaker.objects.create(recording=r, label="speaker_0", display_name="Alice")
    Segment.objects.create(
        recording=r, speaker=sp, sequence_num=0, start_time=0.0, end_time=2.0, text="hello"
    )
    transcript = from_db_segments(r)
    assert transcript.meeting_id == str(r.id)
    assert transcript.duration_ms == 4000
    assert len(transcript.segments) == 1
    assert transcript.qa.passed is True
    md = render_markdown(transcript)
    assert "Alice: hello" in md


def test_build_summary_input_chunks(make_recording):
    from stapel_recordings.models import Segment
    from stapel_recordings.transcript_schema import build_summary_input, from_db_segments

    r = make_recording(language="en", duration_seconds=100.0)
    sp = Speaker.objects.create(recording=r, label="speaker_0", display_name="Bob")
    for i in range(5):
        Segment.objects.create(
            recording=r, speaker=sp, sequence_num=i,
            start_time=float(i), end_time=float(i) + 1, text=f"line {i}",
        )
    transcript = from_db_segments(r)
    out = build_summary_input(transcript, tokens_per_chunk=1)  # force multiple chunks
    assert out["meta"]["total_segments"] == 5
    assert out["meta"]["chunks_count"] >= 2
    assert all("anchors" in c for c in out["chunks"])
