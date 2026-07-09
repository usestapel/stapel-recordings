"""Serializers for the stapel-recordings API."""
from rest_framework import serializers
from stapel_core.django.api.serializers import StapelDataclassSerializer

from .dto import CreateRecordingResponse, RecordingDTO, UploadSessionDTO


class RecordingSerializer(StapelDataclassSerializer):
    class Meta:
        dataclass = RecordingDTO


class UploadSessionSerializer(StapelDataclassSerializer):
    class Meta:
        dataclass = UploadSessionDTO


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
    # Optional: the client's original filename. When present its extension is
    # validated against UPLOAD_EXTENSION_ALLOWLIST and used to build the
    # upload object key. Omit for the legacy extension-less key.
    filename = serializers.CharField(max_length=512, required=False, allow_blank=True)

    def validate_source_type(self, value):
        from .sources import is_valid_source_type, registered_source_types

        if value and not is_valid_source_type(value):
            raise serializers.ValidationError(
                f"unknown source_type {value!r}; registered: "
                f"{registered_source_types()}"
            )
        return value

    def validate_filename(self, value):
        from .services import UnsupportedUploadExtension, validated_upload_ext

        if not value:
            return value
        try:
            validated_upload_ext(value)
        except UnsupportedUploadExtension as exc:
            raise serializers.ValidationError(str(exc)) from exc
        return value


class FinalizeUploadRequestSerializer(serializers.Serializer):
    file_size_bytes = serializers.IntegerField(required=False, min_value=0)
