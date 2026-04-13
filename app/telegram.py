import asyncio
import io
import logging
from datetime import datetime

from sqlalchemy import func, select
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from app.config import settings
from app.database import async_session
from app.llm import compact_history, estimate_tokens, generate_response
from app.models import Conversation, KeyFact, Message, User
from app.quota import check_message_quota, increment_message_count

logger = logging.getLogger(__name__)

# Track users in onboarding (waiting for name reply)
_onboarding_users: set[int] = set()


# ─── User & Conversation helpers ─────────────────────────────────────

async def get_or_create_user(telegram_user) -> User:
    async with async_session() as session:
        result = await session.execute(
            select(User).where(User.telegram_id == telegram_user.id)
        )
        user = result.scalar_one_or_none()
        if user is None:
            user = User(
                telegram_id=telegram_user.id,
                username=telegram_user.username,
                first_name=telegram_user.first_name,
                messages_reset_at=datetime.utcnow(),
            )
            session.add(user)
            await session.commit()
            await session.refresh(user)
        else:
            user.username = telegram_user.username
            user.first_name = telegram_user.first_name
            await session.commit()
        return user


async def get_or_create_conversation(user: User) -> Conversation:
    from datetime import timedelta

    timeout = timedelta(minutes=settings.conversation_timeout_minutes)
    async with async_session() as session:
        result = await session.execute(
            select(Conversation)
            .where(Conversation.user_id == user.id)
            .order_by(Conversation.last_message_at.desc())
            .limit(1)
        )
        conversation = result.scalar_one_or_none()

        if conversation is None or (datetime.utcnow() - conversation.last_message_at) > timeout:
            conversation = Conversation(user_id=user.id)
            session.add(conversation)
            await session.commit()
            await session.refresh(conversation)
        return conversation


async def store_message(
    user: User,
    conversation: Conversation,
    role: str,
    content: str,
    token_count: int = 0,
    model_used: str | None = None,
    telegram_message_id: int | None = None,
) -> Message:
    async with async_session() as session:
        message = Message(
            user_id=user.id,
            conversation_id=conversation.id,
            role=role,
            content=content,
            token_count=token_count,
            model_used=model_used,
            telegram_message_id=telegram_message_id,
        )
        session.add(message)

        conv = await session.get(Conversation, conversation.id)
        conv.last_message_at = datetime.utcnow()
        conv.message_count += 1

        await session.commit()
        await session.refresh(message)
        return message


# ─── Command handlers ────────────────────────────────────────────────

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await get_or_create_user(update.effective_user)

    if user.onboarding_complete:
        # Returning user
        name = await _get_user_name(user.id)
        if name:
            await update.message.reply_text(f"Hey {name}! Text me anytime.")
        else:
            await update.message.reply_text("Hey! Text me anytime.")
    else:
        # New user — ask for name
        _onboarding_users.add(user.telegram_id)
        await update.message.reply_text("Hey! I'm Maya. What's your name?")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "*Maya Commands*\n\n"
        "/help — Show this list\n"
        "/memory — See what I remember about you\n"
        "/forget \\[fact\\] — Delete a memory\n"
        "/plan — View your plan and usage\n"
        "/upgrade — Upgrade to Maya Plus\n"
        "/stats — Your usage statistics\n"
        "/export — Export your chat history \\(Plus\\)\n"
        "/settings — Adjust preferences \\(Plus\\)\n\n"
        "Or just send me a message — I'm always here to chat\\!",
        parse_mode="MarkdownV2",
    )


