"""Settings namespace for stapel-recordings.

All configuration is read through ``recordings_settings`` (lazily, at call
time) — never via module-level ``os.getenv`` (values would freeze at import).
Resolution order per key: ``settings.STAPEL_RECORDINGS`` dict -> flat Django
setting of the same name -> environment variable -> default below.

Dotted-path keys listed in ``import_strings`` are resolved with
``import_string`` — the fork-free escape hatch for swappable behavior
(the STORAGE / NORMALIZER strategies and the PIPELINE_RESOLVER seam).

The flagship extension point is the **pipeline**: an ordered list of stage
names (``PIPELINE``) run by a generic driver over an open stage registry
(``STAGES`` overlay + ``register_stage`` runtime API). Reorder, subset,
insert or replace stages without forking. See MODULE.md.
"""
from stapel_core.conf import AppSettings

#: Default pipeline: the five built-in stages in canonical order. Hosts
#: override with STAPEL_RECORDINGS["PIPELINE"] (reorder / subset / extend)
#: or a PIPELINE_RESOLVER for runtime/per-recording definitions. ``embed``
#: is a no-op unless the opt-in vector app is installed AND enabled (the
#: DiarizeStage pattern) — its presence costs nothing.
DEFAULT_PIPELINE = ("convert", "transcribe", "diarize", "merge", "embed")

#: Defaults for the opt-in vector/search layer (``stapel_recordings.vector``
#: app + the ``[vector]`` extra). One nested block, not top-level keys: the
#: whole layer is optional and everything in here is tuning for it. Hosts
#: override any subset — :func:`vector_config` merges their block over these
#: defaults (one level deep for the nested dicts), so a host sets only what
#: it changes. Know-how (which embedding model, chunking, ranking weights)
#: deliberately stays host-side via this block.
DEFAULT_VECTOR = {
    # Master switch for the embed stage. The stage additionally requires
    # "stapel_recordings.vector" in INSTALLED_APPS — without both it is a
    # no-op (checked: W006 warns on ENABLED without the app).
    "ENABLED": False,
    # Embedding dimensionality — must match what llm.embed returns for the
    # chosen MODEL. Read by the vector app's models AND its migration, so
    # set it before the first migrate; changing it later means a host-side
    # migration + re-embed.
    "DIM": 1536,
    # Embedding model name forwarded to llm.embed ("" = agent default) and
    # stored on every embedding row. Pin it to make re-embeds per model
    # explicit.
    "MODEL": "",
    # Optional provider override forwarded to llm.embed ("" = agent default).
    "PROVIDER": "",
    # Vector arm: restrict ANN candidates to rows stamped with the model
    # that embedded the query (the model llm.embed REPORTS, not the MODEL
    # pin above — they can differ in spelling, and "" means "agent
    # default"). Vectors from two models are two incomparable spaces of
    # the same width; mixing them makes cosine ranking silently garbage.
    # Set False only to deliberately search across models (e.g. mid
    # re-embed) — the reindex path is the `recordings_reembed` command.
    "SEARCH_MODEL_FILTER": True,
    # Texts per llm.embed call.
    "BATCH_SIZE": 64,
    # timeout_seconds forwarded to llm.embed.
    "TIMEOUT_SECONDS": 120,
    # Recording summaries are chunked to this many characters before
    # embedding (0 = never chunk); consecutive chunks overlap by
    # SUMMARY_CHUNK_OVERLAP characters.
    "SUMMARY_CHUNK_CHARS": 2000,
    "SUMMARY_CHUNK_OVERLAP": 200,
    # HNSW index parameters for the segment-embedding cosine index (read by
    # the vector app's migration at migrate time).
    "HNSW": {"M": 16, "EF_CONSTRUCTION": 64},
    # Postgres FTS config per recording language (primary subtag, lower
    # case); anything unmapped falls back to FTS_FALLBACK_CONFIG.
    "FTS_CONFIGS": {
        "en": "english", "de": "german", "fr": "french", "es": "spanish",
        "it": "italian", "pt": "portuguese", "nl": "dutch", "ru": "russian",
    },
    "FTS_FALLBACK_CONFIG": "simple",
    # Hybrid search: reciprocal-rank fusion. score(hit) = Σ over arms of
    # WEIGHT_arm / (RRF_K + rank_arm). ARM_LIMIT caps how many candidates
    # each arm contributes before fusion.
    "RRF_K": 60,
    "RRF_WEIGHTS": {"text": 1.0, "vector": 1.0},
    "ARM_LIMIT": 50,
    # Optional LLM rerank pass over search results (any mode — applied
    # post-fusion / post-text-ranking). The top TOP_K hits' segment texts
    # are sent to the ``llm.rerank`` comm Function (stapel-agent >= 0.5)
    # and that block is re-ordered by rerank score; hits beyond TOP_K keep
    # their prior order after it. TOP_N is forwarded to the provider
    # (score only the N best; 0 = score everything). FAIL_OPEN: True →
    # a rerank failure logs a warning and returns the un-reranked order;
    # False → VectorSearchUnavailable. Note: segment texts DO go to the
    # rerank provider — same trust boundary as llm.transcribe/summarize.
    "RERANK": {
        "ENABLED": False,
        "PROVIDER": "",
        "TOP_K": 50,
        "TOP_N": 20,
        "TIMEOUT_SECONDS": 60,
        "FAIL_OPEN": True,
    },
    # ── Question answering over transcripts (vector/qa.py) ────────────
    # How long to WAIT for llm.complete. Without an explicit argument the
    # call falls back to FUNCTION_TIMEOUT (default 5s), which a real answer
    # over eight excerpts won't fit in. 120s leaves headroom while staying
    # bounded — a human is waiting on an open page behind this call.
    "QA_TIMEOUT_SECONDS": 120,
    # Model size for the answer (llm.complete alias: small|medium|large).
    # medium, not large: answering from eight retrieved excerpts is reading
    # with citation, not reasoning, so large buys cost, not quality. Hosts
    # that disagree change one line.
    "QA_MODEL": "medium",
    # Optional provider pin ("" = the agent's default).
    "QA_PROVIDER": "",
    # Cap on a SINGLE excerpt in the prompt. This exists for transport, not
    # taste: llm.complete goes through comm, NATS caps messages at 1 MiB,
    # and going over loses work that's already been done (and paid for).
    "QA_CONTEXT_CHARS": 1200,
}

