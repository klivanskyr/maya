# Maya Telegram Assistant — Design Spec

## Overview

Maya is a general-purpose LLM assistant accessible via Telegram. Users message a Telegram bot and receive AI-powered responses from a locally-hosted open-source LLM (via Ollama). The system maintains persistent memory per user with intelligent context compaction and key-fact extraction.

## Architecture

**Async Monolith** — single FastAPI process handling:
- Telegram bot updates (long polling or webhook)
- Background LLM inference via Ollama
- Context management and compaction
- Token quota enforcement
- Admin dashboard (Jinja2 + HTMX)

## Messaging Flow

1. User sends a message to Maya's Telegram bot
2. Bot receives update via long polling (dev) or webhook (prod via Cloudflare Tunnel)
3. Bot sends "typing..." indicator (chat action)
4. Handler:
   a. Look up or create user by Telegram user ID
   b. Check token quota — if exceeded, reply with quota-exceeded message, stop
   c. Determine conversation: if last message >30 min ago, create new conversation; else attach to existing
   d. Store inbound message
   e. Assemble context: `[system prompt] + [key facts] + [rolling summary] + [recent messages]`
   f. Call Ollama for inference
   g. Store outbound message, track token usage
   h. Send response via Telegram Bot API
5. Typing indicator shows while Ollama generates — natural UX

## Context Management

### Context Assembly

Each request to Ollama includes (in order):
1. **System prompt** — Maya's persona (fixed cost)
2. **Key facts** — structured facts about the user (name, preferences, etc.)
3. **Rolling summary** — compressed history of older conversations
4. **Recent messages** — newest messages, loaded until context window is filled

### Compaction

**Trigger**: When total token count of raw messages exceeds 75% of the model's context window.

**Process**:
1. Identify oldest messages not in the "recent" window
2. Send to Ollama: "Summarize this conversation history, preserving key facts, user preferences, and important context"
3. Store resulting summary, replacing the previous one
4. Extract key facts (name, location, preferences, important dates) into structured `key_facts` table
5. Mark original messages as `compacted` (retained in DB for admin view, excluded from context assembly)

### Key Facts

Structured facts extracted during compaction and stored independently:
- Persist across all summaries — never lost to compaction
- Categories: name, location, preference, date, other
- Injected into every context window after the system prompt
- Updated/overwritten when new information supersedes old

## Data Model

### users
| Column | Type | Notes |
|--------|------|-------|
| id | INTEGER PK | Auto-increment |
| telegram_id | INTEGER UNIQUE | Telegram user ID, indexed |
| username | TEXT | Telegram username, nullable |
| first_name | TEXT | From Telegram profile |
| created_at | DATETIME | |
| tokens_used | INTEGER | Running total for current period |
| token_quota | INTEGER | Default: 50,000 tokens/day |
| quota_reset_at | DATETIME | Next reset time |
| active | BOOLEAN | Default: true |

### conversations
| Column | Type | Notes |
|--------|------|-------|
| id | INTEGER PK | |
| user_id | INTEGER FK | → users |
| started_at | DATETIME | |
| last_message_at | DATETIME | |
| message_count | INTEGER | |

New conversation created when gap between messages exceeds 30 minutes.

### messages
| Column | Type | Notes |
|--------|------|-------|
| id | INTEGER PK | |
| user_id | INTEGER FK | → users |
| conversation_id | INTEGER FK | → conversations |
| role | TEXT | "user" or "assistant" |
| content | TEXT | |
| token_count | INTEGER | |
| compacted | BOOLEAN | Default: false |
| created_at | DATETIME | |
| telegram_message_id | INTEGER | Telegram message ID, nullable |

### summaries
| Column | Type | Notes |
|--------|------|-------|
| id | INTEGER PK | |
| user_id | INTEGER FK | → users, unique |
| content | TEXT | Rolling summary |
| token_count | INTEGER | |
| last_compacted_message_id | INTEGER FK | → messages |
| updated_at | DATETIME | |

### key_facts
| Column | Type | Notes |
|--------|------|-------|
| id | INTEGER PK | |
| user_id | INTEGER FK | → users |
| category | TEXT | name, location, preference, date, other |
| key | TEXT | e.g., "birthday", "favorite_food" |
| value | TEXT | |
| updated_at | DATETIME | |

Unique constraint on (user_id, key).

## Token Quotas

- Each user gets a configurable `token_quota` (default: 50,000 tokens/day)
- `tokens_used` incremented by prompt + completion tokens after each inference
- When `quota_reset_at` has passed, reset `tokens_used` to 0, set next reset
- Quota check happens before inference — exceeded users get a friendly reply

## Maya Persona

System prompt defines Maya's personality. Stored in config, not in DB. Example:

> You are Maya, a helpful and friendly AI assistant. You communicate via text message, so keep your responses concise and conversational. You remember details about the people you talk to and use that context to be more helpful over time.

## Admin Dashboard

Jinja2 + HTMX, served from FastAPI. Localhost-only binding (no auth for v1).

### Pages
- **Overview**: Total users, messages today, active conversations, tokens used today
- **Users list**: Telegram username, message count, token usage, quota status, last active
- **User detail**: Conversation history, key facts, token usage, quota adjustment
- **Conversations**: Browse by user, view full message threads

## Project Structure

```
mayaai/
├── app/
│   ├── __init__.py
│   ├── main.py              # FastAPI app, lifespan, middleware
│   ├── config.py            # Settings via pydantic-settings
│   ├── models.py            # SQLAlchemy models
│   ├── database.py          # SQLite engine, session management
│   ├── telegram.py          # Telegram bot handler (python-telegram-bot)
│   ├── llm.py               # Ollama client, context assembly, compaction, key-fact extraction
│   ├── quota.py             # Token quota checking and tracking
│   ├── admin/
│   │   ├── __init__.py
│   │   ├── routes.py        # Admin dashboard routes
│   │   └── templates/       # Jinja2 + HTMX templates
│   │       ├── base.html
│   │       ├── overview.html
│   │       ├── users.html
│   │       ├── user_detail.html
│   │       └── conversations.html
│   └── static/              # CSS/JS for admin
├── tests/
├── pyproject.toml
├── .env.example
└── README.md
```

## Tech Stack

- **Runtime**: Python 3.12+
- **Framework**: FastAPI
- **Database**: SQLite via SQLAlchemy (async with aiosqlite)
- **LLM**: Ollama (local)
- **Messaging**: python-telegram-bot (async)
- **Admin UI**: Jinja2 + HTMX
- **Config**: pydantic-settings (.env file)
- **Tunnel**: Cloudflare Tunnel (for webhook mode in prod)

## Verification Plan

1. **Unit tests**: Test context assembly, compaction logic, quota enforcement
2. **Integration test**: Send a test message to the bot, verify response received
3. **Compaction test**: Send enough messages to trigger compaction, verify summary quality and key-fact extraction
4. **Quota test**: Exhaust quota, verify friendly rejection message
5. **Admin test**: Browse dashboard, verify user/conversation data displays correctly
6. **End-to-end**: Message the Telegram bot, have a multi-turn conversation, verify memory persistence and typing indicators
