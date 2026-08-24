"""DRF views for stapel-recordings.

Thin views over the service layer. Each view carries a request/response
serializer seam (``SerializerSeamMixin``) so a host can swap the contract
by subclassing — no need to rewrite the method bodies.

Guest (anonymous session) stance
--------------------------------
With ``AUTH_ANONYMOUS`` on, a guest session is ``is_authenticated``, so a bare
``IsAuthenticated`` says nothing about whether guests belong on a view
(``stapel_core.adoption`` E001/W002). This module's answer is the same for
every account-gated view, because it follows from what a recording *is*:

    **a recording is a durable, owned artifact with a processing pipeline
    behind it — an anonymous session is not an owner.**

Every per-recording verb here is already owner-scoped through
:func:`_owned_qs`, so a guest's answer was 404 or an empty listing all along;
for those, ``IsNotAnonymousUser`` only moves an existing refusal to the door
where it can be read.

``POST /recordings`` is the one that was genuinely open, and it is the most
expensive endpoint in the module: it mints a row, opens an upload session,
and enqueues transcription/diarization/summarization. Metering that on an
account stops meaning anything when a session costs one unauthenticated POST
to mint — so it is gated on a real account, not on being logged in.

Having an account is not the whole answer for that verb, though. The payload
names the workspace the recording lands in, so creation is a *membership*
question, and it is asked here with the same fail-closed seam the workspace
listing uses (``REQUIRE_WORKSPACE_MEMBERSHIP_ON_CREATE``, on by default).

The three ``/shares/...`` routes are the deliberate exception, and they are
not a hole in that stance: they are anonymous *by design* because the caller
authenticates with the link token itself. Every one of them goes through
:func:`stapel_recordings.shares.access_share`, which is where "may this
request read this recording, and how much of it" is actually decided.

Media delivery (audit STORE-01)
-------------------------------
Bytes are served by ``GET .../media`` on both paths — owner and share — and
never by a URL a client can construct against the bucket. The endpoint
authorizes first (object policy / share grant), then mints a short-lived
presigned GET through :mod:`stapel_recordings.media`. If the storage backend
cannot produce an expiring URL, the answer is 503, not a permanent one.

Progress: polling, and the module says so
-----------------------------------------
This module serves no WebSocket — there is no consumer, no routing module and
no Channels dependency — so a client learns that a recording moved by reading
it again. Rather than leave every frontend to invent an interval (and to
discover by experiment which statuses ever change), the answer travels in the
response: ``RecordingDTO.poll_after_seconds`` plus a ``Retry-After`` header,
present only while the pipeline owns the next transition
(:meth:`~stapel_recordings.models.RecordingStatus.is_processing`) and absent
on a terminal or client-owned status — which is how a client is told to stop.
Both come from :func:`~stapel_recordings.dto.poll_after_seconds`, so the body
and the header cannot disagree.
"""
from django.http import HttpResponseRedirect
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework.permissions import AllowAny
from rest_framework.views import APIView
from stapel_core.django.api.errors import StapelErrorResponse, StapelResponse
from stapel_core.django.api.pagination import AnchorPagination
from stapel_core.django.api.permissions import IsNotAnonymousUser

from . import media, pipeline, services, shares, stages
from .conf import flag, recordings_settings
from .dto import (
    CreateRecordingResponse,
    ShareUnlockDTO,
    job_to_dto,
    media_grant_to_dto,
    poll_after_seconds,
    recording_to_dto,
    segment_to_dto,
    shared_recording_to_dto,
    upload_session_to_dto,
)
from .errors import (
    ERR_403_ACTION_DENIED,
    ERR_403_WORKSPACE_FORBIDDEN,
    ERR_404_NOT_FOUND,
    ERR_409_INVALID_STATE,
    ERR_409_MEDIA_NOT_STORED,
    ERR_409_NO_TRANSCRIPT,
    ERR_413_TOO_LARGE,
    ERR_415_UNSUPPORTED_MEDIA,
    ERR_503_MEDIA_UNAVAILABLE,
    ERR_503_SUMMARIZE_UNAVAILABLE,
    ERR_503_UPLOAD_UNVERIFIABLE,
    POLICY_DENIAL_CODES,
)
from .media_types import UnsupportedUploadContent
from .models import Recording
from .policy import as_decision, get_policy
from .resources import resolve_resource_key
from .serializers import (
    CreateRecordingRequestSerializer,
    CreateRecordingResponseSerializer,
    FinalizeUploadRequestSerializer,
    JobSerializer,
    MediaURLSerializer,
    RecordingSerializer,
    SharedRecordingSerializer,
    ShareUnlockRequestSerializer,
    ShareUnlockResponseSerializer,
    TranscriptPageSerializer,
    TranscriptSegmentSerializer,
)

