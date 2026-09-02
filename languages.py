"""ISO 639 language-tag normalization.

Speech-to-text providers report a recording's language as ISO 639-2/3 —
three letters (``rus``, ``eng``, ``spa``, ``zho``) — while every
language-keyed map in this package (``VECTOR["FTS_CONFIGS"]`` above all)
is written in ISO 639-1, the two-letter alphabet a human reaches for. Left
unreconciled, that mismatch is silent: a lookup misses, a fallback engages,
and the only symptom is worse results. On one live deployment 64 of 70
recordings fell through to unstemmed ``simple`` this way.

:func:`to_iso639_1` is the one place that reconciles them. It is
deliberately total and lossless-on-failure: an unknown tag comes back
unchanged (lower-cased, regional subtag dropped) so callers keep their own
fallback, and no caller has to know which alphabet it was handed.

The table is the FULL ISO 639-2 set, generated — never hand-typed. Both
alphabet-3 forms map to the same two-letter code for the twenty languages
where the bibliographic and terminological codes differ (``ger``/``deu``
-> ``de``, ``fre``/``fra`` -> ``fr``, ``chi``/``zho`` -> ``zh``,
``dut``/``nld`` -> ``nl``), which is exactly the class of case a
hand-written shortlist gets wrong. Regenerate with
``python3 tools/gen_iso639.py`` (see that script for provenance).
"""
from __future__ import annotations

#: 3-letter ISO 639-2/3 code -> 2-letter ISO 639-1 code. Generated from the
#: ISO 639 registry; see tools/gen_iso639.py. Only codes that HAVE a
#: two-letter equivalent are listed (most of ISO 639-3 has none).
ISO639_2_TO_1: dict[str, str] = {
    "aar": "aa", "abk": "ab", "afr": "af", "aka": "ak", "alb": "sq", "amh": "am",
    "ara": "ar", "arg": "an", "arm": "hy", "asm": "as", "ava": "av", "ave": "ae",
    "aym": "ay", "aze": "az", "bak": "ba", "bam": "bm", "baq": "eu", "bel": "be",
    "ben": "bn", "bis": "bi", "bod": "bo", "bos": "bs", "bre": "br", "bul": "bg",
    "bur": "my", "cat": "ca", "ces": "cs", "cha": "ch", "che": "ce", "chi": "zh",
    "chu": "cu", "chv": "cv", "cor": "kw", "cos": "co", "cre": "cr", "cym": "cy",
    "cze": "cs", "dan": "da", "deu": "de", "div": "dv", "dut": "nl", "dzo": "dz",
    "ell": "el", "eng": "en", "epo": "eo", "est": "et", "eus": "eu", "ewe": "ee",
    "fao": "fo", "fas": "fa", "fij": "fj", "fin": "fi", "fra": "fr", "fre": "fr",
    "fry": "fy", "ful": "ff", "geo": "ka", "ger": "de", "gla": "gd", "gle": "ga",
    "glg": "gl", "glv": "gv", "gre": "el", "grn": "gn", "guj": "gu", "hat": "ht",
    "hau": "ha", "hbs": "sh", "heb": "he", "her": "hz", "hin": "hi", "hmo": "ho",
    "hrv": "hr", "hun": "hu", "hye": "hy", "ibo": "ig", "ice": "is", "ido": "io",
    "iii": "ii", "iku": "iu", "ile": "ie", "ina": "ia", "ind": "id", "ipk": "ik",
    "isl": "is", "ita": "it", "jav": "jv", "jpn": "ja", "kal": "kl", "kan": "kn",
    "kas": "ks", "kat": "ka", "kau": "kr", "kaz": "kk", "khm": "km", "kik": "ki",
    "kin": "rw", "kir": "ky", "kom": "kv", "kon": "kg", "kor": "ko", "kua": "kj",
    "kur": "ku", "lao": "lo", "lat": "la", "lav": "lv", "lim": "li", "lin": "ln",
    "lit": "lt", "ltz": "lb", "lub": "lu", "lug": "lg", "mac": "mk", "mah": "mh",
    "mal": "ml", "mao": "mi", "mar": "mr", "may": "ms", "mkd": "mk", "mlg": "mg",
    "mlt": "mt", "mon": "mn", "mri": "mi", "msa": "ms", "mya": "my", "nau": "na",
    "nav": "nv", "nbl": "nr", "nde": "nd", "ndo": "ng", "nep": "ne", "nld": "nl",
    "nno": "nn", "nob": "nb", "nor": "no", "nya": "ny", "oci": "oc", "oji": "oj",
    "ori": "or", "orm": "om", "oss": "os", "pan": "pa", "per": "fa", "pli": "pi",
    "pol": "pl", "por": "pt", "pus": "ps", "que": "qu", "roh": "rm", "ron": "ro",
    "rum": "ro", "run": "rn", "rus": "ru", "sag": "sg", "san": "sa", "sin": "si",
    "slk": "sk", "slo": "sk", "slv": "sl", "sme": "se", "smo": "sm", "sna": "sn",
    "snd": "sd", "som": "so", "sot": "st", "spa": "es", "sqi": "sq", "srd": "sc",
    "srp": "sr", "ssw": "ss", "sun": "su", "swa": "sw", "swe": "sv", "tah": "ty",
    "tam": "ta", "tat": "tt", "tel": "te", "tgk": "tg", "tgl": "tl", "tha": "th",
    "tib": "bo", "tir": "ti", "ton": "to", "tsn": "tn", "tso": "ts", "tuk": "tk",
    "tur": "tr", "twi": "tw", "uig": "ug", "ukr": "uk", "urd": "ur", "uzb": "uz",
    "ven": "ve", "vie": "vi", "vol": "vo", "wel": "cy", "wln": "wa", "wol": "wo",
    "xho": "xh", "yid": "yi", "yor": "yo", "zha": "za", "zho": "zh", "zul": "zu",
}


def to_iso639_1(language: str | None) -> str:
    """Normalize a language tag to its lower-case ISO 639-1 subtag.

    Drops any regional/script subtag (``de-CH`` -> ``de``, ``rus-RU`` ->
    ``ru``) and maps ISO 639-2/3 to ISO 639-1 (``rus`` -> ``ru``). A tag
    that is already two letters passes through; a three-letter code with no
    639-1 equivalent (``zho`` has one, ``haw`` does not) and anything
    unrecognized come back lower-cased and otherwise unchanged, so the
    caller's own fallback still decides. Empty/None -> ``""``.
    """
    primary = (language or "").split("-")[0].split("_")[0].strip().lower()
    if len(primary) == 3:
        return ISO639_2_TO_1.get(primary, primary)
    return primary


__all__ = ["ISO639_2_TO_1", "to_iso639_1"]
