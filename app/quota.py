from datetime import date, datetime

from sqlalchemy import select

from app.config import TIERS, settings
from app.database import async_session
from app.models import DailyUsage, User


def get_tier_config(tier: str) -> dict:
    """Get the config for a tier, defaulting to free."""
    return TIERS.get(tier, TIERS["free"])


async def check_message_quota(user: User) -> tuple[bool, int, int, bool]:
    """Check if user has remaining message quota.
    Returns (can_send, messages_used, message_limit, is_overage).
    When over limit, can_send is True but is_overage is True (they pay per message)."""
    tier = get_tier_config(user.tier)
    limit = tier["daily_messages"]

    async with async_session() as session:
        u = await session.get(User, user.id)

        # Reset if past midnight UTC
        now = datetime.utcnow()
        if now.date() > u.messages_reset_at.date():
            u.messages_today = 0
            u.messages_reset_at = now
            await session.commit()

        if u.messages_today < limit:
            return True, u.messages_today, limit, False

        # Over limit — paid users can keep going (overage billing)
        if u.tier in ("pro", "elite"):
            return True, u.messages_today, limit, True

        # Free users are hard-capped
        return False, u.messages_today, limit, False


async def increment_message_count(user_id: int, tokens: int = 0) -> None:
    """Increment daily message count and update daily usage log."""
    today = date.today()

    async with async_session() as session:
        u = await session.get(User, user_id)
        u.messages_today += 1

        # Update daily usage log
        result = await session.execute(
            select(DailyUsage).where(
                DailyUsage.user_id == user_id, DailyUsage.date == today
            )
        )
        usage = result.scalar_one_or_none()
        if usage:
            usage.message_count += 1
            usage.token_count += tokens
        else:
            usage = DailyUsage(
                user_id=user_id,
                date=today,
                message_count=1,
                token_count=tokens,
            )
            session.add(usage)

        await session.commit()
