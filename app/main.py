import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from telegram import Bot

from app.admin.routes import router as admin_router
from app.config import settings
from app.database import async_session, engine
from app.models import Base, Reminder, User
from app.stripe_billing import create_checkout_session, handle_stripe_webhook, verify_checkout_token
from app.telegram import create_bot_app, run_bot_polling, stop_bot_polling

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

templates = Jinja2Templates(directory="app/templates")

bot_app = None
_reminder_task = None


async def _reminder_loop():
    """Background loop that checks for due reminders and sends them."""
    bot = Bot(token=settings.telegram_bot_token)
    while True:
        try:
            now = datetime.utcnow()
            async with async_session() as session:
                result = await session.execute(
                    select(Reminder).where(
                        Reminder.sent == False,  # noqa: E712
                        Reminder.remind_at <= now,
                    )
                )
                due_reminders = result.scalars().all()

                for reminder in due_reminders:
                    user = await session.get(User, reminder.user_id)
                    if user:
                        try:
                            await bot.send_message(
                                chat_id=user.telegram_id,
                                text=f"Reminder: {reminder.message}",
                            )
                        except Exception as e:
                            logger.error(f"Failed to send reminder {reminder.id}: {e}")

                    r = await session.get(Reminder, reminder.id)
                    r.sent = True

                if due_reminders:
                    await session.commit()

        except Exception as e:
            logger.error(f"Reminder loop error: {e}")

        await asyncio.sleep(30)  # Check every 30 seconds


@asynccontextmanager
async def lifespan(app: FastAPI):
    global bot_app, _reminder_task

    # Create tables
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # Start Telegram bot (polling for dev, webhook for prod)
    bot_app = create_bot_app()

    if settings.environment == "production":
        await bot_app.initialize()
        await bot_app.start()
        webhook_url = f"{settings.app_url}/webhook/telegram"
        await bot_app.bot.set_webhook(webhook_url)
        logger.info(f"Telegram webhook set to {webhook_url}")
    else:
        await run_bot_polling(bot_app)

    # Start reminder background loop
    _reminder_task = asyncio.create_task(_reminder_loop())
    logger.info("Reminder loop started")

    yield

    # Shutdown
    if _reminder_task:
        _reminder_task.cancel()
    if settings.environment == "production":
        await bot_app.stop()
        await bot_app.shutdown()
    else:
        await stop_bot_polling(bot_app)


app = FastAPI(title="Maya", version="1.0.0", lifespan=lifespan)
app.include_router(admin_router)
app.mount("/static", StaticFiles(directory="app/static"), name="static")


# ─── Health check ────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok"}


# ─── Telegram webhook (production) ──────────────────────────────────

@app.post("/webhook/telegram")
async def telegram_webhook(request: Request):
    from telegram import Update

    data = await request.json()
    update = Update.de_json(data, bot_app.bot)
    await bot_app.process_update(update)
    return JSONResponse({"ok": True})


# ─── Stripe endpoints ───────────────────────────────────────────────

@app.get("/checkout/{token}")
async def checkout(token: str):
    result = verify_checkout_token(token)
    if result is None:
        return JSONResponse({"error": "Invalid or expired checkout link"}, status_code=400)
    user_id, tier = result
    url = await create_checkout_session(user_id, tier)
    if url:
        return RedirectResponse(url=url, status_code=303)
    return JSONResponse({"error": "Unable to create checkout session"}, status_code=500)


@app.post("/webhook/stripe")
async def stripe_webhook(request: Request):
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature", "")
    success = await handle_stripe_webhook(payload, sig_header)
    if success:
        return JSONResponse({"received": True})
    return JSONResponse({"error": "Webhook verification failed"}, status_code=400)


# ─── Landing pages ──────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def landing_page(request: Request, upgraded: bool = False):
    return templates.TemplateResponse(
        request, "landing.html", context={"upgraded": upgraded}
    )


@app.get("/pricing", response_class=HTMLResponse)
async def pricing_page(request: Request):
    return templates.TemplateResponse(request, "pricing.html")


@app.get("/privacy", response_class=HTMLResponse)
async def privacy_page(request: Request):
    return templates.TemplateResponse(request, "privacy.html")


@app.get("/terms", response_class=HTMLResponse)
async def terms_page(request: Request):
    return templates.TemplateResponse(request, "terms.html")


@app.get("/consent", response_class=HTMLResponse)
async def consent_page(request: Request):
    return templates.TemplateResponse(request, "consent.html")
