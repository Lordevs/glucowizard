import base64
import json
import os
from rest_framework.decorators import api_view, permission_classes, parser_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.parsers import MultiPartParser, FormParser, JSONParser
from rest_framework.response import Response
from rest_framework import status as http_status

from django.utils import timezone
from datetime import timedelta
from django.shortcuts import get_object_or_404
from django.db import transaction
from glucowizard.supabase_client import get_supabase
from .models import Report, AdminPrompt
from .serializers import (
    ReportDetailSerializer,
    ReportListSerializer,
)
from .openai_client import get_client
from .pagination import StandardResultsSetPagination
from payments.models import Subscription, UserCredit


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def report_stats(request):
    """
    Returns aggregated stats for bolus ratio, basal rates, and correction factors.
    Query param: period=week|month (defaults to week).
    """
    period = request.query_params.get("period", "week")
    days = 7 if period == "week" else 30

    start_date = timezone.now() - timedelta(days=days)

    # Fetch reports in chronological order
    reports = Report.objects.filter(
        user=request.user, created_at__gte=start_date
    ).order_by("created_at")

    stats_data = []

    for r in reports:
        # Extract the specific diabetic metrics from the JSON field
        dv = r.diabetic_values or {}
        stats_data.append(
            {
                "id": r.id,
                "created_at": r.created_at,
                "bolus_ratio": dv.get("bolus_ratio", []),
                "basal_rates": dv.get("basal_rates", []),
                "correction_factors": dv.get("correction_factors", []),
            }
        )

    return Response({"period": period, "count": len(stats_data), "data": stats_data})


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def list_reports(request):
    """List all reports for the authenticated user and generate signed URLs with pagination."""
    reports = Report.objects.filter(user=request.user).order_by("-created_at")

    paginator = StandardResultsSetPagination()
    page = paginator.paginate_queryset(reports, request)

    supabase = get_supabase()
    supabase.auth.set_session(request.auth, "")

    if page is not None:
        serializer = ReportListSerializer(page, many=True)
        data = serializer.data
        # Generate signed URLs for each report in the list
        for report_data in data:
            if report_data.get("pdf_file"):
                signed_url_resp = supabase.storage.from_("reports").create_signed_url(
                    report_data["pdf_file"], 3600
                )
                report_data["pdf_url"] = signed_url_resp.get(
                    "signedURL"
                ) or signed_url_resp.get("signed_url")
        return paginator.get_paginated_response(data)

    serializer = ReportListSerializer(reports, many=True)
    data = serializer.data
    for report_data in data:
        if report_data.get("pdf_file"):
            signed_url_resp = supabase.storage.from_("reports").create_signed_url(
                report_data["pdf_file"], 3600
            )
            report_data["pdf_url"] = signed_url_resp.get(
                "signedURL"
            ) or signed_url_resp.get("signed_url")

    return Response(data)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def get_report_detail(request, pk):
    """Get full details for a specific report, including a fresh signed URL."""
    report = get_object_or_404(Report, pk=pk, user=request.user)
    supabase = get_supabase()
    supabase.auth.set_session(request.auth, "")

    serializer = ReportDetailSerializer(report)
    data = serializer.data

    # Generate a signed URL for the detail view (1-hour expiry)
    if report.pdf_file:
        signed_url_resp = supabase.storage.from_("reports").create_signed_url(
            report.pdf_file, 3600
        )
        data["pdf_url"] = signed_url_resp.get("signedURL") or signed_url_resp.get(
            "signed_url"
        )

    return Response(data)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
