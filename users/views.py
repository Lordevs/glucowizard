from rest_framework.decorators import api_view, permission_classes, parser_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework import status
from rest_framework.parsers import MultiPartParser, FormParser

from django.contrib.auth import get_user_model
from glucowizard.supabase_client import get_supabase
from .serializers import RegisterSerializer
from payments.models import Subscription, UserCredit

User = get_user_model()


def get_user_subscription_info(user):
    """Helper to get subscription and credit info for a user."""
    sub = Subscription.objects.filter(user=user).first()
    credits = UserCredit.objects.filter(user=user).first()

    return {
        "is_subscribed": sub.status in ["active", "trialing"] if sub else False,
        "plan_type": sub.plan_type if sub else None,
        "subscription_status": sub.status if sub else None,
        "cancel_at_period_end": sub.cancel_at_period_end if sub else False,
        "current_period_end": sub.current_period_end if sub else None,
        "credits": credits.balance if credits else 0,
    }


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def me(request):
    """
    Returns the authenticated user's information.
    """
    user = request.user
    data = {
        "id": user.id,
        "username": user.username,
        "email": user.email,
        "avatar_url": user.avatar_url,
    }
    data.update(get_user_subscription_info(user))
    return Response(data)


@api_view(["POST"])
@permission_classes([AllowAny])
def register(request):
    serializer = RegisterSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    user = serializer.save()

    # We return the user info.
    # Signup might require email verification depending on Supabase settings.
    return Response(
        {
            "user": {
                "id": user.id,
                "username": user.username,
                "email": user.email,
            },
            "message": "User registered successfully. Please check your email for verification.",
        },
        status=status.HTTP_201_CREATED,
    )


@api_view(["POST"])
@permission_classes([AllowAny])
def login(request):
    email = request.data.get("email")  # Supabase uses email
    password = request.data.get("password")

    if not email or not password:
        return Response(
            {"error": "email and password are required"},
            status=status.HTTP_400_BAD_REQUEST,
        )

    supabase = get_supabase()
    try:
        response = supabase.auth.sign_in_with_password(
            {"email": email, "password": password}
        )

        if not response.session:
            return Response(
                {"error": "Invalid credentials"}, status=status.HTTP_401_UNAUTHORIZED
            )

        # Sync/Ensure local user exists (if they signed up directly via Supabase)
        user, _ = User.objects.get_or_create(
            email=email, defaults={"username": email.split("@")[0]}
        )

        return Response(
            {
                "user": {
                    "id": user.id,
                    "username": user.username,
                    "email": user.email,
                    "avatar_url": user.avatar_url,
                    **get_user_subscription_info(user),
                },
                "session": {
                    "access_token": response.session.access_token,
                    "refresh_token": response.session.refresh_token,
                    "expires_at": response.session.expires_at,
                },
            }
        )
    except Exception as e:
        return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)


@api_view(["POST"])
@permission_classes([AllowAny])
def refresh_token(request):
    refresh_token = request.data.get("refresh_token")
    if not refresh_token:
        return Response(
            {"error": "refresh_token is required"}, status=status.HTTP_400_BAD_REQUEST
        )

    supabase = get_supabase()
    try:
        response = supabase.auth.refresh_session(refresh_token)
        if not response.session:
            return Response(
                {"error": "Invalid refresh token"}, status=status.HTTP_401_UNAUTHORIZED
            )

        return Response(
            {
                "session": {
                    "access_token": response.session.access_token,
                    "refresh_token": response.session.refresh_token,
                    "expires_at": response.session.expires_at,
                }
            }
        )
    except Exception as e:
        return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)


@api_view(["POST"])
@permission_classes([AllowAny])
def forgot_password(request):
    email = request.data.get("email")
    if not email:
        return Response(
            {"error": "email is required"}, status=status.HTTP_400_BAD_REQUEST
        )

    supabase = get_supabase()
    try:
        supabase.auth.reset_password_for_email(email)
        return Response({"message": "Password reset email sent"})
    except Exception as e:
        return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def update_password(request):
    """Update password without old password verification (e.g. after reset)"""
    new_password = request.data.get("password")
    if not new_password:
        return Response(
            {"error": "new password is required"}, status=status.HTTP_400_BAD_REQUEST
        )

    supabase = get_supabase()
    # Set the session using the token from request.auth
    supabase.auth.set_session(request.auth, "")

    try:
        supabase.auth.update_user({"password": new_password})
        return Response({"message": "Password updated successfully"})
    except Exception as e:
        return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def logout(request):
    """
    Logs out the user by revoking their session in Supabase.
    """
    supabase = get_supabase()
    # Use the current access token to set the session for the sign_out call
    supabase.auth.set_session(request.auth, "")
    try:
        supabase.auth.sign_out()
        return Response({"message": "Logged out successfully"})
    except Exception as e:
        return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def change_password(request):
    """Change password by verifying old password first"""
    old_password = request.data.get("old_password")
    new_password = request.data.get("new_password")

    if not old_password or not new_password:
        return Response(
            {"error": "Both old_password and new_password are required"},
            status=status.HTTP_400_BAD_REQUEST,
        )

    supabase = get_supabase()
    try:
        # 1. Verify old password by attempting a login
        email = request.user.email
        verify_resp = supabase.auth.sign_in_with_password(
            {"email": email, "password": old_password}
        )

        if not verify_resp.session:
            return Response(
                {"error": "Invalid old password"}, status=status.HTTP_401_UNAUTHORIZED
            )

        # 2. Update to new password (the verify_resp call updated the client session already)
        supabase.auth.update_user({"password": new_password})
        return Response({"message": "Password changed successfully"})

    except Exception as e:
        return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
