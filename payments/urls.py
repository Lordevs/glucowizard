from django.urls import path
from .views import (
    get_plans,
    create_checkout_session,
    stripe_webhook,
    get_session_status,
    cancel_subscription,
    reactivate_subscription,
)

urlpatterns = [
    path("plans/", get_plans, name="get-plans"),
    path(
        "create-checkout-session/",
        create_checkout_session,
        name="create-checkout-session",
    ),
    path("session-status/", get_session_status, name="session-status"),
    path("cancel-subscription/", cancel_subscription, name="cancel-subscription"),
    path(
        "reactivate-subscription/",
        reactivate_subscription,
        name="reactivate-subscription",
    ),
    path("webhook/", stripe_webhook, name="stripe-webhook"),
]
