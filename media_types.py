"""Content sniffing for uploaded objects.

An extension allowlist describes what the *client called* the file; it says
nothing about what was actually written to the bucket through the presigned
URL. The two are independent inputs, and only the second one is what a
downstream stage will open, hand to ffmpeg, or serve back to a browser — so
the bytes get their own gate.

The gate is deliberately two-tiered instead of a single "is this audio?"
question, because a byte-level yes/no does not exist for media: the allowlist
covers container formats whose signatures are well known (RIFF/WAVE, OggS,
fLaC, ftyp, EBML, ID3, ADTS…) *and* raw streams that legitimately begin with
arbitrary bytes. A strict "must look like known media" rule would therefore
reject valid uploads in some deployments, while a permissive "anything goes"
rule keeps the interesting case open: a Windows executable, an ELF binary, a
zip, or an HTML/script polyglot parked under an ``audio.mp3`` key inside a
bucket that some deployment serves publicly.

So the default policy (:data:`POLICY_REJECT_KNOWN_BAD`) rejects prefixes that
positively identify a **non-media, actively dangerous** type and accepts
everything else, and a stricter deployment flips one setting to
:data:`POLICY_REQUIRE_KNOWN_MEDIA` and accepts only recognized media
containers. ``off`` exists for hosts whose storage the library cannot read a
prefix from at all.
"""
from __future__ import annotations

#: Accept anything that is not positively identified as a dangerous
#: non-media type. The default: closes the executable/archive/markup class
#: without rejecting exotic-but-valid audio.
POLICY_REJECT_KNOWN_BAD = "reject_known_bad"

#: Accept only prefixes matching a known media container signature.
POLICY_REQUIRE_KNOWN_MEDIA = "require_known_media"

#: No content gate at all.
POLICY_OFF = "off"

POLICIES = (POLICY_REJECT_KNOWN_BAD, POLICY_REQUIRE_KNOWN_MEDIA, POLICY_OFF)

#: How many leading bytes are enough to classify. ``ftyp`` sits at offset 4
#: and the longest signature below is 12 bytes; 64 leaves room for future
#: signatures without ever pulling a meaningful slice of user audio.
PREFIX_BYTES = 64

#: (offset, magic, label) of container formats this module recognizes as
#: media. Not exhaustive by design — an unrecognized prefix is "unknown",
#: not "bad".
MEDIA_SIGNATURES: tuple[tuple[int, bytes, str], ...] = (
    (0, b"ID3", "mp3/id3"),
    (0, b"\xff\xfb", "mp3"),
    (0, b"\xff\xf3", "mp3"),
    (0, b"\xff\xf2", "mp3"),
    (0, b"\xff\xfa", "mp3"),
    (0, b"\xff\xf1", "aac/adts"),
    (0, b"\xff\xf9", "aac/adts"),
    (0, b"ADIF", "aac/adif"),
    (0, b"RIFF", "riff (wav/avi)"),
    (0, b"OggS", "ogg"),
    (0, b"fLaC", "flac"),
    (0, b"FORM", "aiff"),
    (0, b"\x1a\x45\xdf\xa3", "matroska/webm"),
    (0, b"\x30\x26\xb2\x75", "asf/wma"),
    (0, b"#!AMR", "amr"),
    (0, b".snd", "au"),
    (0, b"MThd", "midi"),
    (4, b"ftyp", "iso-bmff (mp4/m4a/mov/3gp)"),
    (0, b"\x00\x00\x01\xba", "mpeg-ps"),
    (0, b"\x00\x00\x01\xb3", "mpeg-video"),
    (0, b"\x47", "mpeg-ts"),
)