#: AppSettings-shaped literal dict (capability-config.md §2): a top-level
#: DEFAULTS lets the capabilities.json emitter introspect axis keys/kinds
#: without re-parsing the AppSettings() call.
DEFAULTS = {
        # ── Pipeline (flagship extension point) ──────────────────────
        # Ordered stage-name list run by the generic driver. A host can
        # reorder, drop (e.g. skip "diarize") or insert stages (e.g. a
        # "redact_pii" before "merge") purely by changing this list.
        "PIPELINE": list(DEFAULT_PIPELINE),
        # Overlay of custom/replacement stage handlers: {name: dotted-path}.
        # Merge-over-builtins semantics; a value of None removes a built-in.
        "STAGES": {},
        # Resolver seam: dotted path to ``(recording) -> list[str]``. The
        # default returns the PIPELINE setting; point it at a DB / per-
        # workspace source to let operators edit pipelines at runtime.
        "PIPELINE_RESOLVER": "stapel_recordings.pipeline.default_pipeline_resolver",

        # ── Storage seam (single strategy, replace) ──────────────────
        # Dotted path to a RecordingStorage implementation. Default is a
        # Django-storage backend (works with any DEFAULT_FILE_STORAGE);
        # swap for the bundled S3/MinIO backend or your own.
        "STORAGE": "stapel_recordings.storage.DjangoStorageBackend",
        "STORAGE_PREFIX": "recordings",

        # ── Audio normalization seam (single strategy, replace) ──────
        # Dotted path to ``(src_path, dst_path) -> float|None`` returning
        # duration seconds. Default shells out to ffmpeg; a passthrough is
        # provided for environments without ffmpeg / for tests.
        "NORMALIZER": "stapel_recordings.normalize.ffmpeg_normalize",

        # ── S3/MinIO call timeouts ────────────────────────────────────
        # The bare botocore defaults (connect 60s, read 60s, five retries)
        # turn an unreachable store into near-five-minutes of SILENCE
        # instead of an error: the HTTP worker sits busy and the user just
        # sees a spinner.
        #
        # These values are deliberately tight: presigning is local and
        # instant, while list/upload hit the store over the network. A
        # fast failure beats a wait that never resolves.
        "S3_CONNECT_TIMEOUT": 5,
        "S3_READ_TIMEOUT": 15,
        "S3_MAX_ATTEMPTS": 2,

        # ── Upload sessions ──────────────────────────────────────────
        "UPLOAD_SESSION_TTL_SECONDS": 15 * 60,
        "MULTIPART_SESSION_TTL_SECONDS": 24 * 60 * 60,
        "MULTIPART_PART_SIZE": 10 * 1024 * 1024,
        "MAX_UPLOAD_BYTES": 2 * 1024 * 1024 * 1024,
        # Hard ceiling on how many presigned part URLs one multipart session
        # may mint. MAX_UPLOAD_BYTES already bounds this for sane part
        # sizes; the cap is what keeps a tiny MULTIPART_PART_SIZE (or a
        # future caller-chosen one) from turning one request into tens of
        # thousands of signatures. 10000 is also the S3 protocol limit.
        "MAX_MULTIPART_PARTS": 10000,
        # Content gate applied to the STORED object at finalize time (see
        # media_types.py): "reject_known_bad" (default — refuse executables,
        # archives and renderable markup), "require_known_media" (accept
        # only recognized media containers) or "off".
        "UPLOAD_CONTENT_POLICY": "reject_known_bad",
        # Allowlist of upload file extensions (lower-case, no dot) for the
        # required ``filename`` on ``create_upload_session`` — the object
        # key is suffixed with the validated extension. Tuning,
        # not an axis: extend it for whatever media your NORMALIZER handles.
        "UPLOAD_EXTENSION_ALLOWLIST": [
            "mp3", "m4a", "wav", "ogg", "oga", "opus", "webm", "flac",
            "aac", "aiff", "amr", "wma", "mp4", "mov", "mkv", "3gp",
        ],

        # ── Public share links (shares.py) ────────────────────────────
        # Lifetime of an unlock token issued after a correct passcode. It
        # is a signed token, so this is the only thing bounding how long a
        # copied header keeps working — keep it short enough that a leaked
        # one dies on its own, long enough to read a meeting transcript.
        "SHARE_UNLOCK_TOKEN_TTL_SECONDS": 60 * 60,
        # Failed passcode attempts before unlocking is locked out, and how
        # long the lockout lasts. A passcode is human-chosen (four to six
        # characters in practice); without a bound it is not a secret.
        "SHARE_UNLOCK_MAX_ATTEMPTS": 5,
        "SHARE_UNLOCK_LOCKOUT_SECONDS": 5 * 60,
        # TTL of the media URL handed to a share that carries the "media"
        # permission. Short: the URL leaves the trust boundary.
        "SHARE_MEDIA_URL_TTL_SECONDS": 5 * 60,

        # ── Authorized media delivery (media.py, audit STORE-01) ──────
        # TTL of the presigned GET issued to an authorized OWNER request.
        # Short by construction: the URL is a bearer credential for the
        # object — once minted, whoever holds it reads the bytes without
        # passing the policy again. Long enough to start playback and to
        # survive a seek, short enough that a copied URL dies on its own.
        "MEDIA_URL_TTL_SECONDS": 5 * 60,
        # Tri-state answer to "does the storage backend mint EXPIRING,
        # credentialed GET URLs": None (default) = ask the backend's
        # ``signs_get_urls``. Set True only when the configured Django
        # storage backend really signs its ``url()`` (e.g. S3Boto3Storage
        # with querystring_auth=True) — the default for DjangoStorageBackend
        # is False because a plain ``storage.url()`` never expires, and
        # handing one out IS the anonymous-bucket delivery this module
        # refuses to depend on. False forces the refusal even for a signing
        # backend (useful to prove a stand no longer serves media at all).
        "STORAGE_SIGNS_GET_URLS": None,
        # TTL of the presigned GET the transcribe stage hands to the ASR
        # provider. Not client-facing — but with a private bucket it is the
        # ONLY way the provider reads the audio, so it must outlive
        # TRANSCRIBE_TIMEOUT_SECONDS: a provider that starts late (queued
        # behind other work) must still be able to fetch. W007 warns if it
        # does not.
        "TRANSCRIBE_AUDIO_URL_TTL_SECONDS": 60 * 60,

        # Extra ``Recording.metadata`` keys the HOST reserves for server
        # decisions (a billing waiver, an entitlement stamp). Rejected in
        # client-supplied metadata at any depth by
        # stapel_recordings.metadata.sanitize_user_metadata / the
        # UserMetadataField. The library's own reserved keys are added to
        # this list, never replaced by it.
        "RESERVED_METADATA_KEYS": [],

        # ── Workspace membership gate on create ───────────────────────
        # ``POST /recordings`` names the workspace the new recording lands
        # in, and the caller supplies that id. True (default): the id is
        # verified against the workspaces module — the same membership
        # question the workspace LISTING already asks — BEFORE any row,
        # upload session or object key exists. Without it, any account can
        # mint a recording inside any organization's workspace (storage keys
        # are namespaced by workspace id, and that workspace's members then
        # see the injected row in their listing).
        #
        # Set False ONLY where recordings runs without stapel-workspaces
        # (single-tenant stand, workspace ids minted by the host itself):
        # membership cannot be answered there, and the check fails CLOSED,
        # so it would refuse every create. Opening is the explicit act.
        "REQUIRE_WORKSPACE_MEMBERSHIP_ON_CREATE": True,

        # ── Object policy seam (single strategy, replace) ─────────────
        # Dotted path to the class answering "may this user do this to this
        # recording". Default: owner-only for every verb. A host that wants
        # workspace members to read (or edit) subclasses it and points this
        # here — the decision stops being spread across view bodies.
        "RECORDING_POLICY": "stapel_recordings.policy.OwnerOnlyPolicy",

        # ── Source-type registry (merge-over-builtins extension point) ─
        # Recording source kinds (meet / dictaphone / upload / other by
        # default) are an OPEN registry, not a hardcoded enum: a host adds
        # ``zoom`` / ``teams`` / ``phone`` by overlaying this map — merged
        # OVER stapel_recordings.sources.DEFAULT_SOURCE_TYPES, the same
        # merge-registry idiom as STAGES / notifications.TYPES. ``{key:
        # label}``. See stapel_recordings/sources.py.
        "SOURCE_TYPES": {},

        # ── Transcription / summarization (delegated to stapel-agent) ─
        "TRANSCRIBE_TIMEOUT_SECONDS": 1800,
        "MAX_STAGE_RETRIES": 3,
        "SUMMARIZE_ENABLED": True,
        "SUMMARIZE_MODEL": "medium",
        # How long to WAIT for llm.summarize. Without an explicit argument
        # the call falls back to FUNCTION_TIMEOUT (default 5s), and a
        # meeting summary doesn't get written in five seconds — the failure
        # is swallowed as best-effort, so the summary just silently never
        # appears.
        "SUMMARIZE_TIMEOUT_SECONDS": 300,
        # Who executes llm.* tasks in THIS process. True (default): if no
        # real handler is nearby (microservices — the agent has its own
        # database and can't see this process's task record), register the
        # bridge that hands work to the agent via a Function call. False is
        # for deployments that execute llm.* some other way.
        "DELEGATE_TASKS_TO_AGENT": True,

        # ── Reconcile watchdog ───────────────────────────────────────
        # STUCK_THRESHOLD_SECONDS MUST exceed the longest legitimate stage
        # duration (for the built-ins: TRANSCRIBE_TIMEOUT_SECONDS), or the
        # watchdog will re-emit recording.stage for stages that are still
        # running — the duplicate then piles up on the row lock. Default:
        # transcribe timeout (1800) + 5 min headroom. If you raise
        # TRANSCRIBE_TIMEOUT_SECONDS (or add a slower custom stage), raise
        # this too — a system check (W005) warns on inconsistency.
        "STUCK_THRESHOLD_SECONDS": 35 * 60,
        "ABANDONED_UPLOAD_THRESHOLD_SECONDS": 60 * 60,

        # ── Opt-in vector/search layer (stapel_recordings.vector) ─────
        # Nested tuning block for the optional embeddings app; see
        # DEFAULT_VECTOR above and vector_config() below. Not an axis —
        # the capability switch is installing the vector app itself.
        "VECTOR": dict(DEFAULT_VECTOR),
}