#: Header a client presents its unlock token in. A header, not a query
#: parameter, because query strings end up in access logs and referrers —
#: and this one is a credential for the whole share.
SHARE_UNLOCK_HEADER = "X-Share-Unlock-Token"


class TranscriptPagination(AnchorPagination):
    """Transcript pages anchored on ``sequence_num``, in reading order.

    Ascending, not the newest-first default every other listing uses: a
    transcript is read forward from the start, so ``direction=next`` walks
    *later* segments. ``sequence_num`` is a gapless total order within one
    recording, which is what makes a page stable while the pipeline is still
    appending to the end — an offset would shift under the reader.
    """

    anchor_field = "sequence_num"
    ordering = "sequence_num"

    @property
    def page_size(self):
        return int(recordings_settings.TRANSCRIPT_PAGE_SIZE)

    @property
    def max_page_size(self):
        return int(recordings_settings.TRANSCRIPT_MAX_PAGE_SIZE)


#: Query parameters of the anchor paginator, declared for the schema.
#: These views are plain ``APIView``s, so drf-spectacular never learns about
#: a paginator they instantiate themselves — undeclared, the whole windowing
#: dimension silently disappears from every generated client.
TRANSCRIPT_PAGE_PARAMETERS = [
    OpenApiParameter(
        name="anchor",
        type=int,
        location=OpenApiParameter.QUERY,
        required=False,
        description="sequence_num to page from, exclusive. Omit for the first "
        "page; pass the previous page's next_anchor to continue.",
    ),
    OpenApiParameter(
        name="limit",
        type=int,
        location=OpenApiParameter.QUERY,
        required=False,
        description="Segments per page (default TRANSCRIPT_PAGE_SIZE, capped "
        "at TRANSCRIPT_MAX_PAGE_SIZE).",
    ),
    OpenApiParameter(
        name="direction",
        type=str,
        location=OpenApiParameter.QUERY,
        required=False,
        enum=["next", "prev", "center"],
        default="next",
        description="next = later segments (the reading direction), prev = "
        "earlier, center = the window around the anchor.",
    ),
]


class SerializerSeamMixin:
    """Overridable serializer seam for every stapel-recordings APIView.

    Host projects can swap the request/response serializer of any view by
    subclassing and setting ``request_serializer_class`` /
    ``response_serializer_class`` (or overriding the getters for
    per-request decisions) — no need to rewrite the HTTP method bodies.
    """

    request_serializer_class = None
    response_serializer_class = None

    def get_request_serializer_class(self):
        return self.request_serializer_class

    def get_response_serializer_class(self):
        return self.response_serializer_class


def _owned_qs(request):
    """Recordings this request may read, per the configured object policy.

    The scope is asked for, not rebuilt: ``RECORDING_POLICY`` owns the rule
    (default: owner-only), so widening read access for a host cannot widen
    the mutating verbs by accident — those ask the same policy separately.
    Anonymous requests get an empty queryset; the previous inline scope
    handed back every non-deleted recording for a caller with no user, and
    only the view-level permission class stood between that and a response.
    """
    return get_policy().visible_queryset(getattr(request, "user", None))


def _wants_redirect(request) -> bool:
    """``?redirect=1`` asks for a 302 to the media URL instead of JSON.

    Both shapes exist because both consumers are real: a player element
    takes a URL it can follow (and follows the redirect itself), while an
    app that has to schedule a refresh before the URL dies needs the expiry,
    which only the JSON body carries. JSON is the default — the redirect
    hands the credential to whatever the browser does next."""
    return str(request.query_params.get("redirect", "")).lower() in {"1", "true", "yes"}