#: (offset, magic, label) of types that are never a recording and are
#: dangerous where objects can be fetched by a browser or a worker.
DANGEROUS_SIGNATURES: tuple[tuple[int, bytes, str], ...] = (
    (0, b"MZ", "dos/windows executable"),
    (0, b"\x7fELF", "elf executable"),
    (0, b"\xca\xfe\xba\xbe", "mach-o fat / java class"),
    (0, b"\xcf\xfa\xed\xfe", "mach-o executable"),
    (0, b"\xce\xfa\xed\xfe", "mach-o executable"),
    (0, b"PK\x03\x04", "zip archive"),
    (0, b"PK\x05\x06", "zip archive"),
    (0, b"Rar!", "rar archive"),
    (0, b"7z\xbc\xaf\x27\x1c", "7z archive"),
    (0, b"\x1f\x8b", "gzip archive"),
    (0, b"BZh", "bzip2 archive"),
    (0, b"\xfd7zXZ\x00", "xz archive"),
    (0, b"%PDF", "pdf document"),
    (0, b"\xd0\xcf\x11\xe0", "ole compound document"),
    (0, b"#!", "shell script"),
    (0, b"<?php", "php source"),
    (0, b"\xed\xab\xee\xdb", "rpm package"),
)

#: Markup that a browser may render (and therefore execute) when a bucket
#: serves objects publicly. Matched case-insensitively anywhere in the
#: prefix, since HTML tolerates leading whitespace/BOM/comments.
DANGEROUS_MARKUP: tuple[tuple[bytes, str], ...] = (
    (b"<!doctype html", "html document"),
    (b"<html", "html document"),
    (b"<script", "html/script"),
    (b"<svg", "svg document"),
    (b"<?xml", "xml document"),
)

#: Result of :func:`classify_prefix` when nothing matched either table.
UNKNOWN = "unknown"

#: Result of :func:`classify_prefix` for a recognized media container.
MEDIA = "media"

#: Result of :func:`classify_prefix` for a positively dangerous type.
DANGEROUS = "dangerous"


class UnsupportedUploadContent(ValueError):
    """The stored object's leading bytes are not acceptable under the
    configured content policy."""

    def __init__(self, label: str, policy: str):
        super().__init__(f"upload content rejected: {label} (policy {policy})")
        self.label = label
        self.policy = policy


def _matches(prefix: bytes, offset: int, magic: bytes) -> bool:
    return prefix[offset : offset + len(magic)] == magic


def classify_prefix(prefix: bytes) -> tuple[str, str]:
    """Classify the leading bytes of an object.

    Returns ``(verdict, label)`` where verdict is :data:`MEDIA`,
    :data:`DANGEROUS` or :data:`UNKNOWN`. Dangerous wins over media: a
    polyglot that satisfies both tables is exactly the file this gate is
    for.
    """
    if not prefix:
        return UNKNOWN, "empty"
    lowered = prefix.lower()
    for needle, label in DANGEROUS_MARKUP:
        if needle in lowered:
            return DANGEROUS, label
    for offset, magic, label in DANGEROUS_SIGNATURES:
        if _matches(prefix, offset, magic):
            return DANGEROUS, label
    for offset, magic, label in MEDIA_SIGNATURES:
        if _matches(prefix, offset, magic):
            return MEDIA, label
    return UNKNOWN, "unrecognized"


def check_prefix(prefix: bytes, *, policy: str) -> str:
    """Apply *policy* to *prefix*; raise :class:`UnsupportedUploadContent`
    when the content is not acceptable. Returns the matched label."""
    if policy == POLICY_OFF:
        return "unchecked"
    verdict, label = classify_prefix(prefix)
    if verdict == DANGEROUS:
        raise UnsupportedUploadContent(label, policy)
    if policy == POLICY_REQUIRE_KNOWN_MEDIA and verdict != MEDIA:
        raise UnsupportedUploadContent(label, policy)
    return label


__all__ = [
    "POLICY_REJECT_KNOWN_BAD",
    "POLICY_REQUIRE_KNOWN_MEDIA",
    "POLICY_OFF",
    "POLICIES",
    "PREFIX_BYTES",
    "MEDIA",
    "DANGEROUS",
    "UNKNOWN",
    "UnsupportedUploadContent",
    "classify_prefix",
    "check_prefix",
]
