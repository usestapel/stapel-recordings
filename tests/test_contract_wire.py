"""Every response body the contract declares is a body the views actually send.

``docs/schema.json`` is emitted from the views' ``@extend_schema``
annotations, and an annotation is a CLAIM: it says what the view returns, and
the generator has no way to check it against the method body.
``tests/test_contract.py`` compares the committed document against a FRESH
EMISSION of the same annotations — it proves the file is not stale, and
nothing else, because both sides come from the claim. stapel-alerts 0.2.0
shipped ``GET /issues`` declared as ``Issue[]`` while the wire carried
``{count, offset, limit, results}``: the drift gate was green and the
frontend pair rendered ``undefined``.

This is the gate the generator cannot be: it performs every operation the
committed schema declares with a JSON response body, and validates the body
it gets against the schema it was promised.

Rules this file holds itself to:

* an operation with a declared JSON response and no entry in ``RECIPES``
  FAILS LOUDLY — a gate that quietly covers three of four rows is the family
  of green that proves nothing;
* a path parameter the gate cannot fill fails at the point of substitution,
  naming the operation;
* an operation that genuinely cannot run in-process is listed by name in
  ``UNDRIVABLE`` with a one-line reason. That list is asserted to be exactly
  current: a stale entry, or a missing reason, fails;
* a collection that comes back empty fails in the populated pass — an empty
  array validates against any item schema, so an empty answer is a check that
  looked at nothing;
* every read, and every write whose declared body carries a nullable field,
  is driven a SECOND time in its emptiest legal state (``EMPTY_STATE``):
  a recording with a transcript and one without, a share that grants
  everything and one that grants only ``view``, a page of segments and a
  page with none. Every null finding in the first wave of this gate was on
  the empty state.

Runs on every interpreter: it reads the committed schema and never emits.

THE MOUNT. ``codegen_urls.py`` mounts ``recordings/`` and this module's own
``urls.py`` contributes ``api/v1/``, so the document is written against
``/recordings/api/v1/…``. ``tests/urls.py`` mounts the SAME prefix, so unlike
five of the first eight libraries in this wave this module's suite was
already looking where its document points. The emission mount is declared
here anyway (``test_every_declared_path_resolves_under_this_urlconf``
asserts against it), so a later edit to ``tests/urls.py`` cannot silently
unhook the contract.

WHAT IT FOUND on its first run — 12 of 12 operations driven, 1 red, now fixed:

* ``GET /recordings/{id}/transcript`` declared ``TranscriptPage.next_anchor``
  and ``prev_anchor`` as ``string`` (REQUIRED, nullable) and sent an
  **integer** on every page that had a neighbour.
  ``TranscriptPagination.anchor_field`` is ``sequence_num`` (views.py), an
  ``IntegerField`` (models.py), and ``AnchorPagination.get_paginated_response``
  copies the raw field value into the envelope — it stringifies only values
  that have ``.isoformat()``, which an int does not. Every other anchor
  paginator in the fleet anchors on a datetime, where that branch fires and
  the claim is true; this one anchors on an int, where it never could be. A
  generated client typed ``next_anchor`` as ``string | null`` and handed ``4``
  back as the ``anchor`` query parameter — which happens to work, so nothing
  failed loudly; TypeScript simply believed a lie about every transcript
  longer than one page. The anchor IS an integer, so 0.27.0 declares it one
  (``IntegerField(allow_null=True)`` plus ``anchor_type = "integer"`` on the
  paginator) and ``KNOWN_MISMATCHES`` is empty. The EMPTY state of the same
  operation (a recording with no segments, both anchors genuinely null) was
  always honest and is driven separately, which is why the two tables stay
  separate.

Everything else held, including every ``nullable`` field of ``RecordingDTO``
and ``SharedRecordingDTO`` in both states, and
``test_the_gate_is_not_blind`` proves that is a finding rather than a gate
that never looked.
"""
import copy
import json
import re
import uuid
from pathlib import Path