def _media_response(view, request, recording, *, ttl_seconds=None):
    """Issue a media URL for an ALREADY AUTHORIZED request.

    Both media endpoints funnel through here so the owner path and the share
    path cannot drift in what they hand out or in how they fail. The
    authorization itself is the caller's — this helper never sees a user."""
    try:
        grant = media.issue_media_url(recording, ttl_seconds=ttl_seconds)
    except media.MediaNotStored:
        return StapelErrorResponse(409, ERR_409_MEDIA_NOT_STORED)
    except media.MediaDeliveryUnavailable:
        # Deployment fault (the backend cannot mint an expiring URL). 503 and
        # no URL: the alternative — falling back to a permanent public URL —
        # is the finding this whole path exists to close.
        return StapelErrorResponse(503, ERR_503_MEDIA_UNAVAILABLE)
    if _wants_redirect(request):
        return HttpResponseRedirect(grant.url)
    return StapelResponse(view.get_response_serializer_class()(media_grant_to_dto(grant)))


def _retry_after(response, seconds):
    """Stamp ``Retry-After`` when — and only when — asking again will help."""
    if seconds is not None:
        response["Retry-After"] = str(int(seconds))
    return response


def _recording_response(view, recording, *, status=200):
    """Answer with a recording AND with when to come back for it.

    Every response carrying a recording goes through here, so the header and
    the payload's ``poll_after_seconds`` are one decision made once: a
    recording the pipeline is working on says "ask again in N", and one that
    is finished, failed or waiting on the client's own upload says nothing —
    the absent header IS the "stop asking"."""
    return _retry_after(
        StapelResponse(
            view.get_response_serializer_class()(recording_to_dto(recording)),
            status=status,
        ),
        poll_after_seconds(recording.status),
    )


def _policy_refusal(decision):
    """Render an object-policy denial into the StapelError envelope.

    The policy seam answers with a reason (``PolicyDecision``), and this is
    where the reason becomes an HTTP answer: the host's own status and error
    key travel through untouched, so a re-summary refused for an empty
    balance reaches the client as ``402`` with the key the host registered
    and the UI can render a top-up instead of "not found".

    A decision that names neither — and a policy that still answers with a
    bare ``bool``, which :func:`~stapel_recordings.policy.as_decision`
    coerces — keeps the module's historical answer: ``404``, which reveals
    nothing about a recording the caller may not touch. A decision that
    names a status but no key gets that status' generic key
    (:data:`~stapel_recordings.errors.POLICY_DENIAL_CODES`) rather than a
    404 key stapled onto someone else's status.
    """
    status = decision.status or 404
    code = decision.error_code or POLICY_DENIAL_CODES.get(status, ERR_403_ACTION_DENIED)
    return StapelErrorResponse(status, code)


def _unlock_token(request) -> str:
    return request.headers.get(SHARE_UNLOCK_HEADER, "")


def _share_error(exc: shares.ShareError):
    """Answer a share refusal with the status and i18n key the rule itself
    carries — the mapping lives in :mod:`shares`, not in each view."""
    response = StapelErrorResponse(exc.status_code, exc.error_key)
    retry_after = getattr(exc, "retry_after_seconds", 0)
    if retry_after:
        response["Retry-After"] = str(int(retry_after))
    return response


