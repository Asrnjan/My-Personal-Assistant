# My Personal Assistant

A hands-free, voice-first "Chief of Staff" assistant for a CEO. It collects tasks and meetings from calendar, email and task tools, prioritizes them with Claude, asks short spoken questions, and then acts: closes tasks, delegates them with notifications, declines or forwards meetings, and follows up.

The full specification is in [BUILD_PROMPT.md](BUILD_PROMPT.md). Build progress is tracked in [PROGRESS.md](PROGRESS.md) and assumptions in [DECISIONS.md](DECISIONS.md).

## Running the backend locally

```bash
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp ../.env.example ../.env          # then set API_AUTH_TOKEN at least
alembic upgrade head                # creates the SQLite database
uvicorn app.main:create_app --factory --reload
```

Open http://127.0.0.1:8000/docs for the API docs.

## Checks

```bash
cd backend
ruff check . && ruff format --check .
mypy
pytest
```