recordings_settings = AppSettings(
    "STAPEL_RECORDINGS",
    defaults=DEFAULTS,
    import_strings=("STORAGE", "NORMALIZER", "PIPELINE_RESOLVER", "RECORDING_POLICY"),
    # A switch that can only ever be flipped OPEN must not be reachable from
    # the environment: the name is generic enough to collide in a shared pod
    # or a compose file, and the value would arrive as a string (see
    # :func:`flag`). It still resolves via STAPEL_RECORDINGS, a flat Django
    # setting, or the default — "this stand has no workspaces module" is a
    # deployment declaration, so it is stated in settings.
    no_env=("REQUIRE_WORKSPACE_MEMBERSHIP_ON_CREATE",),
)

#: Spellings :func:`flag` accepts. Anything else is "not a boolean".
_TRUTHY = frozenset({"1", "true", "yes", "on"})
_FALSY = frozenset({"0", "false", "no", "off"})


def _coerce_bool(value, *, unrecognized: bool) -> bool:
    """``bool()`` that does not read ``"false"`` as True."""
    if isinstance(value, str):
        text = value.strip().lower()
        if text in _TRUTHY:
            return True
        if text in _FALSY:
            return False
        return unrecognized
    return bool(value)


def flag(key: str) -> bool:
    """Read a boolean setting without the ``bool("false") is True`` trap.

    ``AppSettings`` does no coercion, so a value that arrives as a STRING —
    a flat Django setting written as ``"false"``, a value copied out of a
    compose file — is truthy for every non-empty spelling. On a security
    switch that reverses the answer silently. Unrecognized text falls back
    to the library DEFAULT, which is the closed answer for every switch
    here, so garbage can never open something.
    """
    return _coerce_bool(getattr(recordings_settings, key), unrecognized=bool(DEFAULTS[key]))


def vector_config() -> dict:
    """Effective VECTOR block: the host's ``STAPEL_RECORDINGS["VECTOR"]``
    merged over :data:`DEFAULT_VECTOR` (AppSettings replaces dict values
    wholesale, so the merge lives here). The nested dicts (``HNSW`` /
    ``FTS_CONFIGS`` / ``RRF_WEIGHTS`` / ``RERANK``) merge one level deep
    too — a host flipping ``RERANK["ENABLED"]`` keeps the other knobs."""
    host = recordings_settings.VECTOR or {}
    merged = {**DEFAULT_VECTOR, **host}
    for key in ("HNSW", "FTS_CONFIGS", "RRF_WEIGHTS", "RERANK"):
        merged[key] = {**DEFAULT_VECTOR[key], **(host.get(key) or {})}
    return merged


__all__ = ["recordings_settings", "flag", "vector_config", "DEFAULT_PIPELINE", "DEFAULT_VECTOR"]