@extend_schema(tags=["Recordings"])
class RecordingListCreateView(SerializerSeamMixin, APIView):
    """Create a recording and open its upload session, or list recordings.

    ``POST`` creates the recording inside the workspace its payload names,
    which requires membership of that workspace (verified against the
    workspaces module; non-members get 403 and nothing is created).

    ``GET`` lists what ``RECORDING_POLICY`` makes visible to you (default:
    your own recordings); pass ``?workspace_id=<uuid>`` to narrow that to one
    workspace you are a member of (membership is verified against the
    workspaces module; non-members get 403). The workspace listing goes
    through the same object policy as the per-recording endpoints, so it
    never lists a recording those would refuse; a deployment that wants
    every member to see every recording in the workspace says so with
    ``WORKSPACE_LISTING_MEMBERS_SEE_ALL``.

    Pass ``?resource_key=<opaque-token>`` to narrow the listing to the single
    recording that token references. The key is the opaque, signed handle
    carried in every recording payload (``resolve_resource_key``); it composes
    with ``workspace_id`` (workspace scope stays membership-gated) or with the
    default owner scope. A missing/forged/tampered key resolves to nothing, so
    the listing comes back **empty** rather than 400 — the token is
    tamper-evident and opaque by design, so we neither leak whether a token is
    genuine nor surface a distinct error for a value the client only ever
    obtains from a prior server response."""

    # POST mints a row, opens an upload session and enqueues the whole
    # transcription pipeline — the most expensive endpoint here, and the only
    # one a guest could actually have used. GET was already owner- or
    # membership-scoped, so nothing readable is lost. See the module header.
    permission_classes = [IsNotAnonymousUser]
    request_serializer_class = CreateRecordingRequestSerializer
    response_serializer_class = CreateRecordingResponseSerializer

    @extend_schema(
        parameters=[
            OpenApiParameter(
                name="workspace_id",
                type=str,
                location=OpenApiParameter.QUERY,
                required=False,
                description="Narrow the listing to this workspace (requires "
                "membership). What it returns inside the workspace is still "
                "what RECORDING_POLICY makes visible.",
            ),
            OpenApiParameter(
                name="resource_key",
                type=str,
                location=OpenApiParameter.QUERY,
                required=False,
                description="Narrow the listing to the single recording this "
                "opaque resource_key references. A missing/forged key yields an "
                "empty listing.",
            ),
        ],
        responses={200: RecordingSerializer(many=True)},
    )
    def get(self, request):  # noqa: R007
        workspace_id = request.query_params.get("workspace_id")
        if workspace_id:
            if not services.check_workspace_membership(
                user_id=getattr(request.user, "pk", None), workspace_id=workspace_id
            ):
                return StapelErrorResponse(403, ERR_403_WORKSPACE_FORBIDDEN)
            # Membership answers "may you ask about this workspace", not
            # "which of its recordings may you read" — that second question
            # belongs to RECORDING_POLICY, the same object policy the detail
            # endpoint asks. Building the queryset inline here is what let
            # the two drift: this listing used to hand back rows that
            # ``GET /recordings/<id>`` refuses with 404 for the same caller,
            # and a host that tightened RECORDING_POLICY did not tighten it.
            in_workspace = Recording.objects.filter(workspace_id=workspace_id)
            if flag("WORKSPACE_LISTING_MEMBERS_SEE_ALL"):
                qs = in_workspace.filter(deleted_at__isnull=True)
            else:
                qs = get_policy().visible_queryset(request.user, in_workspace)
        else:
            qs = _owned_qs(request)

        resource_key = request.query_params.get("resource_key")
        if resource_key is not None:
            recording_id = resolve_resource_key(resource_key)
            # Missing/forged/tampered token → matches nothing (empty listing).
            qs = qs.filter(pk=recording_id) if recording_id else qs.none()

        rows = qs.order_by("-created_at")[:200]
        return StapelResponse(RecordingSerializer([recording_to_dto(r) for r in rows], many=True))

    @extend_schema(request=CreateRecordingRequestSerializer, responses={201: CreateRecordingResponseSerializer})
    def post(self, request):  # noqa: R007
        req = self.get_request_serializer_class()(data=request.data)
        req.is_valid(raise_exception=True)
        data = req.validated_data
        workspace_id = data["workspace_id"]
        # "Create a recording IN THIS WORKSPACE" is a membership question,
        # and the caller supplies the workspace id — so it is asked here,
        # against the same fail-closed seam the listing uses, BEFORE any row,
        # upload session or object key exists. Being logged in is not an
        # answer to it: without this check any account could mint a recording
        # inside any organization's workspace, where its members would then
        # see it listed.
        if flag("REQUIRE_WORKSPACE_MEMBERSHIP_ON_CREATE") and not services.check_workspace_membership(
            user_id=getattr(request.user, "pk", None), workspace_id=workspace_id
        ):
            return StapelErrorResponse(403, ERR_403_WORKSPACE_FORBIDDEN)
        recording = Recording.objects.create(
            workspace_id=workspace_id,
            owner=request.user if request.user.is_authenticated else None,
            title=data["title"],
            source_type=data.get("source_type") or "upload",
            language=data.get("language"),
            diarization_enabled=data.get("diarization_enabled", True),
        )
        session = services.create_upload_session(
            recording=recording, filename=data["filename"]
        )
        payload = CreateRecordingResponse(
            recording=recording_to_dto(recording),
            upload=upload_session_to_dto(session),
        )
        return StapelResponse(self.get_response_serializer_class()(payload), status=201)


