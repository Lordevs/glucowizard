from rest_framework import serializers
from .models import Report


class ReportCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model = Report
        fields = [
            "id",
            "diabetic_values",
            "pdf_file",
            "clinician_consent",
            "created_at",
        ]
        read_only_fields = ["id", "created_at"]


class ReportListSerializer(serializers.ModelSerializer):
    class Meta:
        model = Report
        fields = ["id", "pdf_file", "clinician_consent", "created_at"]
        read_only_fields = ["id", "created_at"]


class ReportDetailSerializer(serializers.ModelSerializer):
    class Meta:
        model = Report
        fields = [
            "id",
            "diabetic_values",
            "pdf_file",
            "clinician_consent",
            "ai_summary_text",
            "ai_raw",
            "openai_response_id",
            "status",
            "error_message",
            "created_at",
            "updated_at",
        ]
