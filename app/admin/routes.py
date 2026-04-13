from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.security import APIKeyHeader
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select

from app.config import settings
from app.database import async_session
from app.models import Conversation, KeyFact, Message, Summary, User

api_key_header = APIKeyHeader(name="X-Admin-Key", auto_error=False)


async def verify_admin(
    request: Request,
    api_key: str | None = Depends(api_key_header),
):
    """Check admin API key from header or query param."""
    key = api_key or request.query_params.get("key")
    if key != settings.admin_api_key:
        raise HTTPException(status_code=403, detail="Invalid admin key")


router = APIRouter(prefix="/admin", dependencies=[Depends(verify_admin)])
templates = Jinja2Templates(directory="app/admin/templates")


@router.get("/", response_class=HTMLResponse)
async def overview(request: Request):
    today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)

    async with async_session() as session:
        total_users = (await session.execute(select(func.count(User.id)))).scalar() or 0

        messages_today = (
            await session.execute(
                select(func.count(Message.id)).where(Message.created_at >= today_start)
            )
        ).scalar() or 0

        # Active conversations in last 30 min
        cutoff = datetime.utcnow() - timedelta(minutes=30)
        active_conversations = (
            await session.execute(
                select(func.count(Conversation.id)).where(
                    Conversation.last_message_at >= cutoff
                )
            )
        ).scalar() or 0

        tokens_today = (
            await session.execute(
                select(func.sum(Message.token_count)).where(
                    Message.created_at >= today_start
                )
            )
        ).scalar() or 0

        # Recent messages with user info
        result = await session.execute(
            select(Message, User.username, User.telegram_id)
            .join(User, Message.user_id == User.id)
            .order_by(Message.created_at.desc())
            .limit(20)
        )
        recent_messages = []
        for msg, username, telegram_id in result.all():
            msg.username = username
            msg.telegram_id = telegram_id
            recent_messages.append(msg)

    return templates.TemplateResponse(
        request,
        "overview.html",
        context={
            "total_users": total_users,
            "messages_today": messages_today,
            "active_conversations": active_conversations,
            "tokens_today": tokens_today,
            "recent_messages": recent_messages,
        },
    )


@router.get("/users", response_class=HTMLResponse)
async def users_list(request: Request):
    async with async_session() as session:
        result = await session.execute(
            select(
                User,
                func.count(Message.id).label("message_count"),
                func.max(Message.created_at).label("last_active"),
            )
            .outerjoin(Message, User.id == Message.user_id)
            .group_by(User.id)
            .order_by(func.max(Message.created_at).desc())
        )
        users = []
        for user, message_count, last_active in result.all():
            user.message_count = message_count
            user.last_active = last_active
            user.quota_pct = 0  # Legacy field, no longer used
            users.append(user)

    return templates.TemplateResponse(
        request, "users.html", context={"users": users}
    )


@router.get("/users/{user_id}", response_class=HTMLResponse)
async def user_detail(request: Request, user_id: int):
    async with async_session() as session:
        user = await session.get(User, user_id)
        if not user:
            return HTMLResponse("User not found", status_code=404)

        # Key facts
        result = await session.execute(
            select(KeyFact).where(KeyFact.user_id == user_id)
        )
        key_facts = result.scalars().all()

        # Summary
        result = await session.execute(
            select(Summary).where(Summary.user_id == user_id)
        )
        summary = result.scalar_one_or_none()

        # Conversations
        result = await session.execute(
            select(Conversation)
            .where(Conversation.user_id == user_id)
            .order_by(Conversation.started_at.desc())
        )
        conversations = result.scalars().all()

    return templates.TemplateResponse(
        request,
        "user_detail.html",
        context={
            "user": user,
            "key_facts": key_facts,
            "summary": summary,
            "conversations": conversations,
        },
    )


@router.get("/conversations/{conv_id}", response_class=HTMLResponse)
async def conversation_detail(request: Request, conv_id: int):
    async with async_session() as session:
        conversation = await session.get(Conversation, conv_id)
        if not conversation:
            return HTMLResponse("Conversation not found", status_code=404)

        user = await session.get(User, conversation.user_id)

        result = await session.execute(
            select(Message)
            .where(Message.conversation_id == conv_id)
            .order_by(Message.created_at.asc())
        )
        messages = result.scalars().all()

    return templates.TemplateResponse(
        request,
        "conversations.html",
        context={
            "conversation": conversation,
            "user": user,
            "messages": messages,
        },
    )
