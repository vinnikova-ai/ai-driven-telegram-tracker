# Habit Loop

A lightweight Telegram Mini App for daily habit tracking, streaks, XP, and Claude-generated weekly reviews.

## Run & Operate

- `uv run python main.py` — run FastAPI and aiogram together locally; defaults to port 8000.
- The API artifact workflow runs that same entry point on port 8080.
- `pnpm --filter @workspace/habit-loop run dev` — serve the static web app.
- `pnpm run typecheck` — typecheck the remaining TypeScript workspace libraries.
- Required Replit Secrets: `TELEGRAM_BOT_TOKEN`, `ANTHROPIC_API_KEY`.
- Optional environment variables: `TELEGRAM_WEBAPP_URL`, `ANTHROPIC_MODEL`.

## Stack

- Python 3.13, FastAPI, aiogram 3, SQLAlchemy async, SQLite, Anthropic SDK, and python-dotenv.
- Frontend: plain HTML, CSS, and JavaScript. Vite serves static assets only; the UI does not use React or Vue.
- The existing pnpm workspace remains for the static web artifact and shared API contract tooling.

## Where things live

- `main.py` — starts FastAPI and aiogram concurrently in one asyncio process.
- `database.py` — SQLite engine and Users, Habits, DailyLogs, and WeeklyReports models.
- `bot.py` — `/start` Web App button, `/weekly`, and Telegram message delivery.
- `api.py` — API routes, signed Telegram Mini App validation, XP, streaks, and Claude summaries.
- `templates/index.html`, `static/app.js`, `static/styles.css` — canonical vanilla frontend.
- `artifacts/habit-loop/scripts/sync-static.mjs` — copies the canonical frontend into the static web artifact.
- `tracker.db` — local SQLite file created at runtime; ignored by Git.

## Architecture decisions

- Keep FastAPI and the Telegram bot in one Python process to minimize background processes.
- Use SQLite in `tracker.db`; do not replace it with the workspace PostgreSQL database.
- Production API calls require signed Telegram `initData`; the synthetic preview profile is limited to localhost and `*.replit.dev`.
- Award 10 XP once per habit per day; unchecking a completion does not permit repeat XP farming.

## Product

- Users open the dashboard from Telegram, add and remove habits, check them off, view streaks and XP, and generate a weekly Claude review that is saved and sent to Telegram.

## User preferences

- Keep the frontend framework-free: vanilla JavaScript, HTML5, and CSS3.
- Keep the Python app within the requested Free Tier memory limit; avoid heavy runtimes and background workers.

## Gotchas

- Telegram Web Apps require a public HTTPS URL. Set `TELEGRAM_WEBAPP_URL` to the published URL and register its domain with BotFather for production use.
- `uv run python main.py` needs `TELEGRAM_BOT_TOKEN`; weekly AI reviews also need `ANTHROPIC_API_KEY`.

## Pointers

- `README.md` contains setup and operating instructions.
