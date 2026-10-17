"""aiogram 3 handlers for starting the tracker and requesting weekly reviews."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from aiogram import Bot, Dispatcher, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message, WebAppInfo
from sqlalchemy import select

from database import SessionLocal, User

if TYPE_CHECKING:
    from api import AuthIdentity

logger = logging.getLogger(__name__)
active_bot: Bot | None = None


def set_active_bot(bot: Bot | None) -> None:
    global active_bot
    active_bot = bot


async def send_telegram_message(chat_id: int, text: str) -> None:
    if active_bot is None:
        raise RuntimeError("Telegram bot is not currently connected.")
    await active_bot.send_message(chat_id=chat_id, text=text)


async def save_telegram_user(telegram_id: int, first_name: str) -> None:
    async with SessionLocal() as session:
        user = await session.scalar(select(User).where(User.telegram_id == telegram_id))
        if user is None:
            session.add(User(telegram_id=telegram_id, first_name=first_name or "Друг"))
        elif first_name:
            user.first_name = first_name[:80]
        await session.commit()


def create_dispatcher(webapp_url: str) -> Dispatcher:
    dispatcher = Dispatcher()
    router = Router(name="habit-tracker")

    @router.message(CommandStart())
    async def start_handler(message: Message) -> None:
        if message.from_user is None:
            return

        await save_telegram_user(message.from_user.id, message.from_user.first_name)
        if not webapp_url:
            await message.answer(
                "Приложение пока не настроено. Добавьте его публичный HTTPS-адрес, "
                "чтобы появилась кнопка «Открыть трекер»."
            )
            return

        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="Открыть трекер",
                                    web_app=WebAppInfo(url="https://telegram-habit-tracker--evgmust88.replit.app"),

                    )
                ]
            ]
        )
        await message.answer(
            f"Привет, {message.from_user.first_name}! Продолжим работать над привычками?",
            reply_markup=keyboard,
        )

    @router.message(Command("weekly"))
    async def weekly_handler(message: Message) -> None:
        if message.from_user is None:
            return
        try:
            from api import generate_weekly_report

            result = await generate_weekly_report(
                telegram_id=message.from_user.id,
                first_name=message.from_user.first_name,
                is_demo=False,
            )
            if not result["delivered"]:
                await message.answer(
                    "Обзор сохранён, но отправить его в Telegram не удалось. "
                    "Открой трекер, чтобы прочитать его там."
                )
        except ValueError as exc:
            await message.answer(str(exc))
        except Exception:
            logger.exception("Telegram weekly review failed for user %s", message.from_user.id)
            await message.answer(
                "Не удалось составить обзор. Попробуй ещё раз чуть позже."
            )

    dispatcher.include_router(router)
    return dispatcher
