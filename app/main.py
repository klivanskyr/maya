import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.admin.routes import router as admin_router
from app.config import settings
from app.database import engine
from app.models import Base
from app.stripe_billing import create_checkout_session, handle_stripe_webhook, verify_checkout_token
from app.telegram import create_bot_app, run_bot_polling, stop_bot_polling

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

templates = Jinja2Templates(directory="app/templates")

bot_app = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global bot_app

    # Create tables
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # Start Telegram bot (polling for dev, webhook for prod)
    bot_app = create_bot_app()

    if settings.environment == "production":
        # In production, use webhooks — set up in deploy step
        await bot_app.initialize()
        await bot_app.start()
        webhook_url = f"{settings.app_url}/webhook/telegram"
        await bot_app.bot.set_webhook(webhook_url)
        logger.info(f"Telegram webhook set to {webhook_url}")
    else:
        # In development, use polling
        await run_bot_polling(bot_app)

    yield

    # Shutdown
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
    user_id = verify_checkout_token(token)
    if user_id is None:
        return JSONResponse({"error": "Invalid or expired checkout link"}, status_code=400)
    url = await create_checkout_session(user_id)
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
