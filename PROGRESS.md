# Progress

Milestones from BUILD_PROMPT.md Section 11.

- [x] 1. Foundation: config loading (env + YAML), SQLite + Alembic, domain models, redacting logger, bearer-token auth, `/health`, CI (ruff, mypy --strict, pytest with 85% coverage gate)
- [x] 2. LLM layer: `LLMClient` with Haiku/Sonnet routing and per-task effort, cached static system prompt with a safety preamble, strict JSON-schema structured output with one repair retry, refusal and truncation errors, server-side refusal fallback for Sonnet, SDK retries, per-call usage and cost ledger, monthly budget cap (rules-only signal + one audit entry per month), `GET /usage`, `FakeSender` for tests and the demo, opt-in live smoke test
- [x] 3. Integrations: shared HTTP client (exponential backoff, Retry-After, one OAuth refresh on 401), Google Calendar + Gmail, Microsoft Graph calendar + mail (delta sync), Jira, Asana, ClickUp, OAuth with PKCE and one-time state, Fernet-encrypted token storage, `/oauth/{provider}/start|callback`, sync-cursor store, adapter registry driven by config, `docs/SETUP.md`
- [x] 4. Ingestion & normalization: `Ingestor` pulls meetings (today + tomorrow, incremental via sync tokens/delta links, full resync drops vanished meetings), tasks (full snapshot; done or gone at source closes them) and actionable email (since last run); dedup by iCalUID across calendars and invite-email title matching; CEO decisions never reopened by a sync; content hash clears `scored_at` so changed items get re-scored; one failing adapter does not stop the others; `POST /sync`, `GET /items?date=&band=&status=`, `GET /items/{id}`; in-memory fake adapters for tests and the demo
- [x] 5. Priority engine: rubric (`config/rubric.yaml`) and delegation map (`config/contacts.yaml`) loaded at startup, delegates seeded insert-only; deterministic pre-score per criterion (goals, revenue, urgency, requester tier, deadline, CEO-required) with hard rules; Haiku scores open unscored items in batches of 25 with rubric, delegation map and learned preferences in the cached system prompt and item content marked untrusted; Sonnet second opinion for the top 5 and anything under 0.6 confidence; hard rules always win on band and score floor; suggested delegates validated against the contacts list; rules-only scoring when the budget cap is hit or a call fails; `POST /sync` now scores after ingesting
- [x] 6. Briefing & scheduler: template-built morning brief (counts, what needs the CEO, suggested delegations, declines and drops, "Shall I go ahead?"), evening wrap-up (still open, tonight's meetings, unacknowledged and accepted delegations, rollover proposal, tomorrow's first meeting), P1 alerts only for new or changed P1 items (one combined alert per sync, items already in a delivered brief are skipped); trimmed to 45 spoken seconds with a per-item "tell me more" list; APScheduler jobs in the configured timezone (morning, evening, background sync every 15 min); delivered briefs stored and pushed on an in-process event bus; `GET /brief/today`, `GET /brief/latest`, `POST /brief/deliver`; `/sync` now also sends alerts; `docs/SCHEDULING.md` with cron and GitHub Actions alternatives
- [x] 7. Conversation agent: `POST /converse` and `WS /ws`; Sonnet with tool use (list_items, get_item, explain_priority, delegate_item, decline_meeting, defer_item, close_task, drop_item, set_priority, set_preference, confirm_pending_action, cancel_pending_action); dialogue rules handled in code ("brief me", "yes", "go ahead with all", "cancel", "skip the second one"); dialogue state per conversation (recent turns, numbered references, actions awaiting a yes); approval policy from `config/approval_policy.yaml` (auto / confirm / never, financial and delete requests refused before any model call); every side effect is a pending action that needs the CEO's own yes; `GET /actions`, `POST /actions/{id}/confirm|cancel`; brief suggestions staged as pending actions so "Shall I go ahead?" works
- [x] 8. Delegation: fuzzy + phonetic name resolution (rapidfuzz, soundex/metaphone) with ownership-area tie-break and a "Rahul Kumar or Rahul Mehta?" question; read-back with deadline and channels; Sonnet-drafted brief (template fallback) sent as HTML + text email with a ref in the subject and on every preferred channel; one failing channel doesn't stop the others and the CEO is told; Jira/Asana/ClickUp reassignment plus a comment, meetings forwarded to the delegate and declined with a note; FollowUp with per-channel refs; acknowledgement from email replies (Haiku-classified, keyword fallback), Telegram buttons and polled channels; one nudge 24h before the deadline; overdue marking with a notice; audit entry for every step; `GET /followups`, `GET/POST/PUT/DELETE /delegates`
- [ ] 9. Notifiers
- [x] 10. Learning & preferences: overrides (delegating a non-P2 item, choosing a different delegate, declining or dropping a P1/P2, "make this top priority") and explicit rules stored as learning signals and summarized into a cached CEO-preferences block for scoring and conversation; `GET/POST /preferences`, `DELETE /preferences/{id}`
- [ ] 11. Security hardening
- [ ] 12. Desktop voice client
- [ ] 13. Mobile PWA
- [ ] 14. Docs
- [ ] 15. Full E2E pass

## Current work

Next: Milestone 9, notifiers (Telegram, Slack, Teams; email notifier already in place).

## Open issues

- Adapter fixtures are hand-written from each API's documented response shapes, not recorded from live accounts; refresh them from a real account when one is connected.
- Asana lists tasks assigned to the CEO only; its API has no filter for tasks the CEO created across a workspace.

- Starlette warns that `httpx` with its TestClient is deprecated; harmless for now, revisit when upgrading FastAPI.
- The budget-exceeded callback is not yet wired to a notifier; that happens with Milestone 9.
- Pushed briefs and alerts reach only in-process subscribers until `WS /ws` (Milestone 7) and the notifiers (Milestone 9) subscribe to the event bus.
- Desktop and PWA CI jobs will be added with Milestones 12 and 13.
