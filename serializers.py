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


class FinalizeUploadRequestSerializer(serializers.Serializer):
    file_size_bytes = serializers.IntegerField(required=False, min_value=0)
