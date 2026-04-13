"""Tools available to Maya via Claude's tool use."""

import logging

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
]


async def execute_tool(name: str, input_data: dict) -> str:
    """Execute a tool by name and return the result as a string."""
    if name == "web_search":
        return await web_search(input_data.get("query", ""))
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
            formatted.append(f"**{title}**\n{body}\nSource: {href}")

        return "\n\n".join(formatted)

    except Exception as e:
        logger.error(f"Web search failed for query '{query}': {e}")
        return f"Search failed: {str(e)}"
