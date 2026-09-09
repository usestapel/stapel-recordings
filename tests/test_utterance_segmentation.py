"""The safety net must not have the defect it is a net for.

``_utterances_from_words`` is what builds segments when ``llm.transcribe``
returns words but no utterances — which is what every provider sends with
diarization off. It cut on the speaker changing and nothing else, so that
case produced one segment covering the whole meeting: on the owner's stand,
24 of 83 completed recordings render as a single segment and one ten-minute
meeting is a single 8592-character turn.

The rule here is numerically identical to stapel-agent's
``stt/segmentation.py`` — same thresholds, derived from the same 94 608 real
word gaps — because a transcript that arrives without utterances must not be
cut differently from one that arrives with them.
"""
from stapel_recordings import stages


def words(n, *, pause_every=0, pause=1.2, speaker=None, word=0.32, gap=0.05):
    out, t = [], 0.0
    for i in range(n):
        if pause_every and i and i % pause_every == 0:
            t += pause
        out.append({"text": f"word{i}", "start": round(t, 3),
                    "end": round(t + word, 3), "speaker": speaker})
        t += word + gap
    return out


class TestTheDefect:
    def test_a_speakerless_meeting_is_not_one_utterance(self):
        ws = words(1600, pause_every=40, pause=1.1)
        assert all(w["speaker"] is None for w in ws)

        out = stages._utterances_from_words(ws)

        assert len(out) > 1, "the whole meeting collapsed into one utterance"
        assert len(out) >= 30

    def test_gapless_unpunctuated_speech_still_cannot_wall(self):
        out = stages._utterances_from_words(words(4000))

        assert len(out) > 1
        assert all(len(u["text"]) <= stages.UTTERANCE_MAX_CHARS + 40 for u in out)

    def test_a_full_stop_cuts(self):
        ws = [
            {"text": "Good", "start": 0.0, "end": 0.3, "speaker": None},
            {"text": "morning", "start": 0.35, "end": 0.7, "speaker": None},
            {"text": "everyone", "start": 0.75, "end": 1.1, "speaker": None},
            {"text": "here.", "start": 1.15, "end": 1.5, "speaker": None},
            {"text": "First", "start": 1.55, "end": 1.9, "speaker": None},
            {"text": "item", "start": 1.95, "end": 2.3, "speaker": None},
            {"text": "is", "start": 2.35, "end": 2.6, "speaker": None},
            {"text": "billing", "start": 2.65, "end": 3.0, "speaker": None},
        ]
        out = stages._utterances_from_words(ws)

        assert [u["text"] for u in out] == [
            "Good morning everyone here.", "First item is billing",
        ]


class TestItStillRespectsSpeakers:
    def test_a_speaker_change_cuts(self):
        ws = (
            [{"text": "hello", "start": 0.0, "end": 0.4, "speaker": "a"}]
            + [{"text": "hi", "start": 0.5, "end": 0.9, "speaker": "b"}]
        )
        out = stages._utterances_from_words(ws)

        assert [u["speaker"] for u in out] == ["a", "b"]

    def test_short_bursts_are_not_shredded(self):
        ws = [
            {"text": "yes", "start": 0.0, "end": 0.3, "speaker": None},
            {"text": "absolutely", "start": 1.2, "end": 1.7, "speaker": None},
        ]
        assert len(stages._utterances_from_words(ws)) == 1

    def test_every_word_lands_in_exactly_one_utterance(self):
        ws = words(300, pause_every=25, pause=1.2)
        out = stages._utterances_from_words(ws)

        seen = [i for u in out for i in u["word_indexes"]]
        assert seen == list(range(len(ws)))


def test_the_thresholds_match_the_agents():
    """If these drift, one transcript is cut two different ways."""
    assert stages.UTTERANCE_GAP_SECONDS == 0.65
    assert stages.UTTERANCE_MAX_SECONDS == 30.0
    assert stages.UTTERANCE_MAX_CHARS == 500
    assert stages.UTTERANCE_MIN_SECONDS == 1.5
    assert stages.UTTERANCE_MIN_WORDS == 4
