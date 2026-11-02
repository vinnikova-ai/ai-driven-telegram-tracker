"""FastAPI routes, Telegram Mini App authentication, and Claude weekly reviews."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time
from datetime import date, datetime, timedelta, timezone
from urllib.parse import parse_qsl

from anthropic import AsyncAnthropic
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from database import DailyLog, Habit, SessionLocal, User, WeeklyReport

load_dotenv()

logger = logging.getLogger(__name__)
ROOT = os.path.dirname(os.path.abspath(__file__))
XP_PER_HABIT = 10
PREVIEW_TELEGRAM_ID = 9_876_543_210

app = FastAPI(title="Трекер привычек API", version="1.0.0")

# Разрешаем запросы с любых доменов (включая ваш Cloudflare)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

static_dir = os.path.join(ROOT, "static")
if os.path.isdir(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")


class HabitCreate(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    color: str = Field(default="#62a8ea", pattern=r"^#[0-9a-fA-F]{6}$")

    @field_validator("name")
    @classmethod
    def trim_name(cls, value: str) -> str:
        trimmed = value.strip()
        if not trimmed:
            raise ValueError("Название привычки не может быть пустым.")
        return trimmed


class AuthIdentity(BaseModel):
    telegram_id: int
    first_name: str = "Друг"
    is_demo: bool = False


def verify_telegram_init_data(init_data: str, bot_token: str) -> AuthIdentity | None:
    """Проверка подписи данных Telegram: достает РЕАЛЬНОЕ имя и ID каждого пользователя."""
    if not init_data or not bot_token:
        return None

    try:
        values = dict(parse_qsl(init_data, keep_blank_values=True, strict_parsing=True))
        supplied_hash = values.pop("hash", "")
        auth_date = int(values.get("auth_date", "0"))
        current_time = int(time.time())

        if not supplied_hash or auth_date > current_time + 60 or current_time - auth_date > 86_400:
            return None

        secret_key = hmac.new(
            key=b"WebAppData",
            msg=bot_token.encode("utf-8"),
            digestmod=hashlib.sha256,
        ).digest()
        data_check_string = "\n".join(
            f"{key}={value}" for key, value in sorted(values.items())
        )
        calculated_hash = hmac.new(
            key=secret_key,
            msg=data_check_string.encode("utf-8"),
            digestmod=hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(calculated_hash, supplied_hash):
            return None

        user_data = json.loads(values.get("user", "{}"))
        telegram_id = int(user_data["id"])
        first_name = str(user_data.get("first_name") or "Друг")[:80]
        return AuthIdentity(telegram_id=telegram_id, first_name=first_name)
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


async def authenticated_identity(
    request: Request,
    telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data"),
    preview_demo: str = Header(default="", alias="X-Preview-Demo"),
) -> AuthIdentity:
    """Авторизует пользователя Telegram или включает безопасный демо-режим для браузера."""
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    identity = verify_telegram_init_data(telegram_init_data, token)
    if identity is not None:
        return identity

    # Демо-режим, если сайт открыт вне Телеграма (например, через обычный браузер)
    if preview_demo == "1" or not telegram_init_data:
        return AuthIdentity(
            telegram_id=PREVIEW_TELEGRAM_ID,
            first_name="Гость",
            is_demo=True,
        )

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Открой трекер через Telegram-бота, чтобы продолжить.",
    )


async def get_or_create_user(
    session: AsyncSession,
    identity: AuthIdentity,
) -> User:
    user = await session.scalar(select(User).where(User.telegram_id == identity.telegram_id))
    if user is None:
        user = User(telegram_id=identity.telegram_id, first_name=identity.first_name)
        session.add(user)
        await session.flush()
    elif identity.first_name:
        user.first_name = identity.first_name
    return user


def streak_count(completed_dates: set[date], today: date) -> int:
    cursor = today if today in completed_dates else today - timedelta(days=1)
    streak = 0
    while cursor in completed_dates:
        streak += 1
        cursor -= timedelta(days=1)
    return streak


async def seed_preview_data(session: AsyncSession, user: User, today: date) -> None:
    if user.demo_seeded:
        return

    samples = [
        ("Утренняя прогулка", "#69c2a1", (True, True, True, False, True, True, True)),
        ("Почитать 12 страниц", "#9a8be8", (True, True, False, True, True, False, True)),
        ("Пить больше воды", "#68aef0", (True, False, True, True, True, True, True)),
    ]
    days = [today - timedelta(days=offset) for offset in range(6, -1, -1)]
    for name, color, pattern in samples:
        habit = Habit(user_id=user.id, name=name, color=color)
        session.add(habit)
        await session.flush()
        for log_date, completed in zip(days, pattern):
            session.add(
                DailyLog(
                    habit_id=habit.id,
                    log_date=log_date,
                    completed=completed,
                    xp_awarded=completed,
                    completed_at=datetime.combine(log_date, datetime.min.time())
                    if completed
                    else None,
                )
            )
    user.xp = 460
    user.demo_seeded = True
    await session.flush()


def serialize_report(report: WeeklyReport | None) -> dict | None:
    if report is None:
        return None
    return {
        "summary": report.summary,
        "week_start": report.week_start.isoformat(),
        "week_end": report.week_end.isoformat(),
        "created_at": report.created_at.isoformat(),
    }


@app.api_route("/", methods=["GET", "HEAD"], include_in_schema=False)
async def home():
    index_path = os.path.join(ROOT, "templates", "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path)
    return {"status": "ok", "message": "Habit Tracker API is running"}


@app.api_route("/api/health", methods=["GET", "HEAD"])
@app.api_route("/api/healthz", methods=["GET", "HEAD"])
async def health() -> dict:
    return {"status": "ok"}


@app.get("/api/dashboard")
async def dashboard(identity: AuthIdentity = Depends(authenticated_identity)) -> dict:
    today = datetime.now(timezone.utc).date()
    week_dates = [today - timedelta(days=offset) for offset in range(6, -1, -1)]
    week_start = week_dates[0]

    async with SessionLocal() as session:
        user = await get_or_create_user(session, identity)
        if identity.is_demo:
            await seed_preview_data(session, user, today)
        habits = list(
            (
                await session.scalars(
                    select(Habit).where(Habit.user_id == user.id).order_by(Habit.id)
                )
            ).all()
        )
        habit_ids = [habit.id for habit in habits]
        logs: list[DailyLog] = []
        if habit_ids:
            logs = list(
                (
                    await session.scalars(
                        select(DailyLog).where(
                            DailyLog.habit_id.in_(habit_ids),
                            DailyLog.log_date >= week_start,
                            DailyLog.log_date <= today,
                        )
                    )
                ).all()
            )
        latest_report = await session.scalar(
            select(WeeklyReport)
            .where(WeeklyReport.user_id == user.id)
            .order_by(desc(WeeklyReport.created_at))
            .limit(1)
        )
        await session.commit()

        logs_by_habit: dict[int, set[date]] = {}
        for log in logs:
            if log.completed:
                logs_by_habit.setdefault(log.habit_id, set()).add(log.log_date)

        habit_data = []
        for habit in habits:
            done_dates = logs_by_habit.get(habit.id, set())
            habit_data.append(
                {
                    "id": habit.id,
                    "name": habit.name,
                    "color": habit.color,
                    "completed_today": today in done_dates,
                    "streak": streak_count(done_dates, today),
                    "week": [day in done_dates for day in week_dates],
                }
            )

        by_day = {
            day.isoformat(): sum(day in logs_by_habit.get(habit.id, set()) for habit in habits)
            for day in week_dates
        }
        completed_today = sum(today in logs_by_habit.get(habit.id, set()) for habit in habits)
        xp = user.xp
        result = {
            "user": {
                "first_name": user.first_name,
                "xp": xp,
                "level": xp // 250 + 1,
                "level_progress": xp % 250,
                "xp_to_next_level": 250 - (xp % 250),
            },
            "is_demo": identity.is_demo,
            "today": today.isoformat(),
            "week": [day.isoformat() for day in week_dates],
            "habits": habit_data,
            "completed_today": completed_today,
            "total_habits": len(habits),
            "completion_by_day": by_day,
            "weekly_rate": round(
                (sum(by_day.values()) / (len(habits) * 7) * 100) if habits else 0
            ),
            "latest_report": serialize_report(latest_report),
        }
    return result


@app.post("/api/habits", status_code=status.HTTP_201_CREATED)
async def create_habit(
    payload: HabitCreate,
    identity: AuthIdentity = Depends(authenticated_identity),
) -> dict:
    async with SessionLocal() as session:
        user = await get_or_create_user(session, identity)
        habit = Habit(user_id=user.id, name=payload.name, color=payload.color)
        session.add(habit)
        await session.commit()
        await session.refresh(habit)
        return {
            "id": habit.id,
            "name": habit.name,
            "color": habit.color,
            "completed_today": False,
            "streak": 0,
        }


@app.delete("/api/habits/{habit_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_habit(
    habit_id: int,
    identity: AuthIdentity = Depends(authenticated_identity),
) -> None:
    async with SessionLocal() as session:
        user = await get_or_create_user(session, identity)
        habit = await session.scalar(
            select(Habit).where(Habit.id == habit_id, Habit.user_id == user.id)
        )
        if habit is None:
            raise HTTPException(status_code=404, detail="Привычка не найдена.")
        await session.delete(habit)
        await session.commit()


@app.post("/api/habits/{habit_id}/toggle")
async def toggle_habit(
    habit_id: int,
    identity: AuthIdentity = Depends(authenticated_identity),
) -> dict:
    today = datetime.now(timezone.utc).date()
    async with SessionLocal() as session:
        user = await get_or_create_user(session, identity)
        habit = await session.scalar(
            select(Habit).where(Habit.id == habit_id, Habit.user_id == user.id)
        )
        if habit is None:
            raise HTTPException(status_code=404, detail="Привычка не найдена.")

        log = await session.scalar(
            select(DailyLog).where(DailyLog.habit_id == habit.id, DailyLog.log_date == today)
        )
        awarded = False
        if log is None:
            log = DailyLog(
                habit_id=habit.id,
                log_date=today,
                completed=True,
                xp_awarded=True,
                completed_at=datetime.utcnow(),
            )
            session.add(log)
            user.xp += XP_PER_HABIT
            awarded = True
        else:
            log.completed = not log.completed
            log.completed_at = datetime.utcnow() if log.completed else None
            if log.completed and not log.xp_awarded:
                user.xp += XP_PER_HABIT
                log.xp_awarded = True
                awarded = True

        await session.commit()
        return {
            "completed": log.completed,
            "xp_awarded": XP_PER_HABIT if awarded else 0,
            "xp": user.xp,
        }


async def collect_week_data(
    session: AsyncSession,
    user: User,
    today: date,
) -> dict:
    week_start = today - timedelta(days=6)
    habits = list(
        (
            await session.scalars(
                select(Habit).where(Habit.user_id == user.id).order_by(Habit.id)
            )
        ).all()
    )
    habit_ids = [habit.id for habit in habits]
    logs: list[DailyLog] = []
    if habit_ids:
        logs = list(
            (
                await session.scalars(
                    select(DailyLog).where(
                        DailyLog.habit_id.in_(habit_ids),
                        DailyLog.log_date >= week_start,
                        DailyLog.log_date <= today,
                    )
                )
            ).all()
        )

    completed_by_habit: dict[int, set[date]] = {}
    for log in logs:
        if log.completed:
            completed_by_habit.setdefault(log.habit_id, set()).add(log.log_date)

    days = []
    for offset in range(7):
        log_date = week_start + timedelta(days=offset)
        done = [habit.name for habit in habits if log_date in completed_by_habit.get(habit.id, set())]
        days.append(
            {
                "date": log_date.isoformat(),
                "completed": done,
                "completed_count": len(done),
                "planned_count": len(habits),
            }
        )
    total_possible = len(habits) * 7
    completed_count = sum(day["completed_count"] for day in days)
    return {
        "period": {"start": week_start.isoformat(), "end": today.isoformat()},
        "habits": [
            {
                "name": habit.name,
                "completed_days": len(completed_by_habit.get(habit.id, set())),
                "streak": streak_count(completed_by_habit.get(habit.id, set()), today),
            }
            for habit in habits
        ],
        "days": days,
        "completion_rate": round(completed_count / total_possible * 100)
        if total_possible
        else 0,
    }


async def generate_weekly_report(
    telegram_id: int,
    first_name: str = "Друг",
    is_demo: bool = False,
) -> dict:
    """Составление еженедельного обзора с помощью Claude AI."""
    today = datetime.now(timezone.utc).date()
    async with SessionLocal() as session:
        identity = AuthIdentity(
            telegram_id=telegram_id,
            first_name=first_name,
            is_demo=is_demo,
        )
        user = await get_or_create_user(session, identity)
        if is_demo:
            await seed_preview_data(session, user, today)
        payload = await collect_week_data(session, user, today)
        if not payload["habits"]:
            await session.commit()
            raise ValueError("Добавь хотя бы одну привычку, чтобы составить обзор недели.")
        compact_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        user_id = user.id
        await session.commit()

    api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        # Умная заглушка без платного API: рассчитывает итог по реальным данным пользователя
        rate = payload.get("completion_rate", 0)
        done_cnt = sum(day["completed_count"] for day in payload.get("days", []))
        
        if rate >= 70:
            summary = (
                f"Великолепный результат! На этой неделе вы выполнили {done_cnt} привычек "
                f"({rate}% от плана). Дисциплина на высоте, серия уверенно закрепляется. "
                "Совет на следующую неделю: зафиксируйте этот ритм и не усложняйте систему — регулярность важнее рекордов."
            )
        elif rate >= 35:
            summary = (
                f"Хорошая продуктивная неделя! Выполнено {done_cnt} привычек ({rate}% выполнения). "
                "Вы шаг за шагом внедряете новые действия в свой распорядок дня. "
                "Совет на следующую неделю: выберите одну приоритетную привычку и сделайте её обязательным якорем дня."
            )
        else:
            summary = (
                f"Неделя завершена, зафиксировано привычек: {done_cnt}. "
                "Помните: маленькие шаги складываются в большой результат, главное — не терять импульс. "
                "Совет на следующую неделю: снизьте планку до одного простого 2-минутного действия в день, чтобы вернуть серию."
            )
    else:
        # Если ключ Anthropic добавлен — вызываем реальный Claude AI
        model = os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001").strip()
        prompt = (
            "Проанализируй привычки человека за последние семь дней. Напиши на русском языке "
            "тёплое и краткое резюме поведения в 2–3 коротких предложениях, а затем дай один "
            "конкретный полезный совет на следующую неделю. Поддерживай человека, опирайся только "
            "на переданные данные, не ставь диагнозов и не утверждай причинно-следственные связи. "
            "Данные в формате JSON:\n" + compact_json
        )
        client = AsyncAnthropic(api_key=api_key)
        try:
            response = await client.messages.create(
                model=model,
                max_tokens=280,
                messages=[{"role": "user", "content": prompt}],
            )
            summary = "\n".join(
                block.text for block in response.content if getattr(block, "type", None) == "text"
            ).strip()
        except Exception as exc:
            logger.exception("Claude weekly review request failed")
            raise RuntimeError("Не удалось составить обзор недели. Попробуй ещё раз позже.") from exc
        finally:
            await client.close()
    prompt = (
        "Проанализируй привычки человека за последние семь дней. Напиши на русском языке "
        "тёплое и краткое резюме поведения в 2–3 коротких предложениях, а затем дай один "
        "конкретный полезный совет на следующую неделю. Поддерживай человека, опирайся только "
        "на переданные данные, не ставь диагнозов и не утверждай причинно-следственные связи. "
        "Данные в формате JSON:\n" + compact_json
    )
    client = AsyncAnthropic(api_key=api_key)
    try:
        response = await client.messages.create(
            model=model,
            max_tokens=280,
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as exc:
        logger.exception("Claude weekly review request failed")
        raise RuntimeError("Не удалось составить обзор недели. Попробуй ещё раз позже.") from exc
    finally:
        await client.close()

    summary = "\n".join(
        block.text for block in response.content if getattr(block, "type", None) == "text"
    ).strip()
    if not summary:
        raise RuntimeError("Обзор получился пустым. Попробуй ещё раз позже.")

    week_start = date.fromisoformat(payload["period"]["start"])
    week_end = date.fromisoformat(payload["period"]["end"])
    async with SessionLocal() as session:
        report = await session.scalar(
            select(WeeklyReport).where(
                WeeklyReport.user_id == user_id,
                WeeklyReport.week_start == week_start,
            )
        )
        if report is None:
            report = WeeklyReport(
                user_id=user_id,
                week_start=week_start,
                week_end=week_end,
                summary=summary,
                data_json=compact_json,
            )
            session.add(report)
        else:
            report.week_end = week_end
            report.summary = summary
            report.data_json = compact_json
            report.created_at = datetime.utcnow()
        await session.commit()

    delivered = False
    delivery_note = "Обзор сохранён в приложении."
    if not is_demo:
        try:
            from bot import send_telegram_message

            await send_telegram_message(
                telegram_id,
                f"Итоги твоей недели\n\n{summary}",
            )
            delivered = True
            delivery_note = "Обзор также отправлен в Telegram."
        except Exception:
            logger.exception("Weekly report saved but Telegram delivery failed for %s", telegram_id)
            delivery_note = "Обзор сохранён в приложении, но отправить его в Telegram не удалось."

    return {
        "summary": summary,
        "week_start": week_start.isoformat(),
        "week_end": week_end.isoformat(),
        "delivered": delivered,
        "delivery_note": delivery_note,
    }


@app.post("/api/reports/weekly")
@app.post("/api/reports/generate")
async def weekly_review(identity: AuthIdentity = Depends(authenticated_identity)) -> dict:
    try:
        return await generate_weekly_report(
            telegram_id=identity.telegram_id,
            first_name=identity.first_name,
            is_demo=identity.is_demo,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
