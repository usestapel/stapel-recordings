"""Serializers for the stapel-recordings API."""
from rest_framework import serializers
from stapel_core.django.api.serializers import StapelDataclassSerializer

from .dto import (
    CreateRecordingResponse,
    JobDTO,
    MediaURLDTO,
    MultipartManifestDTO,
    MultipartMintDTO,
    MultipartStartDTO,
    RecordingDTO,
    SharedRecordingDTO,
    ShareUnlockDTO,
    TranscriptSegmentDTO,
    UploadLimitsDTO,
    UploadLookupDTO,
    UploadSessionDTO,
)


class RecordingSerializer(StapelDataclassSerializer):
    class Meta:
        dataclass = RecordingDTO


class UploadSessionSerializer(StapelDataclassSerializer):
    class Meta:
        dataclass = UploadSessionDTO


class UploadLimitsSerializer(StapelDataclassSerializer):
    class Meta:
        dataclass = UploadLimitsDTO


class CreateRecordingResponseSerializer(StapelDataclassSerializer):
    class Meta:
        dataclass = CreateRecordingResponse


class CreateRecordingRequestSerializer(serializers.Serializer):
    """Incoming payload to create a recording + open an upload session."""

    workspace_id = serializers.UUIDField()
    title = serializers.CharField(max_length=500)
    source_type = serializers.CharField(max_length=32, required=False)
    language = serializers.CharField(max_length=10, required=False, allow_null=True)
    diarization_enabled = serializers.BooleanField(required=False, default=True)
    # The client's original filename. Its extension is validated against
    # UPLOAD_EXTENSION_ALLOWLIST and used to build the upload object key.
    filename = serializers.CharField(max_length=512)

    def validate_source_type(self, value):
        from .sources import is_valid_source_type, registered_source_types

        if value and not is_valid_source_type(value):
            raise serializers.ValidationError(  # noqa: R002
                f"unknown source_type {value!r}; registered: "
                f"{registered_source_types()}"
            )
        return value

    def validate_filename(self, value):
        from .services import UnsupportedUploadExtension, validated_upload_ext

        try:
            validated_upload_ext(value)
        except UnsupportedUploadExtension as exc:
            raise serializers.ValidationError(str(exc)) from exc  # noqa: R002
        return value


_SHA256_HEX = r"^[0-9a-f]{64}$"


class MultipartStartRequestSerializer(serializers.Serializer):
    file_size_bytes = serializers.IntegerField(min_value=1)
    content_type = serializers.CharField(max_length=255, required=False, allow_blank=True)
    # Names the object key's extension; defaults to the recording's upload
    # session key, else the declared content_type.
    filename = serializers.CharField(max_length=512, required=False)
    # Fingerprint v1 of the whole file (stapel_recordings.chunked). Present =
    # a verified upload: parts are minted later, bound to their hashes.
    fingerprint = serializers.RegexField(_SHA256_HEX, required=False)


class MultipartStartResponseSerializer(StapelDataclassSerializer):
    class Meta:
        dataclass = MultipartStartDTO


class PartHashSerializer(serializers.Serializer):
    part_number = serializers.IntegerField(min_value=1)
    sha256 = serializers.RegexField(_SHA256_HEX)


class MultipartMintRequestSerializer(serializers.Serializer):
    parts = PartHashSerializer(many=True, allow_empty=False, max_length=100)


class MultipartMintResponseSerializer(StapelDataclassSerializer):
    class Meta:
        dataclass = MultipartMintDTO


class MultipartManifestSerializer(StapelDataclassSerializer):
    class Meta:
        dataclass = MultipartManifestDTO


class CompletedPartSerializer(serializers.Serializer):
    part_number = serializers.IntegerField(min_value=1)
    etag = serializers.CharField(max_length=256, allow_blank=True)
    sha256 = serializers.RegexField(_SHA256_HEX, required=False)


class MultipartCompleteRequestSerializer(serializers.Serializer):
    parts = CompletedPartSerializer(many=True)


class UploadLookupSerializer(StapelDataclassSerializer):
    class Meta:
        dataclass = UploadLookupDTO


class FinalizeUploadRequestSerializer(serializers.Serializer):
    file_size_bytes = serializers.IntegerField(required=False, min_value=0)


class TranscriptSegmentSerializer(StapelDataclassSerializer):
    class Meta:
        dataclass = TranscriptSegmentDTO


class TranscriptPageSerializer(serializers.Serializer):
    """The anchor-paginated envelope one transcript page arrives in.

    Written out rather than left to the paginator's generic schema so the
    generated client gets a *typed* page: without it ``items`` emits as an
    untyped list and every consumer re-declares the segment shape by hand,
    which is how the two ends drift.
    """

    items = TranscriptSegmentSerializer(many=True)
    # The anchor IS an integer: TranscriptPagination anchors on
    # `sequence_num` and the paginator copies the raw field value into the
    # envelope. Declaring `string` here made every generated client believe a
    # lie about every transcript longer than one page.
    next_anchor = serializers.IntegerField(allow_null=True)
    prev_anchor = serializers.IntegerField(allow_null=True)
    has_next = serializers.BooleanField()
    has_prev = serializers.BooleanField()
    count = serializers.IntegerField()


class SharedRecordingSerializer(StapelDataclassSerializer):
    class Meta:
        dataclass = SharedRecordingDTO


class JobSerializer(StapelDataclassSerializer):
    class Meta:
        dataclass = JobDTO


class MediaURLSerializer(StapelDataclassSerializer):
    class Meta:
        dataclass = MediaURLDTO


class ShareUnlockRequestSerializer(serializers.Serializer):
    """Passcode presented to a share's unlock endpoint."""

    # Not required: a share without a passcode still answers here (with a
    # token), so a client can always unlock first and branch never.
    passcode = serializers.CharField(max_length=128, required=False, allow_blank=True)


class ShareUnlockResponseSerializer(StapelDataclassSerializer):
    class Meta:
        dataclass = ShareUnlockDTO

