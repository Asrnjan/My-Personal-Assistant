# Architecture

```
                ┌──────────────────────────────────────────────┐
                │          Python backend (FastAPI)            │
                │  integrations · priority engine (Claude)     │
                │  conversation agent · approval policy        │
                │  delegation · follow-ups · scheduler         │
                │  learning · SQLite                           │
                └───────────────┬──────────────────────────────┘
                     REST + WebSocket (bearer tokens)
          ┌─────────────────────┼─────────────────────────────┐
          ▼                     ▼                             ▼
 Desktop voice client     Phone app (PWA, served at /)   Telegram bot
 openWakeWord, faster-    browser speech, /stt + /tts    voice notes in/out,
 whisper, Piper/Kokoro    fallback, Web Push             delegate buttons
```

The backend is the single source of truth; clients only listen, speak and show. Everything that costs money goes through one place (`app/llm/client.py`), and everything that changes the outside world goes through one place (`app/engine/actions.py`).

## Backend layout

| Path | What it does |
|---|---|
| `app/main.py` | App factory: builds every service, wires the event bus, starts the scheduler and the Telegram bot, mounts the routers and the PWA |
| `app/config.py` | `.env` → `Settings` (secrets), `config/config.yaml` → `AppConfig` |
| `app/db/` | SQLAlchemy 2 models and Alembic migrations (SQLite) |
| `app/domain/` | Plain models: items, decisions, delegates, enums |
| `app/integrations/` | Adapters behind three interfaces (`CalendarAdapter`, `EmailAdapter`, `TaskAdapter`): Google, Microsoft Graph, Jira, Asana, ClickUp; OAuth with encrypted token storage; a shared HTTP client with retries and rate-limit handling |
| `app/integrations/notify/` | `Notifier` implementations: email, Telegram, Slack, Teams; the Telegram bot loop |
| `app/llm/` | The Claude wrapper (model routing, prompt caching, structured outputs, retries, cost ledger, budget cap), prompts and schemas, and an offline fake |
| `app/engine/ingest.py`, `sync.py` | Pull, normalize and de-duplicate meetings, tasks and actionable email into `items` |
| `app/engine/priority.py`, `rubric.py` | Rules pre-score, then Haiku scores in batches, Sonnet second opinion for the top items and low-confidence calls; hard rules always win |
| `app/engine/briefing.py` | Morning brief, evening wrap-up and P1 alerts, built from templates and trimmed to a spoken length |
| `app/engine/conversation.py` | The dialogue agent: fixed rules for "brief me / yes / go ahead with all / cancel / skip the second one", Sonnet with tools for everything else |
| `app/engine/approval.py` | `approval_policy.yaml`: auto, confirm or never per action; refuses money and deletion requests outright |
| `app/engine/actions.py` | Pending actions: proposed, read back, executed only on the CEO's yes |
| `app/engine/delegation.py`, `recipients.py`, `contacts.py` | Name resolution, brief drafting, multi-channel dispatch, reassigning the source task or forwarding the meeting, recipient allow-list |
| `app/engine/followup.py` | Acknowledgement from email replies, buttons and polled channels; nudges; overdue marking |
| `app/engine/learning.py` | Turns overrides and explicit rules into a preferences block for every prompt |
| `app/engine/notices.py` | Tells the CEO about background events on the first channel that reaches them |
| `app/scheduler.py` | APScheduler jobs: morning, evening, sync every 15 minutes, follow-ups every 30 |
| `app/events.py` | In-process event bus: briefs and notices go to WebSocket clients, Web Push and Telegram |
| `app/push.py`, `app/voice.py` | Web Push (VAPID) and server-side speech (faster-whisper, Piper, Kokoro) |
| `app/api/` | Routers, auth, WebSocket, rate limiting and security headers |

## A morning, step by step

1. **Sync** (`POST /sync` or every 15 minutes): adapters pull changes; items are upserted by source id and de-duplicated across calendars by iCalUID. Changed items lose their score.
2. **Score**: rules give a pre-score from the rubric (requester tier, deadline, goals); Haiku scores unscored items 25 at a time with the rubric, delegation map and learned preferences in a cached system prompt; the top five and anything under 0.6 confidence get a Sonnet second opinion. Hard rules (for example "anything from the board is P1") override the model. A new or changed P1 triggers an alert.
3. **Brief** (08:00 or "brief me"): who needs you, what to delegate and to whom, what to decline or drop, then "Shall I go ahead?". Each suggestion is staged as a pending action.
4. **Approve**: "yes" or "go ahead with all" confirms the staged actions; "skip the second one" or "cancel" removes them. Nothing changes in any outside system before this point.
5. **Delegate**: Sonnet drafts the brief (a template if the model is unavailable), it goes out on each of the delegate's channels, the Jira/Asana/ClickUp task is reassigned with a comment (or the meeting is forwarded and declined with a note), and a follow-up is created. Every step is in the audit log.
6. **Follow up** (every 30 minutes): replies on the email thread from the delegate's own address, Telegram buttons and Slack replies/reactions mark it accepted or raise a question; one nudge 24 hours before the deadline; overdue after it.
7. **Wrap up** (18:30): what's still open, who accepted, who hasn't answered, and an offer to roll the rest to tomorrow.

## Safety model

- **Only the CEO's words cause actions.** Email, task and meeting text is wrapped as `<untrusted>` data in every prompt (with spoofed closing tags escaped), the system prompt says instructions inside it are never followed, and a tool call can only *propose* an action. Execution needs a yes in the CEO's own current utterance or a tap in an authenticated client.
- **Recipients are allow-listed.** Messages go only to people in `contacts.yaml` or participants of the item itself. A model-suggested delegate that isn't a contact is dropped.
- **Never-list.** Requests to pay, buy, refund, approve spending or delete data are refused before any model call. Delegating such work to a person is fine.
- **Secrets.** OAuth tokens are Fernet-encrypted at rest; every secret is redacted from logs; clients exchange the shared secret for their own revocable token.
- **Network.** Per-client rate limits, a tight limit on the sign-in endpoint, strict security headers and CSP, CORS off by default. Telegram commands are accepted only from the CEO's chat id; delegate buttons only from that delegate's chat.

## Cost model

Claude is the only paid service. Haiku does the bulk scoring; Sonnet does conversation, briefs and the few hard calls. Static prompt parts (safety rules, rubric, delegation map, preferences) are cached. Every call's tokens and estimated cost go to `llm_usage`; `GET /usage` shows today's and this month's spend. Past `budget.monthly_usd`, scoring falls back to rules only and you are told once.

## Testing

- Backend: unit tests per engine module, `respx`-mocked integration tests for every adapter and notifier, a fake Claude (`app/llm/fake.py`), and ten end-to-end scenarios in `backend/tests/e2e/`. `mypy --strict`, `ruff`, 85% coverage gate.
- Desktop: speech, wake-word state machine and reconnect logic with WAV fixtures and fakes.
- Phone app: Playwright at phone width with speech APIs stubbed.
- `scripts/seed_demo_data.py` runs the whole flow offline.
