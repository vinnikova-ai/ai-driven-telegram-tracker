"""Run the FastAPI web app and Telegram bot in one asyncio process."""

from __future__ import annotations

import asyncio
import logging
import os
from urllib.parse import urlparse

import uvicorn
from aiogram import Bot
from dotenv import load_dotenv

load_dotenv()

from api import app  # noqa: E402
from bot import create_dispatcher, set_active_bot  # noqa: E402
from database import init_db  # noqa: E402

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("habit-tracker")


def configured_webapp_url() -> str:
    """Resolve a public HTTPS URL for Telegram's Mini App button."""
    value = os.getenv("TELEGRAM_WEBAPP_URL", "").strip()
    if not value:
        domain = os.getenv("REPLIT_DEV_DOMAIN", "").strip()
        if not domain:
            domain = os.getenv("REPLIT_DOMAINS", "").split(",")[0].strip()
        if domain:
            value = domain if domain.startswith(("https://", "http://")) else f"https://{domain}"

    if not value:
        return ""

    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.netloc:
        logger.warning("TELEGRAM_WEBAPP_URL must be a complete HTTPS URL; Web App button disabled.")
        return ""
    return value.rstrip("/")


async def run() -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is required to start the Telegram bot.")

    await init_db()
    port = int(os.getenv("PORT", "8000"))
    webapp_url = configured_webapp_url()

    async with Bot(token=token) as telegram_bot:
        set_active_bot(telegram_bot)
        dispatcher = create_dispatcher(webapp_url)
        server = uvicorn.Server(
            uvicorn.Config(
                app,
                host="0.0.0.0",
                port=port,
                log_level=os.getenv("LOG_LEVEL", "info").lower(),
                proxy_headers=True,
                forwarded_allow_ips="*",
            )
        )

        logger.info("Starting FastAPI and Telegram polling on port %s", port)
        if not webapp_url:
            logger.warning(
                "Set TELEGRAM_WEBAPP_URL to your public HTTPS URL to enable the bot's Web App button."
            )

        server_task = asyncio.create_task(server.serve(), name="fastapi")
        bot_task = asyncio.create_task(
            dispatcher.start_polling(
                telegram_bot,
                handle_signals=False,
                allowed_updates=dispatcher.resolve_used_update_types(),
            ),
            name="telegram-bot",
        )
        try:
            done, pending = await asyncio.wait(
                {server_task, bot_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            for task in done:
                task.result()
        finally:
            set_active_bot(None)
            if not server_task.done():
                server.should_exit = True
                server_task.cancel()
            if not bot_task.done():
                bot_task.cancel()
            await asyncio.gather(server_task, bot_task, return_exceptions=True)


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        logger.info("Habit tracker stopped.")
