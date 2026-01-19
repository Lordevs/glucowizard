import stripe
import os
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated, AllowAny
from rest_framework.response import Response
from rest_framework import status
from django.views.decorators.csrf import csrf_exempt
from .models import Subscription, UserCredit
from django.contrib.auth import get_user_model

User = get_user_model()

stripe.api_key = os.getenv("STRIPE_SECRET_KEY")


@api_view(["GET"])
@permission_classes([AllowAny])
def get_plans(request):
    """
    Fetches all active products from Stripe that have the 'plan_type' metadata,
    and returns their associated prices.
    """
    try:
        # 1. Fetch active products
        products = stripe.Product.list(active=True)

        valid_plan_types = ["pay_as_you_go", "monthly", "yearly"]
        plans = []

        for product in products.data:
            # Check metadata on the Product object
            plan_type = product.metadata.get("plan_type")

            if plan_type in valid_plan_types:
                # 2. Fetch active prices for this specific product
                prices = stripe.Price.list(product=product.id, active=True)

                for price in prices.data:
                    plans.append(
                        {
                            "id": price.id,
                            "product_id": product.id,
                            "name": product.name,
                            "description": product.description,
                            "unit_amount": price.unit_amount
                            / 100,  # Convert cents to dollars
                            "currency": price.currency,
                            "type": price.type,  # 'one_time' or 'recurring'
                            "interval": price.recurring.interval
                            if price.recurring
                            else None,
                            "plan_type": plan_type,
                        }
                    )

        return Response(plans)
    except Exception as e:
        return Response({"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def create_checkout_session(request):
    """
    Creates a Stripe Checkout session for a specific price ID.
    """
    price_id = request.data.get("price_id")
    if not price_id:
        return Response(
            {"error": "price_id is required"}, status=status.HTTP_400_BAD_REQUEST
        )

    user = request.user

    # Define success/cancel URLs based on environment
    is_prod = os.getenv("DJANGO_DEBUG", "True").lower() == "false"
    base_url = "https://glucowizard.com" if is_prod else "http://localhost:3000"

    try:
        # 1. Fetch the price and expand the product to get its metadata
        price = stripe.Price.retrieve(price_id, expand=["product"])
        mode = "subscription" if price.type == "recurring" else "payment"

        # Prioritize Product metadata for plan_type as the source of truth
        plan_type = price.product.metadata.get("plan_type") or price.metadata.get(
            "plan_type"
        )

        # 2. Ensure we have a Stripe Customer ID for this user
        if not user.stripe_customer_id:
            customer = stripe.Customer.create(
                email=user.email, metadata={"user_id": user.id}
            )
            user.stripe_customer_id = customer.id
            user.save()

        # 3. Create Checkout Session
        checkout_session = stripe.checkout.Session.create(
            customer=user.stripe_customer_id,
            line_items=[{"price": price_id, "quantity": 1}],
            mode=mode,
            success_url=f"{base_url}/dashboard/success?session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{base_url}/pricing",
            metadata={"user_id": user.id, "plan_type": plan_type},
        )

        return Response({"url": checkout_session.url})
    except Exception as e:
        return Response({"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def get_session_status(request):
    """
    Checks if a checkout session was successful and if the backend has processed it.
    Used by the frontend success page.
    """
    session_id = request.query_params.get("session_id")
    if not session_id:
        return Response(
            {"error": "session_id is required"}, status=status.HTTP_400_BAD_REQUEST
        )

    try:
        session = stripe.checkout.Session.retrieve(session_id)

        # Check if the webhook has already created the local records
        user = request.user
        has_sub = Subscription.objects.filter(
            user=user, status__in=["active", "trialing"]
        ).exists()
        has_credits = UserCredit.objects.filter(user=user, balance__gt=0).exists()

        return Response(
            {
                "status": session.status,
                "payment_status": session.payment_status,
                "is_processed": has_sub or has_credits,  # Simple check
            }
        )
    except Exception as e:
        return Response({"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def cancel_subscription(request):
    """
    Cancels the user's active subscription at the end of the period.
    """
    try:
        user = request.user
        subscription = Subscription.objects.filter(user=user, status="active").first()

        if not subscription:
            return Response(
                {"error": "No active subscription found"},
                status=status.HTTP_404_NOT_FOUND,
            )

        # Cancel in Stripe at period end
        stripe.Subscription.modify(
            subscription.stripe_subscription_id, cancel_at_period_end=True
        )

        # Update local state
        subscription.cancel_at_period_end = True
        subscription.save()

        return Response(
            {
                "message": "Subscription will be canceled at the end of the billing period."
            }
        )
    except Exception as e:
        return Response({"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@csrf_exempt
@api_view(["POST"])
@permission_classes([AllowAny])
def stripe_webhook(request):
    """
    Handles Stripe Webhooks to sync subscription status and credits.
    """
    payload = request.body
    sig_header = request.META.get("HTTP_STRIPE_SIGNATURE")
    endpoint_secret = os.getenv("STRIPE_WEBHOOK_SECRET")

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, endpoint_secret)
    except Exception as e:
        return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)
    except stripe.SignatureVerificationError as e:
        return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)

    # Handle the event
    print(f"Webhook received: {event['type']}")

    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        handle_checkout_completed(session)
    elif event["type"] in [
        "customer.subscription.updated",
        "customer.subscription.deleted",
    ]:
        subscription = event["data"]["object"]
        handle_subscription_sync(subscription)

    return Response({"status": "success"})


def handle_checkout_completed(session):
    user_id = session.metadata.get("user_id")
    plan_type = session.metadata.get("plan_type")

    print(f"Processing checkout completed for User ID: {user_id}, Plan: {plan_type}")

    try:
        user = User.objects.get(id=user_id)
    except User.DoesNotExist:
        print(f"User with ID {user_id} not found.")
        return

    if session.mode == "payment" and plan_type == "pay_as_you_go":
        # Increment user credits
        credits, created = UserCredit.objects.get_or_create(user=user)
        credits.balance += 1
        credits.save()
        print(f"Incremented credits for User {user_id}. New balance: {credits.balance}")

    elif session.mode == "subscription":
        # Subscription syncing happens via customer.subscription.created or updated
        # but we can ensure a link here too if needed.
        pass


def handle_subscription_sync(stripe_sub):
    customer_id = stripe_sub.customer
    try:
        user = User.objects.get(stripe_customer_id=customer_id)
    except User.DoesNotExist:
        return

    from django.utils import timezone
    from datetime import datetime

    # Convert timestamp to aware datetime
    period_end = datetime.fromtimestamp(stripe_sub.current_period_end, tz=timezone.utc)

    # Get plan_type from product metadata (primary) or price metadata
    price_id = stripe_sub["items"]["data"][0]["price"]["id"]
    price = stripe.Price.retrieve(price_id, expand=["product"])
    plan_type = (
        price.product.metadata.get("plan_type")
        or price.metadata.get("plan_type")
        or "monthly"
    )

    Subscription.objects.update_or_create(
        user=user,
        defaults={
            "stripe_subscription_id": stripe_sub.id,
            "stripe_price_id": price_id,
            "plan_type": plan_type,
            "status": stripe_sub.status,
            "current_period_end": period_end,
            "cancel_at_period_end": stripe_sub.cancel_at_period_end,
        },
    )
    print(f"Synced subscription for User {user.id}. Status: {stripe_sub.status}")