@extend_schema(tags=["Recordings"])
class RecordingDetailView(SerializerSeamMixin, APIView):
    """Fetch a single recording."""

    # Owner-scoped via `_owned_qs`: a guest owns nothing, so this was already
    # a 404 for every id. The gate now says so at the door.
    permission_classes = [IsNotAnonymousUser]
    response_serializer_class = RecordingSerializer

    @extend_schema(responses={200: RecordingSerializer})
    def get(self, request, recording_id):  # noqa: R007
        recording = _owned_qs(request).filter(pk=recording_id).first()
        if recording is None:
            return StapelErrorResponse(404, ERR_404_NOT_FOUND)
        return _recording_response(self, recording)


@extend_schema(tags=["Recordings"])
class FinalizeUploadView(SerializerSeamMixin, APIView):
    """Finalize the upload and enqueue the pipeline."""

    # The second half of the expensive path: it is what actually starts the
    # pipeline. Owner-scoped, and a guest can no longer own a recording to
    # finalize.
    permission_classes = [IsNotAnonymousUser]
    request_serializer_class = FinalizeUploadRequestSerializer
    response_serializer_class = RecordingSerializer

    @extend_schema(request=FinalizeUploadRequestSerializer, responses={200: RecordingSerializer})
    def post(self, request, recording_id):  # noqa: R007
        recording = _owned_qs(request).filter(pk=recording_id).first()
        if recording is None:
            return StapelErrorResponse(404, ERR_404_NOT_FOUND)
        if not get_policy().can_upload(request.user, recording):
            return StapelErrorResponse(404, ERR_404_NOT_FOUND)
        session = recording.upload_sessions.order_by("-created_at").first()
        if session is None:
            return StapelErrorResponse(404, ERR_404_NOT_FOUND)
        req = self.get_request_serializer_class()(data=request.data)
        req.is_valid(raise_exception=True)
        try:
            recording = services.finalize_upload(
                session=session, file_size_bytes=req.validated_data.get("file_size_bytes")
            )
        except services.UploadTooLarge:
            return StapelErrorResponse(413, ERR_413_TOO_LARGE)
        except UnsupportedUploadContent:
            return StapelErrorResponse(415, ERR_415_UNSUPPORTED_MEDIA)
        except services.UploadContentUncheckable:
            # The bytes may be fine — nothing here can tell, because the
            # backend cannot serve a ranged read. That is the deployment's
            # fault, so it is a 5xx the operator can read, and the upload is
            # not accepted on the strength of a check that did not run.
            return StapelErrorResponse(503, ERR_503_UPLOAD_UNVERIFIABLE)
        except (services.UploadNotStored, services.InvalidMultipartParts):
            # Nothing (usable) was ever written under the session key, so
            # there is no upload to finalize — the client has to upload
            # again, not retry the finalize.
            return StapelErrorResponse(409, ERR_409_INVALID_STATE)
        return _recording_response(self, recording)


