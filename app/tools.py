"""Tools available to Maya via Claude's tool use."""

import logging
import re
from datetime import datetime, timedelta

import httpx
from ddgs import DDGS

logger = logging.getLogger(__name__)

# Tool definitions for Claude API
TOOL_DEFINITIONS = [
    {
        "name": "web_search",
        "description": (
            "Search the web for current information. Use this when the user asks about "
            "recent events, facts you're unsure about, prices, weather, news, or anything "
            "that benefits from live data. Always search rather than guessing when accuracy matters."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The search query. Be specific and concise.",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "read_url",
        "description": (
            "Read and extract the main text content from a URL/webpage. Use this when "
            "the user shares a link and wants you to read, summarize, or discuss it. "
            "Also use it when you need to read a specific webpage for more detail after a web search."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "The full URL to read (including https://).",
                },
            },
            "required": ["url"],
        },
    },
    {
        "name": "set_reminder",
        "description": (
            "Set a reminder for the user. Maya will send them a message at the specified time. "
            "Use this when the user says things like 'remind me to...', 'don't let me forget...', "
            "'alert me at...', or 'ping me about...'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "message": {
                    "type": "string",
                    "description": "The reminder message to send to the user.",
                },
                "minutes_from_now": {
                    "type": "integer",
                    "description": (
                        "How many minutes from now to send the reminder. "
                        "For example: 30 for half an hour, 60 for 1 hour, "
                        "1440 for tomorrow (24 hours), 10080 for a week."
                    ),
                },
            },
            "required": ["message", "minutes_from_now"],
        },
    },
]

# Store user_id in thread-local-like context for tool execution
_current_user_id: int | None = None


def set_current_user(user_id: int) -> None:
    global _current_user_id
    _current_user_id = user_id


async def execute_tool(name: str, input_data: dict) -> str:
    """Execute a tool by name and return the result as a string."""
    if name == "web_search":
        return await web_search(input_data.get("query", ""))
    elif name == "read_url":
        return await read_url(input_data.get("url", ""))
    elif name == "set_reminder":
        return await set_reminder(
            input_data.get("message", ""),
            input_data.get("minutes_from_now", 60),
        )
    return f"Unknown tool: {name}"


async def web_search(query: str, max_results: int = 5) -> str:
    """Search the web using DuckDuckGo and return formatted results."""
    try:
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=max_results))

        if not results:
            return "No results found."

        formatted = []
        for r in results:
            title = r.get("title", "")
            body = r.get("body", "")
            href = r.get("href", "")
            formatted.append(f"{title}\n{body}\nSource: {href}")

        return "\n\n".join(formatted)

    except Exception as e:
        logger.error(f"Web search failed for query '{query}': {e}")
        return f"Search failed: {str(e)}"


async def read_url(url: str) -> str:
    """Fetch a URL and extract readable text content."""
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=15.0,
            headers={"User-Agent": "Mozilla/5.0 (compatible; MayaBot/1.0)"},
        ) as client:
            response = await client.get(url)
            response.raise_for_status()

        content_type = response.headers.get("content-type", "")
        if "text/html" not in content_type and "text/plain" not in content_type:
            return f"Cannot read this content type: {content_type}"

        html = response.text

        # Strip HTML tags to get plain text
        text = _html_to_text(html)

        # Truncate to avoid overwhelming the context
        if len(text) > 4000:
            text = text[:4000] + "\n\n[Content truncated — page is very long]"

        if not text.strip():
            return "Could not extract readable text from this page."

        return f"Content from {url}:\n\n{text}"

    except httpx.TimeoutException:
        return f"Timed out trying to read {url}"
    except httpx.HTTPStatusError as e:
        return f"HTTP error {e.response.status_code} reading {url}"
    except Exception as e:
        logger.error(f"Failed to read URL '{url}': {e}")
        return f"Failed to read the page: {str(e)}"


def _html_to_text(html: str) -> str:
    """Simple HTML to text conversion."""
    # Remove script and style blocks
    html = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(r"<style[^>]*>.*?</style>", "", html, flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(r"<nav[^>]*>.*?</nav>", "", html, flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(r"<footer[^>]*>.*?</footer>", "", html, flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(r"<header[^>]*>.*?</header>", "", html, flags=re.DOTALL | re.IGNORECASE)

    # Convert common block elements to newlines
    html = re.sub(r"<br\s*/?>", "\n", html, flags=re.IGNORECASE)
    html = re.sub(r"</(p|div|h[1-6]|li|tr|blockquote)>", "\n", html, flags=re.IGNORECASE)

    # Remove all remaining tags
    text = re.sub(r"<[^>]+>", " ", html)

    # Decode common HTML entities
    text = text.replace("&amp;", "&")
    text = text.replace("&lt;", "<")
    text = text.replace("&gt;", ">")
    text = text.replace("&quot;", '"')
    text = text.replace("&#39;", "'")
    text = text.replace("&nbsp;", " ")

    # Clean up whitespace
    text = re.sub(r" +", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    text = text.strip()

    return text


async def set_reminder(message: str, minutes_from_now: int) -> str:
    """Create a reminder in the database."""
    from app.database import async_session
    from app.models import Reminder

    if _current_user_id is None:
        return "Error: could not identify the user for this reminder."

    if minutes_from_now < 1:
        return "Reminder must be at least 1 minute from now."

    remind_at = datetime.utcnow() + timedelta(minutes=minutes_from_now)

    async with async_session() as session:
        reminder = Reminder(
            user_id=_current_user_id,
            message=message,
            remind_at=remind_at,
        )
        session.add(reminder)
        await session.commit()

    # Format the time nicely
    if minutes_from_now < 60:
        time_str = f"{minutes_from_now} minute{'s' if minutes_from_now != 1 else ''}"
    elif minutes_from_now < 1440:
        hours = minutes_from_now / 60
        time_str = f"{hours:.0f} hour{'s' if hours != 1 else ''}"
    else:
        days = minutes_from_now / 1440
        time_str = f"{days:.0f} day{'s' if days != 1 else ''}"

    return f"Reminder set! I'll message you in {time_str} about: {message}"
