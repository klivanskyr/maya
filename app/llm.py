import json
import logging

import anthropic
from sqlalchemy import select

from app.config import settings
from app.database import async_session
from app.models import KeyFact, Message, Summary, User

logger = logging.getLogger(__name__)

# Model mapping
MODELS = {
    "haiku": "claude-haiku-4-5-20251001",
    "sonnet": "claude-sonnet-4-6-20260414",
}

client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)


def estimate_tokens(text: str) -> int:
    """Rough token estimate: ~4 chars per token for English text."""
    return max(1, len(text) // 4)


async def get_key_facts_text(user_id: int) -> str:
    """Load key facts for a user and format as text."""
    async with async_session() as session:
        result = await session.execute(
            select(KeyFact).where(KeyFact.user_id == user_id)
        )
        facts = result.scalars().all()
        if not facts:
            return ""
        lines = [f"- {f.key}: {f.value}" for f in facts]
        return "Known facts about this user:\n" + "\n".join(lines)


async def get_summary_text(user_id: int) -> str:
    """Load the rolling summary for a user."""
    async with async_session() as session:
        result = await session.execute(
            select(Summary).where(Summary.user_id == user_id)
        )
        summary = result.scalar_one_or_none()
        if summary is None:
            return ""
        return f"Summary of earlier conversations:\n{summary.content}"


async def get_recent_messages(user_id: int, token_budget: int) -> list[dict]:
    """Load recent non-compacted messages, newest first, up to token budget."""
    async with async_session() as session:
        result = await session.execute(
            select(Message)
            .where(Message.user_id == user_id, Message.compacted == False)  # noqa: E712
            .order_by(Message.created_at.desc())
        )
        all_messages = result.scalars().all()

    messages = []
    tokens_used = 0
    for msg in all_messages:
        msg_tokens = msg.token_count or estimate_tokens(msg.content)
        if tokens_used + msg_tokens > token_budget:
            break
        messages.append({"role": msg.role, "content": msg.content})
        tokens_used += msg_tokens

    messages.reverse()  # Chronological order
    return messages


async def assemble_context(user_id: int) -> tuple[str, list[dict]]:
    """Build system prompt and message list for Claude API.
    Returns (system_prompt, messages)."""
    max_tokens = settings.max_context_tokens

    # System prompt with key facts and summary injected
    system_content = settings.maya_system_prompt

    facts_text = await get_key_facts_text(user_id)
    facts_tokens = estimate_tokens(facts_text) if facts_text else 0

    summary_text = await get_summary_text(user_id)
    summary_tokens = estimate_tokens(summary_text) if summary_text else 0

    if facts_text:
        system_content += f"\n\n{facts_text}"
    if summary_text:
        system_content += f"\n\n{summary_text}"

    system_tokens = estimate_tokens(system_content)

    # Remaining budget for recent messages
    overhead = system_tokens + facts_tokens + summary_tokens
    remaining_budget = max(0, max_tokens - overhead - 1024)  # Reserve for response

    recent = await get_recent_messages(user_id, remaining_budget)

    return system_content, recent


async def generate_response(
    user_id: int, user_message: str, model_key: str = "haiku"
) -> tuple[str, int, int, str]:
    """Generate a response from Claude API.
    Returns (response_text, input_tokens, output_tokens, model_used)."""
    system_prompt, messages = await assemble_context(user_id)

    # Add the current message
    messages.append({"role": "user", "content": user_message})

    # Ensure messages alternate properly (Claude requires this)
    messages = _fix_message_order(messages)

    model = MODELS.get(model_key, MODELS["haiku"])

    response = await client.messages.create(
        model=model,
        max_tokens=1024,
        system=system_prompt,
        messages=messages,
    )

    response_text = response.content[0].text
    input_tokens = response.usage.input_tokens
    output_tokens = response.usage.output_tokens

    return response_text, input_tokens, output_tokens, model_key


def _fix_message_order(messages: list[dict]) -> list[dict]:
    """Ensure messages alternate user/assistant. Claude requires this."""
    if not messages:
        return messages

    fixed = []
    for msg in messages:
        if fixed and fixed[-1]["role"] == msg["role"]:
            # Merge consecutive same-role messages
            fixed[-1]["content"] += "\n" + msg["content"]
        else:
            fixed.append(dict(msg))
    return fixed


async def get_total_uncompacted_tokens(user_id: int) -> int:
    """Get total token count of non-compacted messages for a user."""
    async with async_session() as session:
        result = await session.execute(
            select(Message)
            .where(Message.user_id == user_id, Message.compacted == False)  # noqa: E712
        )
        messages = result.scalars().all()
        return sum(m.token_count or estimate_tokens(m.content) for m in messages)


async def compact_history(user_id: int) -> None:
    """Summarize older messages and extract key facts when context gets too large."""
    total_tokens = await get_total_uncompacted_tokens(user_id)
    threshold = int(settings.max_context_tokens * settings.compaction_threshold)

    if total_tokens < threshold:
        return

    logger.info(f"Compacting history for user {user_id} ({total_tokens} tokens > {threshold})")

    async with async_session() as session:
        result = await session.execute(
            select(Message)
            .where(Message.user_id == user_id, Message.compacted == False)  # noqa: E712
            .order_by(Message.created_at.asc())
        )
        all_messages = result.scalars().all()

        if len(all_messages) <= 4:
            return

        to_compact = all_messages[:-4]
        last_compacted = to_compact[-1]

        conversation_text = "\n".join(
            f"{m.role}: {m.content}" for m in to_compact
        )

        # Summarize via Claude
        try:
            summary_response = await client.messages.create(
                model=MODELS["haiku"],
                max_tokens=512,
                system=(
                    "You are a summarization assistant. Summarize the following conversation, "
                    "preserving key facts, user preferences, important context, and the general tone. "
                    "Be concise but thorough."
                ),
                messages=[{"role": "user", "content": conversation_text}],
            )
            summary_text = summary_response.content[0].text
        except Exception as e:
            logger.error(f"Compaction summarization failed for user {user_id}: {e}")
            return

        # Extract key facts
        await extract_key_facts(user_id, conversation_text)

        # Upsert summary
        existing = await session.execute(
            select(Summary).where(Summary.user_id == user_id)
        )
        summary = existing.scalar_one_or_none()
        if summary:
            summary.content = summary_text
            summary.token_count = estimate_tokens(summary_text)
            summary.last_compacted_message_id = last_compacted.id
        else:
            summary = Summary(
                user_id=user_id,
                content=summary_text,
                token_count=estimate_tokens(summary_text),
                last_compacted_message_id=last_compacted.id,
            )
            session.add(summary)

        # Mark messages as compacted
        for msg in to_compact:
            m = await session.get(Message, msg.id)
            m.compacted = True

        await session.commit()
        logger.info(f"Compacted {len(to_compact)} messages for user {user_id}")


async def extract_key_facts(user_id: int, conversation_text: str) -> None:
    """Extract structured key facts from conversation text via Claude."""
    try:
        result = await client.messages.create(
            model=MODELS["haiku"],
            max_tokens=512,
            system=(
                "Extract key facts about the user from this conversation. "
                "Return a JSON array of objects with keys: category, key, value. "
                "Categories: name, location, preference, date, other. "
                "Only extract facts that are clearly stated. Return [] if none found. "
                'Example: [{"category": "name", "key": "first_name", "value": "Ryan"}]'
            ),
            messages=[{"role": "user", "content": conversation_text}],
        )
        raw = result.content[0].text.strip()

        # Strip markdown code fences if present
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0]

        facts = json.loads(raw)
        if not isinstance(facts, list):
            return

        # Check key fact limit for free users
        async with async_session() as session:
            user = await session.get(User, user_id)
            existing_count_result = await session.execute(
                select(KeyFact).where(KeyFact.user_id == user_id)
            )
            existing_facts = existing_count_result.scalars().all()
            existing_count = len(existing_facts)

            max_facts = 25 if user.tier == "free" else 999999

            for fact in facts:
                if not all(k in fact for k in ("category", "key", "value")):
                    continue

                # Check if this fact already exists (update, not new)
                existing = await session.execute(
                    select(KeyFact).where(
                        KeyFact.user_id == user_id, KeyFact.key == fact["key"]
                    )
                )
                kf = existing.scalar_one_or_none()
                if kf:
                    kf.value = fact["value"]
                    kf.category = fact["category"]
                else:
                    if existing_count >= max_facts:
                        # Replace oldest "other" fact for free users
                        oldest_other = await session.execute(
                            select(KeyFact)
                            .where(KeyFact.user_id == user_id, KeyFact.category == "other")
                            .order_by(KeyFact.updated_at.asc())
                            .limit(1)
                        )
                        old_fact = oldest_other.scalar_one_or_none()
                        if old_fact:
                            await session.delete(old_fact)
                            existing_count -= 1
                        else:
                            continue  # Can't add more facts

                    session.add(
                        KeyFact(
                            user_id=user_id,
                            category=fact["category"],
                            key=fact["key"],
                            value=fact["value"],
                        )
                    )
                    existing_count += 1

            await session.commit()

    except (json.JSONDecodeError, Exception) as e:
        logger.warning(f"Failed to extract key facts for user {user_id}: {e}")
