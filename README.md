# Maya

The AI that actually knows you. Just text her on Telegram.

Maya is a consumer AI assistant you text like a friend. She remembers your name, your preferences, your projects — and uses that knowledge to give better answers every time. Powered by Claude.

## Features

- **Just text** — message Maya on Telegram, no app to download
- **Persistent memory** — automatically remembers key facts about you
- **Context compaction** — summarizes old conversations to stay sharp
- **Smart, not robotic** — conversational tone, concise responses
- **Simple pricing** — Free (15 msgs/day) or Plus ($9/mo, unlimited)
- **Privacy first** — view, delete, and export your data anytime

## Quick Start

1. **Install dependencies:**
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -e .
   ```

2. **Configure:**
   ```bash
   cp .env.example .env
   # Set TELEGRAM_BOT_TOKEN, ANTHROPIC_API_KEY, STRIPE_SECRET_KEY
   ```

3. **Run:**
   ```bash
   uvicorn app.main:app --host 0.0.0.0 --port 8000
   ```

4. **Message your bot** on Telegram and start chatting!

## Bot Commands

| Command | Description |
|---------|-------------|
| `/start` | Begin chatting with Maya |
| `/memory` | See what Maya remembers about you |
| `/forget [fact]` | Delete a memory |
| `/plan` | View your plan and usage |
| `/upgrade` | Upgrade to Maya Plus |
| `/stats` | Usage statistics |
| `/help` | List all commands |
| `/export` | Export chat history (Plus) |
| `/settings` | Adjust preferences (Plus) |

## Development

```bash
pip install -e ".[dev]"
pytest
```

Admin dashboard: `http://localhost:8000/admin/`
Landing page: `http://localhost:8000/`
