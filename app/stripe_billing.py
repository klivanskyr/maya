import asyncio
import hashlib
import hmac
import logging
import time
from datetime import datetime

import stripe
from sqlalchemy import select

from app.config import settings
from app.database import async_session
from app.models import Subscription, User

logger = logging.getLogger(__name__)

stripe.api_key = settings.stripe_secret_key

# Checkout token validity: 1 hour
_CHECKOUT_TOKEN_TTL = 3600


def generate_checkout_token(user_id: int) -> str:
    """Generate a signed, time-limited checkout token for a user."""
    timestamp = int(time.time())
    payload = f"{user_id}:{timestamp}"
    sig = hmac.new(
        settings.stripe_secret_key.encode(), payload.encode(), hashlib.sha256
    ).hexdigest()[:16]
    return f"{user_id}-{timestamp}-{sig}"


def verify_checkout_token(token: str) -> int | None:
    """Verify a checkout token and return the user_id, or None if invalid."""
    try:
        parts = token.split("-")
        if len(parts) != 3:
            return None
        user_id = int(parts[0])
        timestamp = int(parts[1])
        sig = parts[2]

        # Check expiry
        if time.time() - timestamp > _CHECKOUT_TOKEN_TTL:
            return None

        # Verify signature
        payload = f"{user_id}:{timestamp}"
        expected = hmac.new(
            settings.stripe_secret_key.encode(), payload.encode(), hashlib.sha256
        ).hexdigest()[:16]
        if not hmac.compare_digest(sig, expected):
            return None

        return user_id
    except (ValueError, IndexError):
        return None


async def create_checkout_session(user_id: int) -> str | None:
    """Create a Stripe Checkout session and return the URL."""
    async with async_session() as session:
        user = await session.get(User, user_id)
        if not user:
            return None

        telegram_id = user.telegram_id

        # Get or create Stripe customer
        if user.stripe_customer_id:
            customer_id = user.stripe_customer_id
        else:
            try:
                customer = await asyncio.to_thread(
                    stripe.Customer.create,
                    metadata={
                        "user_id": str(user.id),
                        "telegram_id": str(telegram_id),
                    },
                )
                customer_id = customer.id
                user.stripe_customer_id = customer_id
                await session.commit()
            except stripe.error.StripeError as e:
                logger.error(f"Failed to create Stripe customer for user {user_id}: {e}")
                return None

    try:
        checkout_session = await asyncio.to_thread(
            stripe.checkout.Session.create,
            customer=customer_id,
            payment_method_types=["card"],
            line_items=[{"price": settings.stripe_price_id_plus, "quantity": 1}],
            mode="subscription",
            success_url=f"{settings.app_url}/?upgraded=true",
            cancel_url=f"{settings.app_url}/pricing",
            metadata={
                "user_id": str(user_id),
                "telegram_id": str(telegram_id),
            },
        )
        return checkout_session.url
    except stripe.error.StripeError as e:
        logger.error(f"Failed to create checkout session for user {user_id}: {e}")
        return None


async def handle_stripe_webhook(payload: bytes, sig_header: str) -> bool:
    """Process a Stripe webhook event. Returns True if handled successfully."""
    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, settings.stripe_webhook_secret
        )
    except (ValueError, stripe.error.SignatureVerificationError) as e:
        logger.error(f"Stripe webhook verification failed: {e}")
        return False

    event_type = event["type"]
    data = event["data"]["object"]

    try:
        if event_type == "checkout.session.completed":
            await _handle_checkout_completed(data)
        elif event_type == "invoice.paid":
            await _handle_invoice_paid(data)
        elif event_type == "customer.subscription.updated":
            await _handle_subscription_updated(data)
        elif event_type == "customer.subscription.deleted":
            await _handle_subscription_deleted(data)
        elif event_type == "invoice.payment_failed":
            await _handle_payment_failed(data)
    except Exception as e:
        logger.error(f"Error handling Stripe event {event_type}: {e}")
        return False

    return True


