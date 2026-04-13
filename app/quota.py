from datetime import date, datetime

from sqlalchemy import select

from app.config import settings
from app.database import async_session
from app.models import DailyUsage, User


async def check_message_quota(user: User) -> tuple[bool, int, int]:
    """Check if user has remaining message quota.
    Returns (can_send, messages_used, message_limit).
    Plus users always return True with limit=0 (unlimited)."""
    if user.tier == "plus":
        return True, user.messages_today, 0

    limit = settings.default_daily_messages

    async with async_session() as session:
        u = await session.get(User, user.id)

        # Reset if past midnight UTC
        now = datetime.utcnow()
        if now.date() > u.messages_reset_at.date():
            u.messages_today = 0
            u.messages_reset_at = now
            await session.commit()

        return u.messages_today < limit, u.messages_today, limit


async def increment_message_count(user_id: int, tokens: int = 0) -> None:
    """Increment daily message count and update daily usage log."""
    today = date.today()

    async with async_session() as session:
        # Update user's fast counter
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
