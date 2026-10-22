# Progress

BUILD COMPLETE

## Final summary

**What was built.** A voice-first Chief of Staff for a CEO, per BUILD_PROMPT.md:

- A FastAPI backend that syncs Google or Microsoft 365 calendars and mail plus Jira, Asana or ClickUp; scores every item with a rubric and Claude (Haiku in bulk, Sonnet for second opinions and conversation, prompt caching, structured outputs, a cost ledger and a monthly cap); builds a spoken morning brief, evening wrap-up and P1 alerts; holds a dialogue with tools where nothing happens without the CEO's yes; delegates with a drafted brief over email, Telegram, Slack and Teams, reassigns the source task or forwards the meeting, tracks acknowledgement, nudges and flags overdue work; and learns from overrides.
- A Telegram bot (voice notes in and out, Yes/Cancel and Accept/Question buttons), an installable phone app with Web Push and a "brief me" deep link for Siri and Google Assistant, and a desktop app with a "hey jarvis" wake word and local speech.
- Security: encrypted OAuth tokens, redacted logs, per-client tokens, rate limits, strict headers, untrusted-content wrapping, recipient allow-listing and a never-list for money and deletion.

**How to run it.** `backend/.venv/bin/python scripts/seed_demo_data.py` for the offline demo; `scripts/dev.sh` to run it (app at http://127.0.0.1:8000/); `docker compose up --build` as an alternative. Accounts and keys: docs/SETUP.md. Phone access: docs/DEPLOY.md. Daily use: docs/USER_GUIDE.md.

**Checks.** Backend: 282 passing tests (the ten Section 10 scenarios included; one live smoke test runs only with `RUN_LIVE_TESTS=1`), 97% coverage, `ruff` and `mypy --strict` clean. Desktop: 233 tests, 99% coverage. Phone app: 23 Playwright tests at phone width. CI runs all of them plus a Docker build and smoke test. The only paid dependency is the Anthropic API.

**Known limitations.**
- Adapter fixtures follow each API's documented shapes; they haven't been checked against live accounts yet.
- Teams is send-only (incoming webhooks), so Teams delegations are acknowledged through the email copy.
- Asana only sees tasks assigned to the CEO.
- The built-in scheduler only runs while the backend runs; a sleeping laptop misses briefs (docs/SCHEDULING.md has the cron and GitHub Actions alternative).
- The desktop app's wake word, microphone and tray were tested with fakes; first use on a real Windows or Mac machine should be watched.
- iPhone Web Push needs iOS 16.4+ and the home-screen install.

**Suggested next steps.**
1. Connect one real Google or Microsoft account and Jira, run a week, and tune `config/rubric.yaml` from the overrides it learns.
2. Record live API responses into the adapter fixtures.
3. Add a Telegram webhook mode for servers with a public URL, and a Teams bot if acknowledging in Teams matters.
4. Add a small settings screen in the phone app for contacts and learned preferences (today they are edited in YAML and through `/docs`).

---

Milestones from BUILD_PROMPT.md Section 11.

- [x] 1. Foundation: config loading (env + YAML), SQLite + Alembic, domain models, redacting logger, bearer-token auth, `/health`, CI (ruff, mypy --strict, pytest with 85% coverage gate)
- [x] 2. LLM layer: `LLMClient` with Haiku/Sonnet routing and per-task effort, cached static system prompt with a safety preamble, strict JSON-schema structured output with one repair retry, refusal and truncation errors, server-side refusal fallback for Sonnet, SDK retries, per-call usage and cost ledger, monthly budget cap (rules-only signal + one audit entry per month), `GET /usage`, `FakeSender` for tests and the demo, opt-in live smoke test
- [x] 3. Integrations: shared HTTP client (exponential backoff, Retry-After, one OAuth refresh on 401), Google Calendar + Gmail, Microsoft Graph calendar + mail (delta sync), Jira, Asana, ClickUp, OAuth with PKCE and one-time state, Fernet-encrypted token storage, `/oauth/{provider}/start|callback`, sync-cursor store, adapter registry driven by config, `docs/SETUP.md`
- [x] 4. Ingestion & normalization: `Ingestor` pulls meetings (today + tomorrow, incremental via sync tokens/delta links, full resync drops vanished meetings), tasks (full snapshot; done or gone at source closes them) and actionable email (since last run); dedup by iCalUID across calendars and invite-email title matching; CEO decisions never reopened by a sync; content hash clears `scored_at` so changed items get re-scored; one failing adapter does not stop the others; `POST /sync`, `GET /items?date=&band=&status=`, `GET /items/{id}`; in-memory fake adapters for tests and the demo
- [x] 5. Priority engine: rubric (`config/rubric.yaml`) and delegation map (`config/contacts.yaml`) loaded at startup, delegates seeded insert-only; deterministic pre-score per criterion (goals, revenue, urgency, requester tier, deadline, CEO-required) with hard rules; Haiku scores open unscored items in batches of 25 with rubric, delegation map and learned preferences in the cached system prompt and item content marked untrusted; Sonnet second opinion for the top 5 and anything under 0.6 confidence; hard rules always win on band and score floor; suggested delegates validated against the contacts list; rules-only scoring when the budget cap is hit or a call fails; `POST /sync` now scores after ingesting
- [x] 6. Briefing & scheduler: template-built morning brief (counts, what needs the CEO, suggested delegations, declines and drops, "Shall I go ahead?"), evening wrap-up (still open, tonight's meetings, unacknowledged and accepted delegations, rollover proposal, tomorrow's first meeting), P1 alerts only for new or changed P1 items (one combined alert per sync, items already in a delivered brief are skipped); trimmed to 45 spoken seconds with a per-item "tell me more" list; APScheduler jobs in the configured timezone (morning, evening, background sync every 15 min); delivered briefs stored and pushed on an in-process event bus; `GET /brief/today`, `GET /brief/latest`, `POST /brief/deliver`; `/sync` now also sends alerts; `docs/SCHEDULING.md` with cron and GitHub Actions alternatives
- [x] 7. Conversation agent: `POST /converse` and `WS /ws`; Sonnet with tool use (list_items, get_item, explain_priority, delegate_item, decline_meeting, defer_item, close_task, drop_item, set_priority, set_preference, confirm_pending_action, cancel_pending_action); dialogue rules handled in code ("brief me", "yes", "go ahead with all", "cancel", "skip the second one"); dialogue state per conversation (recent turns, numbered references, actions awaiting a yes); approval policy from `config/approval_policy.yaml` (auto / confirm / never, financial and delete requests refused before any model call); every side effect is a pending action that needs the CEO's own yes; `GET /actions`, `POST /actions/{id}/confirm|cancel`; brief suggestions staged as pending actions so "Shall I go ahead?" works
- [x] 8. Delegation: fuzzy + phonetic name resolution (rapidfuzz, soundex/metaphone) with ownership-area tie-break and a "Rahul Kumar or Rahul Mehta?" question; read-back with deadline and channels; Sonnet-drafted brief (template fallback) sent as HTML + text email with a ref in the subject and on every preferred channel; one failing channel doesn't stop the others and the CEO is told; Jira/Asana/ClickUp reassignment plus a comment, meetings forwarded to the delegate and declined with a note; FollowUp with per-channel refs; acknowledgement from email replies (Haiku-classified, keyword fallback), Telegram buttons and polled channels; one nudge 24h before the deadline; overdue marking with a notice; audit entry for every step; `GET /followups`, `GET/POST/PUT/DELETE /delegates`
- [x] 9. Notifiers: Telegram (Accept / Question buttons checked against the delegate's own chat, CEO text and voice notes answered with text plus a voice note, Yes / Cancel buttons for confirmations, scheduled briefs forwarded, `/start` tells anyone their chat id, long-poll loop in a daemon thread), Slack (DM with blocks, acknowledgement by reply or ✅/👍 reaction found by polling, threaded nudges), Teams (Adaptive Card via incoming webhook; acknowledgement via the email copy), CEO notices on the first channel that reaches the CEO, notifier registry driven by `adapters.notify`
- [x] 10. Learning & preferences: overrides (delegating a non-P2 item, choosing a different delegate, declining or dropping a P1/P2, "make this top priority") and explicit rules stored as learning signals and summarized into a cached CEO-preferences block for scoring and conversation; `GET/POST /preferences`, `DELETE /preferences/{id}`
- [x] 11. Security hardening: per-client token-bucket rate limits (120/min by default, 10/min per address on `POST /auth/token`, `/health` exempt, 429 with Retry-After), security headers with a CSP on the app (not on `/docs`), CORS off unless `CORS_ORIGINS` is set, all secrets redacted from logs; prompt-injection defenses tested end to end (external content wrapped as untrusted with spoofed closing tags escaped, model-suggested delegates checked against contacts, no action without the CEO's own yes, outbound recipients only from contacts or the item's participants, Telegram commands only from the CEO's chat)
- [x] 12. Desktop voice client (`desktop/`): wake word (openWakeWord "hey jarvis") and push-to-talk hotkey, local faster-whisper STT, Piper (or Kokoro) TTS with barge-in, futuristic animated-orb window, tray icon, WebSocket client with REST fallback and reconnect, pushed briefs read aloud, PyInstaller build script; 233 tests, 99% coverage, own CI job
- [x] 13. Mobile PWA (`mobile-pwa/`, served by the backend at `/`): installable, dark glass UI with a voice orb, browser speech recognition with `/stt` upload fallback, `/tts` fallback, live WebSocket events, pending-action Yes/Cancel cards, follow-ups view, Web Push (VAPID; `GET /push/vapid-public-key`, `POST /push/subscribe|unsubscribe`, expired subscriptions dropped), "brief me" deep link for phone shortcuts, offline shell; Playwright tests in CI. Backend `POST /stt` and `GET /tts` run faster-whisper and Piper/Kokoro locally (`pip install -e ".[voice]"`)
- [x] 14. Docs: README, `docs/SETUP.md` (quick start, Claude key, Google, Microsoft, Jira, Asana, ClickUp, contacts and policy, Telegram via BotFather, Slack, Teams, voices, phone app install on Android and iOS, Web Push keys, desktop app), `docs/DEPLOY.md` (Cloudflare Tunnel, Oracle Cloud Always Free, Docker Compose, backups), `docs/VOICE_SHORTCUTS.md` (Siri Shortcut, Google Assistant routine), `docs/ARCHITECTURE.md`, `docs/USER_GUIDE.md`, `docs/SCHEDULING.md`
- [x] 15. Full E2E pass: all ten Section 10 scenarios in `backend/tests/e2e/test_scenarios.py`; `scripts/seed_demo_data.py` runs morning brief → delegation → follow-up → wrap-up offline (and is itself tested); `scripts/dev.sh` starts the backend serving the phone app; `scripts/run_loop.sh`; `docker-compose.yml` with an optional Cloudflare tunnel, built and smoke-tested in CI

## Current work

All milestones done.

### Modern UI and in-app setup

- [x] Phone app redesign: night-sky theme, bottom tab bar (Home, Team, Chat, Apps), greeting, quick-request chips, the latest reply under the assistant, back button moves between tabs.
- [x] The blue orb is replaced by an edgeless sphere of stars that behaves like a drop of water (`mobile-pwa/cluster.js`, canvas): about 1,200 stars in a Gaussian cloud with no outline. Each syllable you say (live microphone level, or recognised words) lands somewhere new and ripples travel around the surface; the assistant's speech lands as warm ripples; thinking drops land in a circling ring; calling its name, tapping or sending a message makes the whole drop wobble and flash; touching it sends ripples from the touched point. Idle, it turns, breathes and takes an occasional faint drip. Reduced motion draws still frames.
- [x] Apps tab: every integration in one place with steps, a link and one or two fields. New `GET/PUT/DELETE /connections/{app}` checks credentials with the service, looks up ids (Asana workspace, ClickUp team and user, Telegram chat, Slack member), stores them encrypted (`app_connections`, migration 0010) and applies them live: the Claude key, task boards and chat channels switch on without a restart or config edit, and Telegram's bot restarts with the new token.
- [x] Home setup checklist until the device, Claude and email are connected.

## Open issues

- Adapter fixtures are hand-written from each API's documented response shapes, not recorded from live accounts; refresh them from a real account when one is connected.
- Asana lists tasks assigned to the CEO only; its API has no filter for tasks the CEO created across a workspace.

- Starlette warns that `httpx` with its TestClient is deprecated; harmless for now, revisit when upgrading FastAPI.