@parser_classes([MultiPartParser, FormParser])
def upload_avatar(request):
    file = request.FILES.get("avatar")
    if not file:
        return Response(
            {"error": "No file provided"}, status=status.HTTP_400_BAD_REQUEST
        )

    supabase = get_supabase()
    # Set the session using the user's access token stored in request.auth
    supabase.auth.set_session(request.auth, "")

    file_path = f"avatars/{request.user.id}_{file.name}"

    try:
        # Delete old avatar if it exists and is different from the new one
        old_avatar_url = request.user.avatar_url
        if old_avatar_url:
            # The URL usually looks like: .../storage/v1/object/public/profiles/avatars/ID_name.ext
            if "/public/profiles/" in old_avatar_url:
                old_path = old_avatar_url.split("/public/profiles/")[-1]
                # Strip query parameters if any
                if "?" in old_path:
                    old_path = old_path.split("?")[0]

                # If the filename is different, remove the old one.
                # If it's the same, 'upsert' will handle the overwrite.
                if old_path != file_path:
                    try:
                        supabase.storage.from_("profiles").remove([old_path])
                    except Exception:
                        pass  # Ignore errors in deletion to not block the new upload

        # Upload to Supabase Storage with upsert enabled
        supabase.storage.from_("profiles").upload(
            file_path,
            file.read(),
            {"content-type": file.content_type, "upsert": "true"},
        )

        # Get public URL
        url_resp = supabase.storage.from_("profiles").get_public_url(file_path)

        # Update local user
        request.user.avatar_url = url_resp
        request.user.save()

        return Response({"avatar_url": url_resp})
    except Exception as e:
        return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)


@api_view(["GET"])
@permission_classes([AllowAny])
def google_auth(request):
    """
    Returns the Supabase OAuth URL.
    Frontend should pass ?redirect_to=http://localhost:3000/auth/callback
    """
    frontend_callback = request.query_params.get(
        "redirect_to", "http://localhost:3000/auth/callback"
    )

    supabase = get_supabase()
    try:
        response = supabase.auth.sign_in_with_oauth(
            {"provider": "google", "options": {"redirect_to": frontend_callback}}
        )

        # Extract the code_verifier generated by the SDK to store it in the session
        # Since the backend is stateless across requests, we must persist this for the callback
        verifier = None
        storage = getattr(supabase.auth, "_storage", None)
        if storage and hasattr(storage, "storage"):
            for k, v in storage.storage.items():
                if "verifier" in k:
                    verifier = v
                    break

        if verifier:
            request.session["supabase_code_verifier"] = verifier

        return Response({"url": response.url})
    except Exception as e:
        return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)


@api_view(["GET"])
@permission_classes([AllowAny])
def google_callback(request):
    """
    Handle the redirect from Supabase with the authorization code.
    """
    code = request.GET.get("code")
    if not code:
        return Response(
            {"error": "No code provided"}, status=status.HTTP_400_BAD_REQUEST
        )

    supabase = get_supabase()
    try:
        # Retrieve the code_verifier from the session
        verifier = request.session.get("supabase_code_verifier")
        if "supabase_code_verifier" in request.session:
            del request.session["supabase_code_verifier"]

        # Exchange the code for a session
        res = supabase.auth.exchange_code_for_session(
            {"auth_code": code, "code_verifier": verifier}
        )
        session = res.session
        user_info = res.user

        # Sync with local Django user
        email = user_info.email
        user, created = User.objects.get_or_create(
            email=email,
            defaults={
                "username": user_info.user_metadata.get("username")
                or email.split("@")[0]
            },
        )

        return Response(
            {
                "user": {
                    "id": user.id,
                    "email": user.email,
                    "username": user.username,
                    "avatar_url": user.avatar_url,
                },
                "session": {
                    "access_token": session.access_token,
                    "refresh_token": session.refresh_token,
                    "expires_in": session.expires_in,
                },
            }
        )
    except Exception as e:
        return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)