@extend_schema(tags=["Recordings"])
class ReprocessRecordingView(SerializerSeamMixin, APIView):
    """Re-run the whole pipeline for a finished recording (``completed → queued``).

    Exposes the ``pipeline.reprocess_recording`` transition: the progress cursor
    is cleared and every stage re-runs from stage 0 (distinct from the
    resume-in-place retry). Allowed **only** from ``completed`` — from any other
    status the transition is a no-op and the endpoint answers ``409``
    (``error.409.recording_invalid_state``). Owner-scoped, like every other
    per-recording verb; an unknown/foreign/deleted recording is ``404``.

    Authority is the object policy's ``can_reprocess``. A policy that
    answers with a :class:`~stapel_recordings.policy.PolicyDecision` names
    the status and error key of its own refusal (``402`` for an unpaid
    balance, say); a bare ``bool`` keeps the ``404``."""

    # Re-runs every pipeline stage from zero — the one endpoint that can spend
    # the pipeline's cost twice. Owner-scoped, now also account-gated.
    permission_classes = [IsNotAnonymousUser]
    response_serializer_class = RecordingSerializer

    @extend_schema(request=None, responses={200: RecordingSerializer})
    def post(self, request, recording_id):  # noqa: R007
        recording = _owned_qs(request).filter(pk=recording_id).first()
        if recording is None:
            return StapelErrorResponse(404, ERR_404_NOT_FOUND)
        # Reprocess spends the whole pipeline again and rewrites derived
        # artifacts, so it asks the policy for that verb specifically —
        # being able to READ a recording is not authority to re-run it.
        decision = as_decision(get_policy().can_reprocess(request.user, recording))
        if not decision.allowed:
            return _policy_refusal(decision)
        if not pipeline.reprocess_recording(str(recording.id)):
            # Recording exists and is owned (checked above), so the only reason
            # the transition is refused is a non-``completed`` status.
            return StapelErrorResponse(409, ERR_409_INVALID_STATE)
        recording.refresh_from_db()
        return _recording_response(self, recording)


@extend_schema(tags=["Recordings"])
class ResummarizeRecordingView(SerializerSeamMixin, APIView):
    """Regenerate the summary of ONE recording — no STT, no diarization.

    The cheap half of :class:`ReprocessRecordingView`. Reprocess re-runs the
    whole pipeline (a second transcription, a second bill) and is the wrong
    tool for the case that actually happens: the transcript is right — often
    because a human just corrected it — and the summary built from the older
    transcript is the only thing out of date. Where reprocess is a staff
    verb, this is the user's own: it re-runs the same ``llm.summarize`` call
    the ``merge`` stage makes, stores the result the same way, and re-pins
    the summary to the transcript it was built from so the staleness marker
    clears.

    ``202`` with a :class:`~stapel_recordings.dto.JobDTO` — the work is
    accepted, not finished. **Idempotent**: while a re-summary for this
    recording is in flight, every further POST answers ``202`` with the SAME
    job instead of paying for a second summary, so a double-clicked button
    costs one summary.

    ``409`` (``error.409.recording_no_transcript``) when there is nothing to
    summarize yet; ``503`` (``error.503.recording_summarize_unavailable``)
    when the deployment has summaries switched off or the bus refused the
    work. ``404`` for an unknown, foreign or deleted recording.

    Authority is the object policy's ``can_resummarize`` — which defaults to
    whatever the policy says about ``can_reprocess``, so a host that already
    narrowed reprocess does not have to discover a second verb, while a host
    that wants "users may re-summarize, only staff may reprocess" overrides
    one method.

    A denial answers with the policy's OWN status and error key when it
    returns a :class:`~stapel_recordings.policy.PolicyDecision`: a metered
    host refuses an out-of-credit re-summary with ``402`` and a key its UI
    can turn into a top-up prompt, instead of the ``404`` that says the
    recording does not exist. A policy that answers with a bare ``bool``
    keeps the ``404``."""

    permission_classes = [IsNotAnonymousUser]
    response_serializer_class = JobSerializer

    @extend_schema(request=None, responses={202: JobSerializer})
    def post(self, request, recording_id):  # noqa: R007
        recording = _owned_qs(request).filter(pk=recording_id).first()
        if recording is None:
            return StapelErrorResponse(404, ERR_404_NOT_FOUND)
        decision = as_decision(get_policy().can_resummarize(request.user, recording))
        if not decision.allowed:
            return _policy_refusal(decision)
        try:
            job, _started = stages.start_resummarize(recording, user=request.user)
        except stages.NoTranscriptToSummarize:
            return StapelErrorResponse(409, ERR_409_NO_TRANSCRIPT)
        except stages.SummarizationUnavailable:
            return StapelErrorResponse(503, ERR_503_SUMMARIZE_UNAVAILABLE)
        return _retry_after(
            StapelResponse(
                self.get_response_serializer_class()(job_to_dto(job)), status=202
            ),
            recordings_settings.JOB_POLL_INTERVAL_SECONDS,
        )