async def memory_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await get_or_create_user(update.effective_user)

    async with async_session() as session:
        result = await session.execute(
            select(KeyFact).where(KeyFact.user_id == user.id).order_by(KeyFact.category)
        )
        facts = result.scalars().all()

    if not facts:
        await update.message.reply_text(
            "I haven't learned anything about you yet! "
            "Just keep chatting and I'll pick things up."
        )
        return

    # Group by category
    grouped: dict[str, list[str]] = {}
    category_labels = {
        "name": "Name & Identity",
        "location": "Location",
        "preference": "Preferences",
        "date": "Important Dates",
        "other": "Other",
    }
    for fact in facts:
        label = category_labels.get(fact.category, fact.category.title())
        grouped.setdefault(label, []).append(f"  - {fact.key}: {fact.value}")

    lines = ["What I know about you:\n"]
    for category, items in grouped.items():
        lines.append(f"[{category}]")
        lines.extend(items)
        lines.append("")

    # Count and limit info
    count = len(facts)
    if user.tier == "free":
        lines.append(f"{count} of 25 facts stored (Free plan).")
        if count >= 20:
            lines.append("Want me to remember more? /upgrade to Maya Plus.")
    else:
        lines.append(f"{count} facts stored (unlimited).")

    await update.message.reply_text("\n".join(lines))


async def forget_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await get_or_create_user(update.effective_user)

    # Parse query from command args
    query = " ".join(context.args) if context.args else ""
    if not query:
        await update.message.reply_text(
            "What should I forget? Use /forget [keyword], like:\n"
            "/forget dog's name"
        )
        return

    async with async_session() as session:
        result = await session.execute(
            select(KeyFact).where(KeyFact.user_id == user.id)
        )
        all_facts = result.scalars().all()

        # Search for matches (case-insensitive on key and value)
        query_lower = query.lower()
        matches = [
            f for f in all_facts
            if query_lower in f.key.lower() or query_lower in f.value.lower()
        ]

        if not matches:
            await update.message.reply_text(
                f"I couldn't find a memory matching '{query}'. "
                "Use /memory to see everything I know about you."
            )
            return

        if len(matches) == 1:
            fact = matches[0]
            fact_to_delete = await session.get(KeyFact, fact.id)
            await session.delete(fact_to_delete)
            await session.commit()
            await update.message.reply_text(
                f"Done — I've forgotten that {fact.key}: {fact.value}"
            )
        else:
            lines = ["I found multiple matches. Which one?\n"]
            for f in matches:
                lines.append(f"  - {f.key}: {f.value}")
            lines.append("\nTry being more specific with /forget [keyword].")
            await update.message.reply_text("\n".join(lines))


async def upgrade_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await get_or_create_user(update.effective_user)

    if user.tier == "plus":
        await update.message.reply_text(
            "You're already on Maya Plus! Type /plan to see your usage."
        )
        return

    from app.stripe_billing import generate_checkout_token

    token = generate_checkout_token(user.id)
    checkout_url = f"{settings.app_url}/checkout/{token}"
    await update.message.reply_text(
        "✨ *Maya Plus — \\$9/month*\n\n"
        "• Unlimited messages \\(you're currently limited to 15/day\\)\n"
        "• Unlimited memory \\(currently limited to 25 facts\\)\n"
        "• Access to Sonnet 4\\.6, a more capable AI model\n"
        "• Full memory management\n"
        "• Chat export\n\n"
        f"[Upgrade Now]({checkout_url})\n\n"
        "You'll be taken to a secure checkout page\\.",
        parse_mode="MarkdownV2",
    )


