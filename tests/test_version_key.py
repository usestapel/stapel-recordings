"""The transcript version key: what it must notice, and what it must ignore.

A version key that is too sensitive is not a mild inconvenience — it marks
every derived artifact stale on every save, so a real staleness signal becomes
indistinguishable from noise and gets ignored. A key that is too insensitive
lets a summary keep quoting a turn that was edited out. Both halves are
therefore tested, and the "ignores" half is the longer one on purpose.
"""
from dataclasses import fields

import pytest

from stapel_recordings.models import Segment, Speaker
from stapel_recordings.transcript_schema import (
    _CONTENT_LANGUAGE,
    _CONTENT_SEGMENT,
    _CONTENT_SPEAKER,
    _CONTENT_TRANSCRIPT_COMPOSITES,
    _CONTENT_TRANSCRIPT_SCALARS,
    _NON_CONTENT_LANGUAGE,
    _NON_CONTENT_SEGMENT,
    _NON_CONTENT_SPEAKER,
    _NON_CONTENT_TRANSCRIPT,
    LanguageMeta,
    UnifiedSegment,
    UnifiedSpeaker,
    UnifiedTranscript,
    from_db_segments,
    transcript_hash,
)

pytestmark = pytest.mark.django_db


# ─── The classification must cover the schema ──────────────────────────

CLASSIFICATIONS = [
    (
        UnifiedTranscript,
        _CONTENT_TRANSCRIPT_SCALARS | _CONTENT_TRANSCRIPT_COMPOSITES,
        _NON_CONTENT_TRANSCRIPT,
    ),
    (LanguageMeta, _CONTENT_LANGUAGE, _NON_CONTENT_LANGUAGE),
    (UnifiedSpeaker, _CONTENT_SPEAKER, _NON_CONTENT_SPEAKER),
    (UnifiedSegment, _CONTENT_SEGMENT, _NON_CONTENT_SEGMENT),
]


@pytest.mark.parametrize(
    "cls,content,non_content", CLASSIFICATIONS, ids=lambda a: getattr(a, "__name__", "")
)
def test_every_field_is_classified(cls, content, non_content):
    """Adding a field to the schema is a decision about the version key.

    Without this, a new field defaults into "not content" by being forgotten —
    which is the wrong default: a field nobody classified is a field nobody
    thought about, and if it turns out to be rendered to the model, edits to it
    silently stop invalidating anything.
    """
    declared = {f.name for f in fields(cls)}
    classified = set(content) | set(non_content)
    assert classified == declared, (
        f"{cls.__name__}: unclassified {declared - classified}, "
        f"unknown {classified - declared}"
    )


@pytest.mark.parametrize(
    "cls,content,non_content", CLASSIFICATIONS, ids=lambda a: getattr(a, "__name__", "")
)
def test_no_field_is_classified_both_ways(cls, content, non_content):
    assert not (set(content) & set(non_content))


# ─── Fixtures ──────────────────────────────────────────────────────────


def _transcript(**overrides) -> UnifiedTranscript:
    base = dict(
        schema_version="1.0",
        meeting_id="m-1",
        duration_ms=4000,
        language=LanguageMeta(routed="en", detected="en", path="A"),
        engine=None,
        segments=[
            UnifiedSegment(
                id="seg_0000", start_ms=0, end_ms=2000, speaker_id="spk_0", text="hello"
            ),
            UnifiedSegment(
                id="seg_0001", start_ms=2000, end_ms=4000, speaker_id="spk_1", text="hi"
            ),
        ],
        speakers=[
            UnifiedSpeaker(speaker_id="spk_0", db_id="a", name="Alice", color="#111111"),
            UnifiedSpeaker(speaker_id="spk_1", db_id="b", name="Bob", color="#222222"),
        ],
        qa=None,
    )
    base.update(overrides)
    return UnifiedTranscript(**base)


def _with_segment(index: int, **changes) -> UnifiedTranscript:
    t = _transcript()
    seg = t.segments[index]
    for k, v in changes.items():
        setattr(seg, k, v)
    return t


def _with_speaker(index: int, **changes) -> UnifiedTranscript:
    t = _transcript()
    spk = t.speakers[index]
    for k, v in changes.items():
        setattr(spk, k, v)
    return t


# ─── What it must notice ───────────────────────────────────────────────


@pytest.mark.parametrize(
    "changed,why",
    [
        (_transcript(meeting_id="m-2"), "a different recording is a different key"),
        (_transcript(schema_version="2.0"), "the canonical shape itself changed"),
        (_transcript(duration_ms=9000), "duration is rendered in the header"),
        (
            _transcript(language=LanguageMeta(routed="ru", detected="ru", path="A")),
            "language is rendered in the header",
        ),
        (_with_segment(0, text="goodbye"), "the turn text changed"),
        (_with_segment(0, start_ms=500), "the rendered timestamp moved"),
        (_with_segment(0, end_ms=2500), "the anchor span moved"),
        (_with_segment(0, speaker_id="spk_1"), "the turn was reattributed"),
        (_with_segment(0, id="seg_9999"), "the anchor id changed"),
        (_with_speaker(0, name="Alicia"), "a rename changes what the model read"),
        (_with_speaker(0, speaker_id="spk_9"), "speaker ids are what segments reference"),
    ],
)
def test_key_changes(changed, why):
    assert transcript_hash(changed) != transcript_hash(_transcript()), why