@extend_schema(tags=["Recordings"])
class RecordingTranscriptView(SerializerSeamMixin, APIView):
    """Read the OWNER's own speaker-attributed transcript.

    The gap this closes: segments used to leave this module through exactly
    one door — ``SharedRecordingDTO.segments`` on a public share link — so an
    owner could read their transcript only by publishing it to the internet
    first. ``RecordingDTO.transcript_storage_key`` was not an alternative: it
    is a raw object key, and nothing signs it (the media endpoint signs the
    *media* object, not the transcript).

    Same authority as every other per-recording read: owner-scoped through
    ``_owned_qs`` and then the object policy's ``can_read``, so an unknown,
    foreign or deleted recording is a ``404`` and a widened
    ``RECORDING_POLICY`` widens this with it. Same wire shape as the share
    path — :class:`~stapel_recordings.dto.TranscriptSegmentDTO` — so a
    transcript renderer is written once and serves both doors.

    Paginated, anchored on ``sequence_num`` (:class:`TranscriptPagination`):
    a meeting-length transcript is thousands of segments, and the anchor is
    what keeps a page stable while the pipeline is still appending to the end.

    A recording that has no segments yet answers ``200`` with an empty page,
    not an error — "not transcribed yet" is a stage of a normal lifecycle,
    and while the pipeline is mid-flight the response carries the same
    ``Retry-After`` the recording read does, so the client is told when to
    come back rather than left to guess.
    """

    permission_classes = [IsNotAnonymousUser]
    response_serializer_class = TranscriptSegmentSerializer
    pagination_class = TranscriptPagination

    @extend_schema(
        parameters=TRANSCRIPT_PAGE_PARAMETERS,
        responses={200: TranscriptPageSerializer},
    )
    def get(self, request, recording_id):  # noqa: R007
        recording = _owned_qs(request).filter(pk=recording_id).first()
        if recording is None or not get_policy().can_read(request.user, recording):
            return StapelErrorResponse(404, ERR_404_NOT_FOUND)
        paginator = self.pagination_class()
        page = paginator.paginate_queryset(
            recording.segments.select_related("speaker"), request
        )
        response_cls = self.get_response_serializer_class()
        items = [response_cls(segment_to_dto(s)).data for s in page]
        return _retry_after(
            paginator.get_paginated_response(items),
            poll_after_seconds(recording.status),
        )


@extend_schema(tags=["Recordings"])
class RecordingMediaView(SerializerSeamMixin, APIView):
    """Issue a short-lived, authorized URL to a recording's media object.

    This endpoint is how bytes are reached — it exists so that the bucket
    does not have to be readable by anyone holding an object key (audit
    STORE-01). The object policy answers ``can_read`` for *this* recording
    first; only then is a presigned GET minted, with
    ``MEDIA_URL_TTL_SECONDS`` on it.

    ``?redirect=1`` answers ``302`` to the URL (drop-in for an ``<audio
    src>``); the default JSON body carries the expiry so a client can
    refresh before playback dies. ``503`` means the deployment's storage
    backend cannot sign — no permanent URL is substituted."""

    permission_classes = [IsNotAnonymousUser]
    response_serializer_class = MediaURLSerializer

    @extend_schema(
        parameters=[
            OpenApiParameter(
                name="redirect",
                type=bool,
                location=OpenApiParameter.QUERY,
                required=False,
                description="Answer 302 to the media URL instead of a JSON body.",
            ),
        ],
        responses={200: MediaURLSerializer},
    )
    def get(self, request, recording_id):  # noqa: R007
        recording = _owned_qs(request).filter(pk=recording_id).first()
        if recording is None or not get_policy().can_read(request.user, recording):
            return StapelErrorResponse(404, ERR_404_NOT_FOUND)
        return _media_response(self, request, recording)