import jsonschema
import pytest
from django.test import override_settings
from django.urls import include, path as url_path
from rest_framework.test import APIClient

REPO = Path(__file__).resolve().parent.parent
SCHEMA = json.loads((REPO / "docs" / "schema.json").read_text())

#: The mount the contract is emitted at, reproduced for the test client
#: (``codegen_urls.py``: ``recordings/`` + the module's own ``api/v1/``).
urlpatterns = [
    url_path("recordings/", include("stapel_recordings.urls")),
]

pytestmark = [pytest.mark.django_db, pytest.mark.urls(__name__)]

V1 = "/recordings/api/v1"

#: Set by the autouse fixture below, so the module-level recipes can reach
#: the suite's fixtures without taking them as arguments.
_DRAIN = None
_MEMBERSHIP = None


@pytest.fixture(autouse=True)
def _media_root(tmp_path):
    """Pin ``MEDIA_ROOT`` for the duration of a test.

    The harness settings point it at a fixed ``/tmp`` directory shared by
    every run on the machine. Nothing in these recipes writes through it —
    the storage seam is swapped for the in-memory backend below — but an
    unpinned root is how an export in stapel-auth ended up written into the
    checkout, where a stray directory then shadowed a real module.
    """
    with override_settings(MEDIA_ROOT=str(tmp_path / "media")):
        yield


@pytest.fixture(autouse=True)
def _seams(use_fakes, stub_transcribe, stub_summarize, stub_membership, drain):
    """The four seams a deployment wires, wired the way its own suite does.

    ``use_fakes`` swaps the storage backend for the in-memory one and the
    normalizer for the passthrough — ffmpeg and an object store are not part
    of any response shape. ``llm.transcribe`` / ``llm.summarize`` are comm
    Functions another module owns; ``workspaces.check_membership`` is
    stapel-workspaces'. Those are SEAMS, and standing them up is what a
    deployment does — everything on this side of them (the DTO, the
    serializer, the status, the pipeline) runs for real.
    """
    global _DRAIN, _MEMBERSHIP
    _DRAIN, _MEMBERSHIP = drain, stub_membership
    yield
    _DRAIN, _MEMBERSHIP = None, None


# ─────────────────────────────────────────────────────────────────────────────
# The contract side: what the document declares
# ─────────────────────────────────────────────────────────────────────────────


def _json_schema(node):
    """OpenAPI 3.0 → JSON Schema, for the divergences that matter here.

    OAS 3.0 spells "may be null" as ``nullable: true`` beside a ``type``;
    JSON Schema has no such keyword and would refuse the null — which is
    exactly the value most of these fields answer in their empty state.
    Everything else drf-spectacular emits here (``$ref``, ``format``,
    ``required``) is JSON Schema as written.
    """
    if isinstance(node, list):
        return [_json_schema(item) for item in node]
    if not isinstance(node, dict):
        return node
    rebuilt = {k: _json_schema(v) for k, v in node.items() if k != "nullable"}
    if node.get("nullable"):
        return {"anyOf": [rebuilt, {"type": "null"}]}
    return rebuilt


def _validator(response_schema):
    root = copy.deepcopy(response_schema)
    root["components"] = copy.deepcopy(SCHEMA["components"])
    return jsonschema.Draft202012Validator(_json_schema(root))


def _operations():
    """Every ``(method, path, 2xx code, JSON body schema)`` the contract declares."""
    ops = []
    for path, methods in SCHEMA["paths"].items():
        for method, op in methods.items():
            if method not in {"get", "post", "put", "patch", "delete"}:
                continue
            for code, response in op.get("responses", {}).items():
                body = (
                    response.get("content", {})
                    .get("application/json", {})
                    .get("schema")
                )
                if body is not None and code.startswith("2"):
                    ops.append((method.upper(), path, int(code), body))
    return sorted(ops, key=lambda o: (o[1], o[0], o[2]))


OPERATIONS = _operations()