async def _handle_checkout_completed(session_data: dict) -> None:
    """Handle successful checkout — activate Plus subscription."""
    user_id = int(session_data.get("metadata", {}).get("user_id", 0))
    subscription_id = session_data.get("subscription")
    customer_id = session_data.get("customer")

    if not user_id or not subscription_id:
        logger.error(f"Missing data in checkout session: user_id={user_id}")
        return

    # Get subscription details from Stripe
    try:
        sub = await asyncio.to_thread(stripe.Subscription.retrieve, subscription_id)
    except stripe.error.StripeError as e:
        logger.error(f"Failed to retrieve subscription {subscription_id}: {e}")
        # Still upgrade the user even if we can't get period details
        async with async_session() as db:
            user = await db.get(User, user_id)
            if user:
                user.tier = "plus"
                user.stripe_customer_id = customer_id
                await db.commit()
        await _send_telegram_message(
            user_id,
            "Welcome to Maya Plus! You now have unlimited messages, "
            "unlimited memory, and access to Sonnet 4.6. Enjoy!",
        )
        return

    async with async_session() as db:
        user = await db.get(User, user_id)
        if not user:
            logger.error(f"User {user_id} not found for checkout completion")
            return

        user.tier = "plus"
        user.stripe_customer_id = customer_id

        # Create or update subscription record
        result = await db.execute(
            select(Subscription).where(Subscription.user_id == user_id)
        )
        existing = result.scalar_one_or_none()

        if existing:
            existing.stripe_subscription_id = subscription_id
            existing.stripe_price_id = sub["items"]["data"][0]["price"]["id"]
            existing.status = "active"
            existing.current_period_start = datetime.fromtimestamp(sub["current_period_start"])
            existing.current_period_end = datetime.fromtimestamp(sub["current_period_end"])
            existing.updated_at = datetime.utcnow()
        else:
            db.add(Subscription(
                user_id=user_id,
                stripe_subscription_id=subscription_id,
                stripe_price_id=sub["items"]["data"][0]["price"]["id"],
                status="active",
                current_period_start=datetime.fromtimestamp(sub["current_period_start"]),
                current_period_end=datetime.fromtimestamp(sub["current_period_end"]),
            ))

        await db.commit()

    await _send_telegram_message(
        user_id,
        "Welcome to Maya Plus! You now have unlimited messages, "
        "unlimited memory, and access to Sonnet 4.6. Enjoy!",
    )
    logger.info(f"User {user_id} upgraded to Plus")


async def _handle_invoice_paid(invoice: dict) -> None:
    """Handle successful recurring payment."""
    subscription_id = invoice.get("subscription")
    if not subscription_id:
        return

    async with async_session() as db:
        result = await db.execute(
            select(Subscription).where(
                Subscription.stripe_subscription_id == subscription_id
            )
        )
        sub = result.scalar_one_or_none()
        if sub:
            sub.status = "active"
            sub.updated_at = datetime.utcnow()

            user = await db.get(User, sub.user_id)
            if user:
                user.tier = "plus"

            await db.commit()


async def _handle_subscription_updated(subscription: dict) -> None:
    """Handle subscription changes (plan change, cancellation scheduled)."""
    subscription_id = subscription.get("id")

    async with async_session() as db:
        result = await db.execute(
            select(Subscription).where(
                Subscription.stripe_subscription_id == subscription_id
            )
        )
        sub = result.scalar_one_or_none()
        if sub:
            sub.status = subscription.get("status", sub.status)
            sub.cancel_at_period_end = subscription.get("cancel_at_period_end", False)
            if subscription.get("current_period_end"):
                sub.current_period_end = datetime.fromtimestamp(
                    subscription["current_period_end"]
                )
            sub.updated_at = datetime.utcnow()
            await db.commit()


async def _handle_subscription_deleted(subscription: dict) -> None:
    """Handle subscription cancellation — downgrade to Free."""
    subscription_id = subscription.get("id")

    async with async_session() as db:
        result = await db.execute(
            select(Subscription).where(
                Subscription.stripe_subscription_id == subscription_id
            )
        )
        sub = result.scalar_one_or_none()
        if sub:
            sub.status = "canceled"
            sub.updated_at = datetime.utcnow()

            user = await db.get(User, sub.user_id)
            if user:
                user.tier = "free"

            # Always commit subscription status, even if user not found
            await db.commit()

            if user:
                await _send_telegram_message(
                    user.id,
                    "Your Maya Plus subscription has ended. You're now on the Free plan "
                    "(15 messages/day, 25 memories). You can /upgrade anytime to come back.",
                )

    logger.info(f"Subscription {subscription_id} canceled")


async def _handle_payment_failed(invoice: dict) -> None:
    """Handle failed payment — warn user."""
    subscription_id = invoice.get("subscription")
    if not subscription_id:
        return

    async with async_session() as db:
        result = await db.execute(
            select(Subscription).where(
                Subscription.stripe_subscription_id == subscription_id
            )
        )
        sub = result.scalar_one_or_none()
        if sub:
            sub.status = "past_due"
            sub.updated_at = datetime.utcnow()
            await db.commit()

            await _send_telegram_message(
                sub.user_id,
                "Heads up — your Maya Plus payment failed. I'll try again in a few days. "
                "If it fails again, your plan will switch back to Free.",
            )


async def _send_telegram_message(user_id: int, text: str) -> None:
    """Send a Telegram message to a user by their internal ID."""
    from telegram import Bot

    async with async_session() as db:
        user = await db.get(User, user_id)
        if not user:
            return

    bot = Bot(token=settings.telegram_bot_token)
    try:
        await bot.send_message(chat_id=user.telegram_id, text=text)
    except Exception as e:
        logger.error(f"Failed to send Telegram message to user {user_id}: {e}")
