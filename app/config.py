from pydantic_settings import BaseSettings


# Tier configuration
TIERS = {
    "free": {
        "label": "Free",
        "daily_messages": 15,
        "max_facts": 25,
        "models": ["haiku"],
        "price": 0,
    },
    "pro": {
        "label": "Pro",
        "daily_messages": 50,
        "max_facts": 100,
        "models": ["haiku"],
        "price": 10,
    },
    "elite": {
        "label": "Elite",
        "daily_messages": 100,
        "max_facts": 999999,  # unlimited
        "models": ["haiku", "sonnet"],
        "price": 20,
    },
}

# Overage rate after daily limit (per message)
OVERAGE_RATE_CENTS = 5  # $0.05 per message over limit


class Settings(BaseSettings):
    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}

    # Telegram
    telegram_bot_token: str

    # Anthropic Claude API
    anthropic_api_key: str = ""

    # Stripe
    stripe_secret_key: str = ""
    stripe_publishable_key: str = ""
    stripe_webhook_secret: str = ""
    stripe_price_id_pro: str = ""
    stripe_price_id_elite: str = ""

    # Database
    database_url: str = "sqlite+aiosqlite:///./maya.db"

    # Maya persona
    maya_system_prompt: str = (
        "You are Maya, a friendly AI assistant that people text on Telegram. "
        "You talk like a smart friend — concise, warm, casual. Use contractions. "
        "No corporate speak. No \"Great question!\" or \"I'd be happy to help!\" openers. "
        "You remember details about the person you're talking to and use that context naturally. "
        "Keep responses short — this is texting, not email. "
        "NEVER use markdown formatting — no bold, no italics, no headers, no bullet points with asterisks. "
        "Just plain text like a normal text message. Use dashes (-) for lists if needed. "
        "You have access to these tools - use them proactively: "
        "1) web_search - search the web for current info. Always search rather than guessing when accuracy matters. "
        "2) read_url - read and extract text from any webpage/link the user shares. "
        "3) set_reminder - set a timed reminder that you will send to the user later. "
        "You can also see and understand images/photos that users send you. "
        "The user can use these commands: "
        "/memory - see what you remember about them, "
        "/forget [fact] - delete a specific memory, "
        "/plan - view their current plan and usage, "
        "/upgrade - see upgrade options (Pro $10/mo, Elite $20/mo), "
        "/stats - see their usage statistics, "
        "/export - export chat history (Pro and Elite), "
        "/settings - change AI model (Elite only), "
        "/help - see all commands. "
        "Plans: Free (15 msgs/day, Haiku), Pro $10/mo (50 msgs/day, Haiku), "
        "Elite $20/mo (100 msgs/day, Haiku + Sonnet, unlimited memory). "
        "After hitting the daily limit, users can keep messaging at $0.05/message. "
        "If someone asks what you can do or how to use you, mention the relevant commands and capabilities naturally."
    )

    # Context
    max_context_tokens: int = 8192
    compaction_threshold: float = 0.75
    conversation_timeout_minutes: int = 30

    # Admin
    admin_api_key: str = "maya-admin-secret-change-me"

    # App
    app_url: str = "http://localhost:8000"
    environment: str = "development"


settings = Settings()
