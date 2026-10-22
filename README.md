# My Personal Assistant

A hands-free, voice-first "Chief of Staff" for a CEO. It reads your calendar, inbox and task boards (Google or Microsoft 365; Jira, Asana or ClickUp), decides with Claude what needs you, what to hand off and what to drop, tells you in a short spoken brief, and then acts once you say yes: delegates with a proper brief over email, Telegram, Slack or Teams, reassigns the task, declines meetings with a note, and follows up until your team has accepted.

The only paid service is the Claude API. Everything else is free and runs on your own computer or a free server.

## Try it in two minutes, offline

```bash
git clone https://github.com/Asrnjan/My-Personal-Assistant.git && cd My-Personal-Assistant
python3 -m venv backend/.venv && backend/.venv/bin/pip install -e "backend[dev]"
backend/.venv/bin/python scripts/seed_demo_data.py
```

That runs a whole Friday (morning brief, "go ahead with all", delegations, a reply from your team, the evening wrap-up) against in-memory accounts and a stand-in for Claude. Nothing leaves your machine.

## Run it on Windows (no Linux needed)

1. Download the code: on GitHub, **Code → Download ZIP**, then right-click the ZIP and choose **Extract All**.
2. Open the extracted folder and double-click **Start Atlas.bat**.

The first run finds Python (or offers to install it with one keypress), sets everything up, shows your sign-in secret and opens the app in your browser. After that, double-clicking **Start Atlas.bat** just starts it. Keep its window open while you use Atlas.

Prefer typing? Everything after the first start can be done from Command Prompt in the same folder with the `atlas` command: `atlas status`, `atlas connect claude sk-ant-...`, `atlas keys microsoft ...`, `atlas signin outlook`, `atlas team add ...`, `atlas say "brief me"`, `atlas phone`. Type `atlas` for the full list.

## Run it for real

```bash
scripts/dev.sh
```

The first run sets up everything it can and prints your sign-in secret. Open http://127.0.0.1:8000/ for the app and http://127.0.0.1:8000/docs for the API. Then follow [docs/SETUP.md](docs/SETUP.md) to connect your accounts, and [docs/DEPLOY.md](docs/DEPLOY.md) to reach it from your phone (free, with Cloudflare Tunnel). Prefer Docker? `docker compose up --build`.

## What's in the box

| Part | Where | What it is |
|---|---|---|
| Backend | `backend/` | FastAPI + SQLite. Integrations, Claude-powered prioritization, the conversation agent, approval policy, delegation, follow-ups, scheduler, learning |
| Phone app | `mobile-pwa/` | Installable web app for Android and iOS (no app store): talk to it, see your brief, approve with a tap, get push notifications. Served by the backend at `/` |
| Desktop app | `desktop/` | "Hey jarvis" wake word, local speech recognition and voice, tray icon on Windows and macOS |
| Telegram bot | built into the backend | Voice notes in and out, Yes/Cancel buttons, and Accept/Question buttons for your team |

## Documentation

- [SETUP.md](docs/SETUP.md): accounts, keys, contacts, Telegram, Slack, Teams, voices, phone and desktop apps
- [DEPLOY.md](docs/DEPLOY.md): Cloudflare Tunnel, Oracle Cloud Always Free, Docker
- [VOICE_SHORTCUTS.md](docs/VOICE_SHORTCUTS.md): "Hey Siri, daily brief" and "Hey Google, daily brief"
- [USER_GUIDE.md](docs/USER_GUIDE.md): your day with the assistant and what you can say
- [ARCHITECTURE.md](docs/ARCHITECTURE.md): how it's built and how it stays safe
- [SCHEDULING.md](docs/SCHEDULING.md): brief times, and cron or GitHub Actions instead of the built-in scheduler

The original specification is [BUILD_PROMPT.md](BUILD_PROMPT.md); [PROGRESS.md](PROGRESS.md) tracks what's built and [DECISIONS.md](DECISIONS.md) records every assumption.

## Development checks

```bash
cd backend && ruff check . && ruff format --check . && mypy && pytest     # backend
cd desktop && ruff check . && mypy && pytest                               # desktop app
cd mobile-pwa && npm ci && npx playwright install chromium && npx playwright test   # phone app
```

CI runs all three on every push.
