import json
import logging

import anthropic
from sqlalchemy import select

from app.config import settings
from app.database import async_session
from app.models import KeyFact, Message, Summary, User

logger = logging.getLogger(__name__)

# Single model — branded as proprietary to users
MODEL = "claude-haiku-4-5-20251001"

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
    user_id: int,
    user_message: str,
    image_data: bytes | None = None,
    image_media_type: str | None = None,
) -> tuple[str, int, int]:
    """Generate a response from Claude API with tool use support.
    Returns (response_text, input_tokens, output_tokens).
    Optionally accepts image_data (bytes) for vision queries."""
    from app.tools import TOOL_DEFINITIONS, execute_tool, set_current_user

    set_current_user(user_id)

    system_prompt, messages = await assemble_context(user_id)

    # Build the user message content (text, or text + image)
    if image_data and image_media_type:
        import base64

        user_content = []
        user_content.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": image_media_type,
                "data": base64.b64encode(image_data).decode("utf-8"),
            },
        })
        user_content.append({
            "type": "text",
            "text": user_message or "What's in this image?",
        })
        messages.append({"role": "user", "content": user_content})
    else:
        messages.append({"role": "user", "content": user_message})

    # Ensure messages alternate properly (Claude requires this)
    messages = _fix_message_order(messages)

    total_input_tokens = 0
    total_output_tokens = 0

    # Tool use loop — Claude may call tools, we execute and feed results back
    max_tool_rounds = 3
    for _ in range(max_tool_rounds):
        response = await client.messages.create(
            model=MODEL,
            max_tokens=1024,
            system=system_prompt,
            messages=messages,
            tools=TOOL_DEFINITIONS,
        )

        total_input_tokens += response.usage.input_tokens
        total_output_tokens += response.usage.output_tokens

        # Check if Claude wants to use a tool
        if response.stop_reason == "tool_use":
            # Extract tool calls and text from the response
            tool_results = []
            assistant_content = []

            for block in response.content:
                if block.type == "tool_use":
                    assistant_content.append(block)
                    # Execute the tool
                    result = await execute_tool(block.name, block.input)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result,
                    })
                elif block.type == "text":
                    assistant_content.append(block)

            # Add assistant message with tool calls, then tool results
            messages.append({"role": "assistant", "content": assistant_content})
            messages.append({"role": "user", "content": tool_results})

            # Continue the loop — Claude will process tool results
            continue

        # No tool use — we have the final response
        break

    # Extract final text response
    response_text = ""
    for block in response.content:
        if hasattr(block, "text"):
            response_text += block.text

    return response_text, total_input_tokens, total_output_tokens


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
                model=MODEL,
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
            model=MODEL,
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
