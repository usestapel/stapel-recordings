"""DRF views for stapel-recordings.

Thin views over the service layer. Each view carries a request/response
serializer seam (``SerializerSeamMixin``) so a host can swap the contract
by subclassing — no need to rewrite the method bodies.
"""
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import permissions
from rest_framework.views import APIView
from stapel_core.django.api.errors import StapelErrorResponse, StapelResponse

from . import services
from .dto import CreateRecordingResponse, recording_to_dto, upload_session_to_dto
from .errors import ERR_403_WORKSPACE_FORBIDDEN, ERR_404_NOT_FOUND
from .models import Recording
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
    verified against the workspaces module; non-members get 403)."""

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
            )
        ],
        responses={200: RecordingSerializer(many=True)},
    )
    def get(self, request):
        workspace_id = request.query_params.get("workspace_id")
        if workspace_id:
            if not services.check_workspace_membership(
                user_id=getattr(request.user, "pk", None), workspace_id=workspace_id
            ):
                return StapelErrorResponse(403, ERR_403_WORKSPACE_FORBIDDEN)
            rows = (
                Recording.objects.filter(
                    deleted_at__isnull=True, workspace_id=workspace_id
                )
                .order_by("-created_at")[:200]
            )
        else:
            rows = _owned_qs(request).order_by("-created_at")[:200]
        return StapelResponse(RecordingSerializer([recording_to_dto(r) for r in rows], many=True))

    @extend_schema(request=CreateRecordingRequestSerializer, responses={201: CreateRecordingResponseSerializer})
    def post(self, request):
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
            recording=recording, filename=data.get("filename") or None
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
    def get(self, request, recording_id):
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
    def post(self, request, recording_id):
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