async def plan_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await get_or_create_user(update.effective_user)

    async with async_session() as session:
        u = await session.get(User, user.id)
        fact_count_result = await session.execute(
            select(func.count(KeyFact.id)).where(KeyFact.user_id == user.id)
        )
        fact_count = fact_count_result.scalar() or 0

    if u.tier == "plus":
        model_name = "Sonnet 4.6" if u.preferred_model == "sonnet" else "Haiku 4.5"
        text = (
            "Your Plan: Maya Plus\n\n"
            f"  Messages today: {u.messages_today} (unlimited)\n"
            f"  Memory: {fact_count} facts (unlimited)\n"
            f"  AI model: {model_name} (send /settings to switch)\n\n"
            "Thanks for being a Plus member!"
        )
    else:
        limit = settings.default_daily_messages
        text = (
            "Your Plan: Free\n\n"
            f"  Messages today: {u.messages_today} / {limit}\n"
            f"  Memory: {fact_count} / 25 facts\n"
            "  AI model: Haiku 4.5\n"
            "  Resets at: midnight UTC\n\n"
            "Want unlimited everything? /upgrade"
        )

    await update.message.reply_text(text)


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await get_or_create_user(update.effective_user)

    async with async_session() as session:
        total_messages = (
            await session.execute(
                select(func.count(Message.id)).where(Message.user_id == user.id)
            )
        ).scalar() or 0

        total_conversations = (
            await session.execute(
                select(func.count(Conversation.id)).where(Conversation.user_id == user.id)
            )
        ).scalar() or 0

        fact_count = (
            await session.execute(
                select(func.count(KeyFact.id)).where(KeyFact.user_id == user.id)
            )
        ).scalar() or 0

        u = await session.get(User, user.id)

    member_since = user.created_at.strftime("%B %d, %Y")

    await update.message.reply_text(
        "Your Stats\n\n"
        f"  Member since: {member_since}\n"
        f"  Total messages: {total_messages}\n"
        f"  Total conversations: {total_conversations}\n"
        f"  Memories stored: {fact_count}\n"
        f"  Messages today: {u.messages_today}\n"
        f"  Current plan: {u.tier.title()}"
    )


async def export_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await get_or_create_user(update.effective_user)

    if user.tier != "plus":
        await update.message.reply_text("Chat export is a Plus feature. /upgrade to unlock it.")
        return

    async with async_session() as session:
        result = await session.execute(
            select(Message)
            .where(Message.user_id == user.id)
            .order_by(Message.created_at.desc())
            .limit(10000)
        )
        messages = list(reversed(result.scalars().all()))

    if not messages:
        await update.message.reply_text("No messages to export yet!")
        return

    lines = []
    for msg in messages:
        ts = msg.created_at.strftime("%Y-%m-%d %H:%M")
        role = "You" if msg.role == "user" else "Maya"
        lines.append(f"[{ts}] {role}: {msg.content}")

    content = "\n\n".join(lines)
    file = io.BytesIO(content.encode("utf-8"))
    file.name = "maya-chat-export.txt"

    await update.message.reply_document(document=file, caption="Here's your chat history!")


async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await get_or_create_user(update.effective_user)

    if user.tier != "plus":
        await update.message.reply_text(
            "Settings are available for Plus subscribers. /upgrade to unlock."
        )
        return

    current = "Sonnet 4.6" if user.preferred_model == "sonnet" else "Haiku 4.5"
    other = "sonnet" if user.preferred_model == "haiku" else "haiku"
    other_label = "Sonnet 4.6" if other == "sonnet" else "Haiku 4.5"

    await update.message.reply_text(
        f"Settings\n\n"
        f"  AI Model: {current}\n\n"
        f"Send /setmodel {other} to switch to {other_label}."
    )


async def setmodel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await get_or_create_user(update.effective_user)

    if user.tier != "plus":
        await update.message.reply_text("Model selection is a Plus feature. /upgrade to unlock.")
        return

    choice = context.args[0].lower() if context.args else ""
    if choice not in ("haiku", "sonnet"):
        await update.message.reply_text("Use /setmodel haiku or /setmodel sonnet")
        return

    async with async_session() as session:
        u = await session.get(User, user.id)
        u.preferred_model = choice
        await session.commit()

    label = "Sonnet 4.6" if choice == "sonnet" else "Haiku 4.5"
    await update.message.reply_text(f"Got it — switched to {label}.")