@extend_schema(tags=["Recordings"])
class SharedRecordingView(SerializerSeamMixin, APIView):
    """Read a recording through a public share link.

    Anonymous by design: the link token in the path IS the credential, and
    ``shares.access_share`` is what verifies it — revocation, expiry, the
    recording's own lifecycle, and (for a passcode-protected share) a
    *verified* unlock token from the ``X-Share-Unlock-Token`` header. The
    projection then renders only what the share grants."""

    # No authentication class: a share link is a bearer credential, and the
    # caller's account (if any) neither grants nor withholds anything here.
    # Running session auth over it would only add CSRF to an anonymous GET.
    authentication_classes = []
    permission_classes = [AllowAny]
    response_serializer_class = SharedRecordingSerializer

    @extend_schema(responses={200: SharedRecordingSerializer})
    def get(self, request, link_token):  # noqa: R007
        try:
            access = shares.access_share(link_token, unlock_token=_unlock_token(request))
        except shares.ShareError as exc:
            return _share_error(exc)
        return StapelResponse(
            self.get_response_serializer_class()(shared_recording_to_dto(access))
        )


@extend_schema(tags=["Recordings"])
class ShareUnlockView(SerializerSeamMixin, APIView):
    """Exchange a share's passcode for a time-limited unlock token.

    The token is signed and bound to the share, its generation and a TTL —
    presenting *any* nonempty value here is what SHARE-01 was. Guessing is
    bounded by the persisted lockout in ``shares.unlock_share``."""

    authentication_classes = []
    permission_classes = [AllowAny]
    request_serializer_class = ShareUnlockRequestSerializer
    response_serializer_class = ShareUnlockResponseSerializer

    @extend_schema(
        request=ShareUnlockRequestSerializer, responses={200: ShareUnlockResponseSerializer}
    )
    def post(self, request, link_token):  # noqa: R007
        req = self.get_request_serializer_class()(data=request.data)
        req.is_valid(raise_exception=True)
        share = shares.resolve_share(link_token)
        if share is None:
            # Same refusal as revoked/expired: telling an unknown token apart
            # from a known one maps out which guessed links exist.
            return _share_error(shares.ShareNotFound())
        try:
            token = shares.unlock_share(share, req.validated_data.get("passcode") or "")
        except shares.ShareError as exc:
            return _share_error(exc)
        payload = ShareUnlockDTO(
            unlock_token=token,
            expires_in=int(recordings_settings.SHARE_UNLOCK_TOKEN_TTL_SECONDS),
        )
        return StapelResponse(self.get_response_serializer_class()(payload))


@extend_schema(tags=["Recordings"])
class SharedRecordingMediaView(SerializerSeamMixin, APIView):
    """Media URL for a share link that carries the ``media`` permission.

    The public half of STORE-01: a shared recording plays without the bucket
    being anonymously readable. The share is verified on every call, the
    ``media`` grant is required explicitly (a share that only grants ``view``
    gets 403), and the URL is minted with ``SHARE_MEDIA_URL_TTL_SECONDS`` —
    shorter than the owner's, because this one leaves the trust boundary."""

    authentication_classes = []
    permission_classes = [AllowAny]
    response_serializer_class = MediaURLSerializer

    @extend_schema(
        parameters=[
            OpenApiParameter(
                name="redirect",
                type=bool,
                location=OpenApiParameter.QUERY,
                required=False,
                description="Answer 302 to the media URL instead of a JSON body.",
            ),
        ],
        responses={200: MediaURLSerializer},
    )
    def get(self, request, link_token):  # noqa: R007
        try:
            access = shares.access_share(link_token, unlock_token=_unlock_token(request))
            shares.require_permission(access, shares.PERM_MEDIA)
        except shares.ShareError as exc:
            return _share_error(exc)
        return _media_response(
            self,
            request,
            access.recording,
            ttl_seconds=int(recordings_settings.SHARE_MEDIA_URL_TTL_SECONDS),
        )