@parser_classes([MultiPartParser, FormParser, JSONParser])
def create_report(request):
    """
    Creates a new report, uploads PDF to Supabase, and gets AI analysis.
    Stores the PDF path in the database.
    """
    # 1. Early Validation of API Keys
    supabase_url = os.getenv("SUPABASE_URL")
    supabase_key = os.getenv("SUPABASE_KEY")
    openai_key = os.getenv("OPENAI_API_KEY")

    if not all([supabase_url, supabase_key, openai_key]):
        missing = [
            k
            for k, v in {
                "SUPABASE_URL": supabase_url,
                "SUPABASE_KEY": supabase_key,
                "OPENAI_API_KEY": openai_key,
            }.items()
            if not v
        ]
        return Response(
            {"error": f"Server configuration error: Missing {', '.join(missing)}"},
            status=http_status.HTTP_500_INTERNAL_SERVER_ERROR,
        )

    # 1.5. Payment Validation
    user = request.user
    has_active_subscription = Subscription.objects.filter(
        user=user, status__in=["active", "trialing"]
    ).exists()

    if not has_active_subscription:
        with transaction.atomic():
            credits = UserCredit.objects.filter(user=user).select_for_update().first()
            if not credits or credits.balance <= 0:
                return Response(
                    {
                        "error": "Payment required. You need an active subscription or credits to generate a report.",
                        "code": "payment_required",
                    },
                    status=http_status.HTTP_402_PAYMENT_REQUIRED,
                )
            # Use one credit for pay-as-you-go
            credits.balance -= 1
            credits.save()

    # 2. Parse diabetic_values
    diabetic_values = request.data.get("diabetic_values")
    if isinstance(diabetic_values, str):
        try:
            diabetic_values = json.loads(diabetic_values)
        except (json.JSONDecodeError, TypeError):
            return Response(
                {"error": "diabetic_values must be valid JSON"},
                status=http_status.HTTP_400_BAD_REQUEST,
            )
    elif not diabetic_values:
        diabetic_values = {}

    # 3. Handle PDF upload to Supabase
    pdf = request.FILES.get("pdf_file")
    pdf_path = ""

    if pdf:
        try:
            supabase = get_supabase()
            # Try to set session, but don't fail hard if it's strictly a public bucket or handled via service key
            if request.auth:
                try:
                    supabase.auth.set_session(request.auth, "")
                except Exception as auth_err:
                    print(f"Supabase auth session warning: {auth_err}")

            pdf_path = f"{request.user.id}_{timezone.now().timestamp()}_{pdf.name}"
            # Read pdf content once for upload
            pdf_content = pdf.read()
            supabase.storage.from_("reports").upload(
                pdf_path,
                pdf_content,
                {"content-type": pdf.content_type, "upsert": "true"},
            )
            # Reset pointer for possible AI processing
            pdf.seek(0)
        except Exception as e:
            print(f"Supabase upload error: {e}")
            return Response(
                {"error": f"Failed to upload PDF to storage: {str(e)}"},
                status=http_status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

    # 4. Create database record
    clinician_consent = request.data.get("clinician_consent", False)
    if isinstance(clinician_consent, str):
        clinician_consent = clinician_consent.lower() == "true"

    report = Report.objects.create(
        user=request.user,
        diabetic_values=diabetic_values,
        pdf_file=pdf_path,
        clinician_consent=clinician_consent,
        status="processing",
    )

    # 5. Get AI Analysis (OpenAI)
    try:
        client = get_client()

        # Build Prompts
        system_prompt = (
            "Analyze the following diabetes management data and provide a comprehensive report in Markdown format.\n\n"
            "### Inputs Provided\n"
            "- CGM report (time-series and summary statistics)\n"
            "- Current insulin dosing parameters\n\n"
            "### Tasks\n"
            "1. **Summary**: Provide key insights from the glucose data (Average Glucose, GMI, CV, Time in Range).\n"
            "2. **Analysis**: Identify trends, hyperglycemia, hypoglycemia, and variability issues.\n"
            "3. **Recommendations**: Provide clinically reasonable suggested adjustments for Bolus Ratio, Basal Rate, and Correction Factor.\n\n"
            "### Rules\n"
            "- **STRICTLY NO JSON**: Do not include any JSON code blocks (e.g., ```json ... ```) or raw JSON strings in your response. All information must be presented as Markdown text, headings, lists, or tables.\n"
            "- **NO MARKDOWN CODE WRAPPERS**: Do not wrap your entire response in triple backticks (e.g., ```markdown ... ```). Provide the raw Markdown text directly.\n"
            "- **Conservative Suggestions**: Suggestions must be conservative, expressed as ranges or directional changes, and clearly noted as non-prescriptive.\n"
            "- **Data Uncertainty**: Clearly note when trends are uncertain or data is insufficient.\n\n"
        )

        if report.clinician_consent:
            system_prompt += (
                "### Clinician Suggestions Table\n"
                "The user HAS requested suggestions to share with their healthcare professional.\n"
                "For EACH time slot/period mentioned in the user's current insulin parameters (e.g., Morning, Afternoon, Nighttime or specific time ranges), "
                "create a Markdown table detailing suggested adjustments for Bolus Ratio, Correction Factor, and Basal Rates.\n\n"
                "Table Columns: | Time Period / Slot | Parameter | Current Value | Suggested Adjustment | Rationale |\n"
                "| --- | --- | --- | --- | --- |\n\n"
            )
        else:
            system_prompt += (
                "### Recommendations\n"
                "Provide the suggested adjustments for insulin parameters in a clear, bulleted or numbered list format.\n\n"
            )

        system_prompt += (
            "### Current Insulin Parameters (JSON)\n"
            f"{json.dumps(report.diabetic_values)}"
        )

        active_admin_prompt = AdminPrompt.objects.filter(is_active=True).first()
        if active_admin_prompt and active_admin_prompt.custom_instructions:
            system_prompt += f"\n\n### Additional Instructions\n{active_admin_prompt.custom_instructions}"

        content_parts = [{"type": "text", "text": system_prompt}]

        if pdf:
            pdf.seek(0)
            pdf_bytes = pdf.read()
            b64 = base64.b64encode(pdf_bytes).decode("utf-8")

            # Use gpt-4o's native PDF/vision support if available
            content_parts.append(
                {
                    "type": "input_file",
                    "filename": pdf.name,
                    "file_data": f"data:application/pdf;base64,{b64}",
                }
            )

        # Call OpenAI with proper model and error handling
        try:
            if hasattr(client, "responses"):
                # The newer responses API expects "input_text" instead of "text"
                response_content = []
                for part in content_parts:
                    p = part.copy()
                    if p.get("type") == "text":
                        p["type"] = "input_text"
                    response_content.append(p)

                resp = client.responses.create(
                    model="gpt-4o",
                    input=[{"role": "user", "content": response_content}],
                )
                report.openai_response_id = getattr(resp, "id", "")
                report.ai_summary_text = getattr(resp, "output_text", "") or ""
                report.ai_raw = resp.model_dump() if hasattr(resp, "model_dump") else {}
            else:
                # Fallback to standard chat completions
                resp = client.chat.completions.create(
                    model="gpt-4o",
                    messages=[{"role": "user", "content": content_parts}],
                )
                report.openai_response_id = resp.id
                report.ai_summary_text = resp.choices[0].message.content
                report.ai_raw = resp.model_dump() if hasattr(resp, "model_dump") else {}

            report.status = "done"
            report.save()

        except Exception as ai_err:
            raise Exception(f"OpenAI API error: {str(ai_err)}")

    except Exception as e:
        report.status = "error"
        report.error_message = str(e)
        report.save()
        print(f"Error processing report {report.id}: {e}")
        return Response(
            ReportDetailSerializer(report).data,
            status=http_status.HTTP_500_INTERNAL_SERVER_ERROR,
        )

    return Response(
        ReportDetailSerializer(report).data, status=http_status.HTTP_201_CREATED
    )