def _resolved(node, _depth=0):
    """A response schema with its component ``$ref``s inlined, so a caller can
    see whether the body it describes carries a nullable field anywhere."""
    if _depth > 6:
        return node
    if isinstance(node, list):
        return [_resolved(item, _depth + 1) for item in node]
    if not isinstance(node, dict):
        return node
    ref = node.get("$ref")
    if isinstance(ref, str) and ref.startswith("#/components/schemas/"):
        name = ref.rsplit("/", 1)[-1]
        return _resolved(SCHEMA["components"]["schemas"].get(name, {}), _depth + 1)
    return {k: _resolved(v, _depth + 1) for k, v in node.items()}


def _has_nullable(schema):
    return '"nullable": true' in json.dumps(_resolved(schema), sort_keys=True)


# ─────────────────────────────────────────────────────────────────────────────
# The wire side: harness
# ─────────────────────────────────────────────────────────────────────────────


def _unique(prefix):
    return f"{prefix}{uuid.uuid4().hex[:10]}"


def anonymous():
    return APIClient()


def make_user():
    from django.contrib.auth import get_user_model

    return get_user_model().objects.create(username=_unique("wire-"))


def client_for(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client


def make_recording(owner, **kwargs):
    from stapel_recordings.models import Recording, RecordingStatus

    defaults = dict(
        workspace_id=uuid.uuid4(),
        owner=owner,
        title="Wire contract recording",
        status=RecordingStatus.QUEUED,
        file_storage_key=None,
        diarization_enabled=True,
    )
    defaults.update(kwargs)
    return Recording.objects.create(**defaults)


def stored_audio(recording, suffix="audio"):
    """Put the raw object where the pipeline and the media grant look for it."""
    from stapel_recordings.storage import get_storage

    key = f"recordings/{recording.workspace_id}/{recording.id}/{suffix}"
    recording.file_storage_key = key
    recording.save(update_fields=["file_storage_key"])
    get_storage().put_bytes(key, b"raw-audio-bytes", content_type="audio/mpeg")
    return recording


def transcribed(owner=None):
    """A recording taken through the REAL pipeline to ``completed``.

    Stage 0 is emitted and the outbox drained, exactly as the production
    relay does it, so the row that comes back carries a language, a duration,
    a provider, segments, speakers, a stored transcript and a summary — every
    optional field of ``RecordingDTO`` filled by the code that fills it in
    production, rather than by this file writing them onto the row.
    """
    from stapel_recordings import events
    from stapel_recordings.models import Recording, RecordingStatus

    recording = stored_audio(make_recording(owner or make_user(), status="queued"))
    events.emit_stage(recording.id, 0)
    _DRAIN()
    row = Recording.objects.get(pk=recording.id)
    assert row.status == RecordingStatus.COMPLETED, row.status
    return row


def completed_bare(owner=None):
    """A ``completed`` recording that never went through the pipeline: no
    language, no duration, no provider, no transcript key, no summary — the
    six ``nullable`` claims of ``RecordingDTO``, all answered null at once."""
    from stapel_recordings.models import RecordingStatus

    return stored_audio(make_recording(owner or make_user(), status=RecordingStatus.COMPLETED))


def segments(recording, count=3, named=True):
    """Real ``Segment`` rows, numbered from 1 so an anchor is unambiguous."""
    from stapel_recordings.models import Segment, Speaker

    speaker = Speaker.objects.create(
        recording=recording,
        label="speaker_0",
        display_name="Ada" if named else "",
    )
    for i in range(1, count + 1):
        Segment.objects.create(
            recording=recording,
            speaker=speaker if named else None,
            sequence_num=i,
            start_time=float(i),
            end_time=float(i) + 1.0,
            text=f"segment {i}",
        )
    return recording


def upload_session(recording, filename="take.mp3"):
    from stapel_recordings import services

    return services.create_upload_session(recording=recording, filename=filename)


def uploaded(owner=None, **recording_kwargs):
    """A recording whose upload session is open and whose object is present —
    the state ``POST /finalize`` is for."""
    from stapel_recordings.models import RecordingStatus

    recording = make_recording(
        owner or make_user(), status=RecordingStatus.CREATED, **recording_kwargs
    )
    session = upload_session(recording)
    from stapel_recordings.storage import get_storage

    get_storage().put_bytes(session.storage_key, b"x" * 2048, content_type="audio/mpeg")
    recording.refresh_from_db()
    return recording


def share_for(recording, permissions=None, passcode=None):
    """One share link through the real minting path; returns the token."""
    from stapel_recordings import shares

    _share, token = shares.create_share(
        recording=recording, permissions=permissions, passcode=passcode
    )
    return token


def member(user, workspace_id):
    """Grant membership on the ``workspaces.check_membership`` seam."""
    _MEMBERSHIP.grant(workspace_id, user.pk)
    return workspace_id


# ─────────────────────────────────────────────────────────────────────────────
# The recipe table
# ─────────────────────────────────────────────────────────────────────────────


class Call:
    """Performs one declared operation, and refuses to guess a path parameter."""

    def __init__(self, method, path):
        self.method = method
        self.path = path

    def __call__(self, client, params=None, data=None, query="", **extra):
        url = self.path
        for name, value in (params or {}).items():
            url = url.replace("{%s}" % name, str(value))
        assert "{" not in url, (
            f"{self.method} {self.path}: a path parameter this gate does not "
            "know how to fill — teach its recipe, or the operation goes unchecked"
        )
        send = getattr(client, self.method.lower())
        if self.method in ("GET", "DELETE"):
            return send(url + query, **extra)
        return send(url + query, data if data is not None else {}, format="json", **extra)


#: How to perform each operation the contract declares with a JSON response
#: body, keyed by ``(METHOD, path template, status code)``.
RECIPES = {}

#: The same operations again, in the emptiest state the contract still has to
#: describe. Every null finding in the first wave of this gate was there.
EMPTY_STATE = {}


def recipe(method, path, code=None, table=None):
    def register(fn):
        target = RECIPES if table is None else table
        key = (method, V1 + path, code)
        assert key not in target, f"duplicate recipe for {method} {path} {code}"
        target[key] = fn
        return fn

    return register


def empty_state(method, path, code=None):
    return recipe(method, path, code, table=EMPTY_STATE)


#: Operations that cannot be driven in-process, by name and with the reason.
#: A short, visible list is acceptable here; a silent skip is not.
#:
#: EMPTY. Every operation this module declares is reachable from a test
#: client. The four sibling-owned calls (``llm.transcribe``,
#: ``llm.summarize``, ``workspaces.check_membership``, and the storage
#: backend) are comm/seam boundaries a deployment wires, so this gate wires
#: them too and everything on this side of them runs for real.
UNDRIVABLE: dict = {}


# ── the owner's recordings ───────────────────────────────────────────────────


@recipe("GET", "/recordings")
def _list(call):
    owner = make_user()
    transcribed(owner)
    return call(client_for(owner))


@empty_state("GET", "/recordings")
def _list_empty(call):
    """An account that has recorded nothing — the listing is genuinely []."""
    return call(client_for(make_user()))


@recipe("POST", "/recordings", code=201)
def _create(call):
    owner = make_user()
    workspace_id = member(owner, uuid.uuid4())
    return call(
        client_for(owner),
        data={
            "workspace_id": str(workspace_id),
            "title": "Wire contract recording",
            "filename": "take.mp3",
            "language": "en",
        },
    )


@empty_state("POST", "/recordings", code=201)
def _create_bare(call):
    """No language hint: the recording comes back with the null ``language``,
    ``duration_seconds``, ``provider_used``, ``transcript_storage_key``,
    ``summary`` and ``needs_payment_reason`` that the declaration marks
    REQUIRED, and a null ``poll_after_seconds`` because ``created`` waits on
    the CLIENT, not on the pipeline."""
    owner = make_user()
    workspace_id = member(owner, uuid.uuid4())
    return call(
        client_for(owner),
        data={
            "workspace_id": str(workspace_id),
            "title": "Wire contract recording",
            "filename": "take.mp3",
        },
    )


@recipe("GET", "/recordings/{recording_id}")
def _detail(call):
    owner = make_user()
    recording = transcribed(owner)
    return call(client_for(owner), params={"recording_id": recording.id})


@empty_state("GET", "/recordings/{recording_id}")
def _detail_bare(call):
    owner = make_user()
    return call(
        client_for(owner), params={"recording_id": completed_bare(owner).id}
    )


@recipe("GET", "/recordings/upload-limits")
def _upload_limits(call):
    return call(client_for(make_user()))


@recipe("POST", "/recordings/{recording_id}/finalize")
def _finalize(call):
    owner = make_user()
    recording = uploaded(owner, language="en")
    return call(
        client_for(owner),
        params={"recording_id": recording.id},
        data={"file_size_bytes": 2048},
    )


@empty_state("POST", "/recordings/{recording_id}/finalize")
def _finalize_bare(call):
    """A recording with no language declared and nothing derived yet: the
    finalize answer is the state where every optional field is still null."""
    owner = make_user()
    return call(
        client_for(owner), params={"recording_id": uploaded(owner).id}
    )


@recipe("POST", "/recordings/{recording_id}/reprocess")
def _reprocess(call):
    owner = make_user()
    recording = transcribed(owner)
    return call(client_for(owner), params={"recording_id": recording.id})


@empty_state("POST", "/recordings/{recording_id}/reprocess")
def _reprocess_bare(call):
    """``completed`` is the only status this transition accepts, so the
    emptiest legal state is a completed recording that derived nothing."""
    owner = make_user()
    return call(
        client_for(owner), params={"recording_id": completed_bare(owner).id}
    )


@recipe("POST", "/recordings/{recording_id}/resummarize", code=202)
def _resummarize(call):
    owner = make_user()
    recording = transcribed(owner)
    return call(client_for(owner), params={"recording_id": recording.id})


@recipe("GET", "/recordings/{recording_id}/transcript")
def _transcript(call):
    """One segment per page, so the envelope actually carries an anchor.

    The default page size is 200 — a transcript that fits in one page answers
    ``next_anchor: null`` and says nothing about the type the field carries
    when there IS a next page, which is the claim this operation gets wrong.
    """
    owner = make_user()
    recording = segments(completed_bare(owner), count=3)
    return call(
        client_for(owner), params={"recording_id": recording.id}, query="?limit=1"
    )


@empty_state("GET", "/recordings/{recording_id}/transcript")
def _transcript_empty(call):
    """A recording nothing has transcribed yet: an empty page, and both
    anchors genuinely null — the state the declaration describes correctly."""
    owner = make_user()
    return call(
        client_for(owner), params={"recording_id": completed_bare(owner).id}
    )


@recipe("GET", "/recordings/{recording_id}/media")
def _media(call):
    owner = make_user()
    recording = completed_bare(owner)
    return call(client_for(owner), params={"recording_id": recording.id})


# ── the public share surface ─────────────────────────────────────────────────


@recipe("GET", "/shares/{link_token}")
def _share_detail(call):
    """A share that grants everything: summary, segments and a media URL all
    present, which is the only state that proves those three fields render."""
    from stapel_recordings import shares

    owner = make_user()
    recording = transcribed(owner)
    token = share_for(
        recording,
        permissions=[
            shares.PERM_VIEW,
            shares.PERM_SUMMARY,
            shares.PERM_TRANSCRIPT,
            shares.PERM_MEDIA,
        ],
    )
    return call(anonymous(), params={"link_token": token})


@empty_state("GET", "/shares/{link_token}")
def _share_detail_view_only(call):
    """The default share — ``view`` and nothing else — on a recording that
    derived nothing: null ``language``, null ``duration_seconds``, null
    ``summary``, null ``media_url`` and an empty ``segments``. Four of the
    five are REQUIRED in the declaration."""
    owner = make_user()
    return call(anonymous(), params={"link_token": share_for(completed_bare(owner))})


@recipe("GET", "/shares/{link_token}/media")
def _share_media(call):
    from stapel_recordings import shares

    owner = make_user()
    recording = completed_bare(owner)
    token = share_for(recording, permissions=[shares.PERM_VIEW, shares.PERM_MEDIA])
    return call(anonymous(), params={"link_token": token})


@recipe("POST", "/shares/{link_token}/unlock")
def _share_unlock(call):
    owner = make_user()
    token = share_for(completed_bare(owner), passcode="1234")
    return call(anonymous(), params={"link_token": token}, data={"passcode": "1234"})


@empty_state("POST", "/shares/{link_token}/unlock")
def _share_unlock_no_passcode(call):
    """A share with no passcode still answers here with a token, so a client
    can always unlock first and branch never — the emptiest input this
    endpoint takes."""
    owner = make_user()
    return call(
        anonymous(), params={"link_token": share_for(completed_bare(owner))}, data={}
    )


# ─────────────────────────────────────────────────────────────────────────────
# The gate
# ─────────────────────────────────────────────────────────────────────────────


#: Operations whose declared body the POPULATED wire does not send.
#:
#: An entry names the defect AND its owner, and ``strict=True`` turns a fixed
#: one into a failure until the entry is deleted, so a finding can be neither
#: forgotten nor quietly kept.
KNOWN_MISMATCHES: dict = {}

#: The same, for the EMPTY-state pass. Separate on purpose: a defect can live
#: in one state and not the other, and marking both xfail would hide a claim
#: that the wire actually keeps. EMPTY here — the empty transcript page is
#: the one place this contract tells the truth about its anchors.
KNOWN_MISMATCHES_EMPTY: dict = {}


def _recipe_for(table, method, path, code):
    """The code-specific recipe if there is one, else the operation's."""
    return table.get((method, path, code)) or table.get((method, path, None))


def test_the_contract_declares_something_to_check():
    assert OPERATIONS, "docs/schema.json declares no JSON responses at all"


def test_every_declared_path_resolves_under_this_urlconf():
    """The suite must be looking where the document describes.

    Five of the first eight libraries this gate was written for had a
    committed contract that nothing had ever driven, because the test urlconf
    mounted somewhere the document does not describe: one mounted a different
    prefix AND one segment short, one mounted the paths bare, one mounted less
    than the emission did, one doubled a segment to reproduce a host's
    deployed prefix. In every case the operations were "covered" by a file
    that could not have reached a single one of them.

    A missing recipe already fails loudly; this fails when the MOUNT is wrong,
    which no per-operation check can see, because when the mount is wrong
    every operation is equally and silently unreachable.
    """
    from django.urls import Resolver404, resolve

    # Resolution cares about the SHAPE of a segment, and this URL set uses
    # both uuid and str converters. A path counts as reachable if any one
    # shape resolves: the question is whether the mount exists, not whether a
    # particular id does.
    candidates = (
        "00000000-0000-4000-8000-000000000000",
        "1",
        "a-slug",
    )

    unreachable = []
    for _method, path, _code, _schema in OPERATIONS:
        for value in candidates:
            try:
                resolve(re.sub(r"\{[^}]+\}", value, path))
                break
            except Resolver404:
                continue
        else:
            unreachable.append(path)

    assert not unreachable, (
        "these declared paths do not resolve under this module's urlconf, so "
        "nothing here can be driving them — the mount is wrong, not the "
        "recipes:\n  " + "\n  ".join(sorted(set(unreachable)))
    )


def test_every_declared_operation_is_driven_or_named_undrivable():
    """No operation is covered by silence, and no entry outlives its operation."""
    missing = [
        (method, path, code)
        for method, path, code, _schema in OPERATIONS
        if _recipe_for(RECIPES, method, path, code) is None
        and (method, path) not in UNDRIVABLE
    ]
    assert not missing, (
        "operations with a declared JSON response body and no recipe:\n"
        + "\n".join(f"  {m} {p} -> {c}" for m, p, c in missing)
    )

    declared_codes = {(m, p, c) for m, p, c, _ in OPERATIONS}
    declared_ops = {(m, p) for m, p, _c, _ in OPERATIONS}
    stale = sorted(
        key
        for key in RECIPES
        if (key[0], key[1]) not in declared_ops
        or (key[2] is not None and key not in declared_codes)
    )
    assert not stale, (
        "recipes for operations/status codes the contract no longer declares:\n"
        + "\n".join(f"  {m} {p} -> {c}" for m, p, c in stale)
    )
    both = sorted((m, p) for m, p, _c in RECIPES if (m, p) in UNDRIVABLE)
    assert not both, f"driven AND excluded: {both}"
    for key, reason in UNDRIVABLE.items():
        assert reason and reason.strip(), f"{key} is excluded with no reason"

    # RECIPES ∪ UNDRIVABLE is EXACTLY the declared set, in both directions.
    covered = {(m, p) for m, p, _c in RECIPES} | set(UNDRIVABLE)
    assert covered == declared_ops, (
        "the covered set and the declared set differ:\n"
        f"  declared and not covered: {sorted(declared_ops - covered)}\n"
        f"  covered and not declared: {sorted(covered - declared_ops)}"
    )


def test_every_read_is_also_driven_in_its_emptiest_state():
    """A populated answer cannot say what a field holds when there is nothing.

    Every null finding in the first wave of this gate was on the empty state.
    A gate that only ever seeds three rows and asks never sees any of them.
    """
    exempt = {
        # MediaURLDTO has three REQUIRED, non-nullable fields and no
        # collection — a grant either exists (200) or the endpoint answers
        # 409/503, so there is no emptier body for it to send.
        ("GET", V1 + "/recordings/{recording_id}/media"),
        ("GET", V1 + "/shares/{link_token}/media"),
        # Derived from the deployment's settings alone (upload_limits()).
        # There is no per-caller or per-row state for it to be empty of, so an
        # "empty" run would be byte-for-byte the populated one.
        ("GET", V1 + "/recordings/upload-limits"),
        # JobDTO.recording_id is null only for a job with no recording, and
        # this endpoint exists to queue work FOR a recording — it names one in
        # the path and puts it on the job. No legal state of this operation
        # answers null there.
        ("POST", V1 + "/recordings/{recording_id}/resummarize"),
    }
    reads = {
        (method, path)
        for method, path, _code, schema in OPERATIONS
        if method == "GET" or _has_nullable(schema)
    }
    covered = {(m, p) for m, p, _c in EMPTY_STATE}
    missing = sorted(reads - covered - exempt)
    assert not missing, (
        "operations driven only against a populated database — the state where "
        "every null claim in this gate's history was found is unchecked:\n"
        + "\n".join(f"  {m} {p}" for m, p in missing)
    )
    declared_ops = {(m, p) for m, p, _c, _ in OPERATIONS}
    stale = sorted({(m, p) for m, p, _c in EMPTY_STATE} - declared_ops)
    assert not stale, f"empty-state recipes for undeclared operations: {stale}"


def test_every_known_mismatch_is_still_declared_and_explained():
    """A recorded defect must name a live operation and carry its reason.

    Without this, an operation that is renamed or removed leaves an entry that
    silences nothing and reads like a known problem forever.
    """
    declared = {(method, path) for method, path, _code, _schema in OPERATIONS}
    for table in (KNOWN_MISMATCHES, KNOWN_MISMATCHES_EMPTY):
        for key, reason in table.items():
            assert key in declared, (
                f"{key} is recorded as a known mismatch but the contract no "
                "longer declares it — delete the entry"
            )
            assert reason and reason.strip(), f"{key} is recorded with no reason"
            assert "OWNER:" in reason, (
                f"{key} names a defect but not who owns it — an unowned "
                "finding is a finding nobody fixes"
            )


def _drive(table, method, path, code, body_schema, *, expect_rows):
    perform = _recipe_for(table, method, path, code)
    assert perform is not None, (
        f"{method} {path} declares a response body and has no recipe — an "
        "unchecked operation is a schema nobody proves. Teach RECIPES, or "
        "name it in UNDRIVABLE with a reason."
    )

    response = perform(Call(method, path))
    assert response.status_code == code, (
        f"{method} {path}: expected the declared {code}, got "
        f"{response.status_code}: {response.content[:400]}"
    )

    body = response.json()
    errors = sorted(_validator(body_schema).iter_errors(body), key=lambda e: list(e.path))
    assert not errors, (
        f"{method} {path} answers a body the contract does not describe:\n"
        + "\n".join(f"  at {list(e.path) or '<root>'}: {e.message}" for e in errors[:10])
        + f"\n  body: {json.dumps(body)[:600]}"
    )
    # An empty list validates against any item schema, so a collection must
    # actually carry a row for the check to have looked at anything — both the
    # bare arrays and the anchor-pagination envelope's ``items``.
    if expect_rows:
        rows = body if isinstance(body, list) else None
        if rows is None and isinstance(body, dict) and isinstance(body.get("items"), list):
            rows = body["items"]
        if rows is not None:
            assert rows, f"{method} {path}: the declared collection came back empty"
    return body


@pytest.mark.parametrize(
    "method,path,code,body_schema",
    OPERATIONS,
    ids=[f"{m} {p} {c}" for m, p, c, _ in OPERATIONS],
)
def test_the_wire_matches_the_declared_response(method, path, code, body_schema, request):
    if (method, path) in UNDRIVABLE:
        pytest.skip(f"excluded by name: {UNDRIVABLE[(method, path)]}")

    if (method, path) in KNOWN_MISMATCHES:
        request.node.add_marker(
            pytest.mark.xfail(
                strict=True,
                reason=f"{method} {path}: {KNOWN_MISMATCHES[(method, path)]}",
            )
        )

    _drive(RECIPES, method, path, code, body_schema, expect_rows=True)


_EMPTY_OPERATIONS = [
    (method, path, code, schema)
    for method, path, code, schema in OPERATIONS
    if _recipe_for(EMPTY_STATE, method, path, code) is not None
]


@pytest.mark.parametrize(
    "method,path,code,body_schema",
    _EMPTY_OPERATIONS,
    ids=[f"{m} {p} {c}" for m, p, c, _ in _EMPTY_OPERATIONS],
)
def test_the_wire_matches_the_declared_response_when_there_is_nothing_there(
    method, path, code, body_schema, request
):
    """The same claim, asked in the state where the nulls live."""
    if (method, path) in KNOWN_MISMATCHES_EMPTY:
        request.node.add_marker(
            pytest.mark.xfail(
                strict=True,
                reason=f"{method} {path}: {KNOWN_MISMATCHES_EMPTY[(method, path)]}",
            )
        )

    _drive(EMPTY_STATE, method, path, code, body_schema, expect_rows=False)


def test_the_gate_is_not_blind():
    """A canary: swap a declared schema for one the wire cannot satisfy.

    Everything above can be green for two reasons — the claims are honest, or
    the check never looks at the body. This tells them apart by validating a
    real response against ``{"type": "string"}``: every operation here answers
    an object or an array, so every one of them must fail. If any passes, the
    validation in ``_drive`` is not reaching the received body and this whole
    file proves nothing.
    """
    honest = [
        (method, path, code)
        for method, path, code, _schema in OPERATIONS
        if (method, path) not in KNOWN_MISMATCHES and (method, path) not in UNDRIVABLE
    ]
    assert honest, "nothing left to canary"

    survivors = []
    for method, path, code in honest:
        try:
            _drive(RECIPES, method, path, code, {"type": "string"}, expect_rows=False)
        except AssertionError:
            continue
        survivors.append(f"{method} {path}")
    assert not survivors, (
        "these operations passed validation against {'type': 'string'} — the "
        "gate is not looking at the body it received:\n  " + "\n  ".join(survivors)
    )
