import uuid
from django.conf import settings
from django.db import models


class Report(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="reports",
    )

    # JSONField: diabetic readings/values (your structure)
    diabetic_values = models.JSONField(default=dict)

    # PDF path or URL (stored in Supabase)
    pdf_file = models.CharField(max_length=512, blank=True, null=True)

    # AI result (store text + raw JSON if you want)
    ai_summary_text = models.TextField(blank=True, default="")
    ai_raw = models.JSONField(default=dict, blank=True)

    openai_response_id = models.CharField(max_length=128, blank=True, default="")
    status = models.CharField(
        max_length=32, default="created"
    )  # created|processing|done|error
    error_message = models.TextField(blank=True, default="")
    clinician_consent = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["user", "created_at"]),
            models.Index(fields=["status", "created_at"]),
        ]


class AdminPrompt(models.Model):
    is_active = models.BooleanField(
        default=True, help_text="Only active prompts will be appended."
    )
    custom_instructions = models.TextField(
        help_text="Custom instructions to append to the system prompt."
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Admin Prompt ({'Active' if self.is_active else 'Inactive'}) - {self.updated_at.strftime('%Y-%m-%d %H:%M')}"

    class Meta:
        verbose_name = "Admin Prompt"
        verbose_name_plural = "Admin Prompts"
        ordering = ["-updated_at"]