# ─── Message handler ─────────────────────────────────────────────────

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return

    user = await get_or_create_user(update.effective_user)

    # Handle onboarding (waiting for name)
    if user.telegram_id in _onboarding_users:
        _onboarding_users.discard(user.telegram_id)
        name = update.message.text.strip()

        # Store the name as a key fact
        async with async_session() as session:
            session.add(KeyFact(
                user_id=user.id,
                category="name",
                key="first_name",
                value=name,
            ))
            u = await session.get(User, user.id)
            u.onboarding_complete = True
            await session.commit()

        await update.message.reply_text(
            f"Great to meet you, {name}! Text me anytime you have a question."
        )
        return

    # If user hasn't completed onboarding but sends a message (not /start)
    if not user.onboarding_complete:
        _onboarding_users.add(user.telegram_id)
        await update.message.reply_text("Hey! I'm Maya. What's your name?")
        return

    conversation = await get_or_create_conversation(user)

    # Check message quota
    can_send, used, limit = await check_message_quota(user)
    if not can_send:
        await update.message.reply_text(
            f"You've used all {limit} of your daily messages! "
            "Your limit resets at midnight UTC.\n\n"
            "Want unlimited messages? Upgrade to Maya Plus for $9/month — "
            "you also get better AI, unlimited memory, and more.\n\n"
            "/upgrade to get started."
        )
        return

    # Store the incoming message
    await store_message(
        user=user,
        conversation=conversation,
        role="user",
        content=update.message.text,
        telegram_message_id=update.message.message_id,
    )

    # Typing indicator
    async def keep_typing():
        try:
            while True:
                await update.message.chat.send_action(ChatAction.TYPING)
                await asyncio.sleep(4)
        except asyncio.CancelledError:
            pass

    typing_task = asyncio.create_task(keep_typing())

    # Determine model
    model_key = "haiku"
    if user.tier == "plus" and user.preferred_model == "sonnet":
        model_key = "sonnet"

    try:
        reply_text, input_tokens, output_tokens, model_used = await generate_response(
            user.id, update.message.text, model_key=model_key
        )
    except Exception as e:
        logger.error(f"LLM error for user {user.id}: {e}")
        reply_text = "Sorry, I'm having trouble thinking right now. Try again in a moment."
        input_tokens, output_tokens, model_used = 0, 0, None
    finally:
        typing_task.cancel()

    total_tokens = input_tokens + output_tokens

    # Store the response
    await store_message(
        user=user,
        conversation=conversation,
        role="assistant",
        content=reply_text,
        token_count=output_tokens,
        model_used=model_used,
    )
    await increment_message_count(user.id, tokens=total_tokens)

    await update.message.reply_text(reply_text)

    # Compaction check (non-blocking)
    try:
        await compact_history(user.id)
    except Exception as e:
        logger.warning(f"Compaction failed for user {user.id}: {e}")


# ─── Helpers ─────────────────────────────────────────────────────────

async def _get_user_name(user_id: int) -> str | None:
    """Get the user's stored name from key facts."""
    async with async_session() as session:
        result = await session.execute(
            select(KeyFact).where(
                KeyFact.user_id == user_id, KeyFact.key == "first_name"
            )
        )
        fact = result.scalar_one_or_none()
        return fact.value if fact else None


# ─── Bot setup ───────────────────────────────────────────────────────

def create_bot_app() -> Application:
    app = Application.builder().token(settings.telegram_bot_token).build()

    # Commands
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("memory", memory_command))
    app.add_handler(CommandHandler("forget", forget_command))
    app.add_handler(CommandHandler("upgrade", upgrade_command))
    app.add_handler(CommandHandler("plan", plan_command))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("export", export_command))
    app.add_handler(CommandHandler("settings", settings_command))
    app.add_handler(CommandHandler("setmodel", setmodel_command))

    # Regular messages
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    return app


async def run_bot_polling(bot_app: Application) -> None:
    await bot_app.initialize()
    await bot_app.start()
    await bot_app.updater.start_polling()
    logger.info("Telegram bot started polling")


async def stop_bot_polling(bot_app: Application) -> None:
    await bot_app.updater.stop()
    await bot_app.stop()
    await bot_app.shutdown()
    logger.info("Telegram bot stopped")
