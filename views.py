"""DRF views for stapel-recordings.

Thin views over the service layer. Each view carries a request/response
serializer seam (``SerializerSeamMixin``) so a host can swap the contract
by subclassing — no need to rewrite the method bodies.
"""
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import permissions
from rest_framework.views import APIView
from stapel_core.django.api.errors import StapelErrorResponse, StapelResponse

from . import pipeline, services
from .dto import CreateRecordingResponse, recording_to_dto, upload_session_to_dto
from .errors import (
    ERR_403_WORKSPACE_FORBIDDEN,
    ERR_404_NOT_FOUND,
    ERR_409_INVALID_STATE,
)
from .models import Recording
from .resources import resolve_resource_key
from .serializers import (
    CreateRecordingRequestSerializer,
    CreateRecordingResponseSerializer,
    FinalizeUploadRequestSerializer,
    RecordingSerializer,
)


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
    qs = Recording.objects.filter(deleted_at__isnull=True)
    if request.user and request.user.is_authenticated:
        return qs.filter(owner=request.user)
    return qs


@extend_schema(tags=["Recordings"])
class RecordingListCreateView(SerializerSeamMixin, APIView):
    """Create a recording and open its upload session, or list recordings.

    ``GET`` lists your own recordings by default; pass ``?workspace_id=<uuid>``
    to list every recording in a workspace you are a member of (membership is
    verified against the workspaces module; non-members get 403).

    Pass ``?resource_key=<opaque-token>`` to narrow the listing to the single
    recording that token references. The key is the opaque, signed handle
    carried in every recording payload (``resolve_resource_key``); it composes
    with ``workspace_id`` (workspace scope stays membership-gated) or with the
    default owner scope. A missing/forged/tampered key resolves to nothing, so
    the listing comes back **empty** rather than 400 — the token is
    tamper-evident and opaque by design, so we neither leak whether a token is
    genuine nor surface a distinct error for a value the client only ever
    obtains from a prior server response."""

    permission_classes = [permissions.IsAuthenticated]
    request_serializer_class = CreateRecordingRequestSerializer
    response_serializer_class = CreateRecordingResponseSerializer

    @extend_schema(
        parameters=[
            OpenApiParameter(
                name="workspace_id",
                type=str,
                location=OpenApiParameter.QUERY,
                required=False,
                description="List all recordings in this workspace (requires "
                "membership) instead of only your own.",
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
            qs = Recording.objects.filter(
                deleted_at__isnull=True, workspace_id=workspace_id
            )
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
        recording = Recording.objects.create(
            workspace_id=data["workspace_id"],
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

    permission_classes = [permissions.IsAuthenticated]
    response_serializer_class = RecordingSerializer

    @extend_schema(responses={200: RecordingSerializer})
    def get(self, request, recording_id):  # noqa: R007
        recording = _owned_qs(request).filter(pk=recording_id).first()
        if recording is None:
            return StapelErrorResponse(404, ERR_404_NOT_FOUND)
        return StapelResponse(self.get_response_serializer_class()(recording_to_dto(recording)))


@extend_schema(tags=["Recordings"])
class FinalizeUploadView(SerializerSeamMixin, APIView):
    """Finalize the upload and enqueue the pipeline."""

    permission_classes = [permissions.IsAuthenticated]
    request_serializer_class = FinalizeUploadRequestSerializer
    response_serializer_class = RecordingSerializer

    @extend_schema(request=FinalizeUploadRequestSerializer, responses={200: RecordingSerializer})
    def post(self, request, recording_id):  # noqa: R007
        recording = _owned_qs(request).filter(pk=recording_id).first()
        if recording is None:
            return StapelErrorResponse(404, ERR_404_NOT_FOUND)
        session = recording.upload_sessions.order_by("-created_at").first()
        if session is None:
            return StapelErrorResponse(404, ERR_404_NOT_FOUND)
        req = self.get_request_serializer_class()(data=request.data)
        req.is_valid(raise_exception=True)
        recording = services.finalize_upload(
            session=session, file_size_bytes=req.validated_data.get("file_size_bytes")
        )
        return StapelResponse(self.get_response_serializer_class()(recording_to_dto(recording)))


@extend_schema(tags=["Recordings"])
class ReprocessRecordingView(SerializerSeamMixin, APIView):
    """Re-run the whole pipeline for a finished recording (``completed → queued``).

    Exposes the ``pipeline.reprocess_recording`` transition: the progress cursor
    is cleared and every stage re-runs from stage 0 (distinct from the
    resume-in-place retry). Allowed **only** from ``completed`` — from any other
    status the transition is a no-op and the endpoint answers ``409``
    (``error.409.recording_invalid_state``). Owner-scoped, like every other
    per-recording verb; an unknown/foreign/deleted recording is ``404``."""

    permission_classes = [permissions.IsAuthenticated]
    response_serializer_class = RecordingSerializer

    @extend_schema(request=None, responses={200: RecordingSerializer})
    def post(self, request, recording_id):  # noqa: R007
        recording = _owned_qs(request).filter(pk=recording_id).first()
        if recording is None:
            return StapelErrorResponse(404, ERR_404_NOT_FOUND)
        if not pipeline.reprocess_recording(str(recording.id)):
            # Recording exists and is owned (checked above), so the only reason
            # the transition is refused is a non-``completed`` status.
            return StapelErrorResponse(409, ERR_409_INVALID_STATE)
        recording.refresh_from_db()
        return StapelResponse(self.get_response_serializer_class()(recording_to_dto(recording)))