def test_reordering_turns_changes_the_key():
    """Order is meaning: anchors are positional and the narrative is sequential."""
    t = _transcript()
    swapped = _transcript()
    swapped.segments = list(reversed(swapped.segments))
    assert transcript_hash(swapped) != transcript_hash(t)


# ─── What it must ignore ───────────────────────────────────────────────


@pytest.mark.parametrize(
    "changed,why",
    [
        (_transcript(qa="anything"), "a verdict about the transcript is not the transcript"),
        (_transcript(engine="whisper-v9"), "provenance label; a real re-run moves the segments"),
        (_with_speaker(0, color="#FF0000"), "presentation never reached the model"),
        (_with_speaker(0, db_id="other-uuid"), "a join key never reached the model"),
        (_with_segment(0, words=[{"w": "hello", "start_ms": 0, "end_ms": 100}]),
         "no anchor points into the word grid"),
        (_with_segment(0, lang="de"), "per-segment detection metadata is not rendered"),
    ],
)
def test_key_is_unchanged(changed, why):
    assert transcript_hash(changed) == transcript_hash(_transcript()), why


def test_language_provenance_is_not_content():
    """How the language was chosen ("A" auto vs "B" user) never reached the model."""
    a = _transcript(language=LanguageMeta(routed="en", detected="en", path="A"))
    b = _transcript(language=LanguageMeta(routed="en", detected="en", path="B"))
    assert transcript_hash(a) == transcript_hash(b)


# ─── Against the database, where the trap lives ────────────────────────


def _recording_with_transcript(make_recording):
    r = make_recording(language="en", duration_seconds=4.0, diarization_enabled=True)
    alice = Speaker.objects.create(recording=r, label="speaker_0", display_name="Alice")
    bob = Speaker.objects.create(recording=r, label="speaker_1", display_name="Bob")
    Segment.objects.create(
        recording=r, speaker=alice, sequence_num=0, start_time=0.0, end_time=2.0, text="hello"
    )
    Segment.objects.create(
        recording=r, speaker=bob, sequence_num=1, start_time=2.0, end_time=4.0, text="hi"
    )
    return r


def test_key_is_stable_across_reads(make_recording):
    r = _recording_with_transcript(make_recording)
    assert transcript_hash(from_db_segments(r)) == transcript_hash(from_db_segments(r))


def test_renaming_the_recording_does_not_invalidate_derived_work(make_recording):
    """The trap this whole design exists to avoid.

    Hashing the recording row wholesale — title, status, ``updated_at`` — is
    the obvious implementation and it is wrong: ``updated_at`` is ``auto_now``,
    so every save would mint a new key and every summary and every user
    correction would read as stale forever, with no way to tell a real edit
    from a touched row.
    """
    r = _recording_with_transcript(make_recording)
    before = transcript_hash(from_db_segments(r))

    r.title = "Renamed after the fact"
    r.save()
    r.refresh_from_db()

    assert transcript_hash(from_db_segments(r)) == before


def test_editing_a_turn_does_invalidate_derived_work(make_recording):
    r = _recording_with_transcript(make_recording)
    before = transcript_hash(from_db_segments(r))

    seg = r.segments.order_by("sequence_num").first()
    seg.text = "hello, actually"
    seg.is_edited = True
    seg.save()

    assert transcript_hash(from_db_segments(r)) != before


def test_speaker_ids_do_not_depend_on_row_order(make_recording):
    """``spk_N`` is positional, so the queryset behind it must be ordered.

    Without an ORDER BY the database may return speakers in any order, and the
    same untouched recording canonicalizes two different ways between reads —
    one transcript, two keys. This asserts the ordering is by ``label``, not by
    insertion.
    """
    r = make_recording(language="en", duration_seconds=4.0, diarization_enabled=True)
    # Inserted out of label order on purpose.
    zoe = Speaker.objects.create(recording=r, label="speaker_1", display_name="Zoe")
    amy = Speaker.objects.create(recording=r, label="speaker_0", display_name="Amy")
    Segment.objects.create(
        recording=r, speaker=amy, sequence_num=0, start_time=0.0, end_time=2.0, text="a"
    )
    Segment.objects.create(
        recording=r, speaker=zoe, sequence_num=1, start_time=2.0, end_time=4.0, text="z"
    )

    transcript = from_db_segments(r)
    by_id = {s.speaker_id: s.name for s in transcript.speakers}
    assert by_id["spk_0"] == "Amy", "speaker_0 must canonicalize first regardless of insertion"
    assert by_id["spk_1"] == "Zoe"
