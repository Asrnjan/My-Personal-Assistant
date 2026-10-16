# Progress

Milestones from BUILD_PROMPT.md Section 11.

- [x] 1. Foundation: config loading (env + YAML), SQLite + Alembic, domain models, redacting logger, bearer-token auth, `/health`, CI (ruff, mypy --strict, pytest with 85% coverage gate)
- [x] 2. LLM layer: `LLMClient` with Haiku/Sonnet routing and per-task effort, cached static system prompt with a safety preamble, strict JSON-schema structured output with one repair retry, refusal and truncation errors, server-side refusal fallback for Sonnet, SDK retries, per-call usage and cost ledger, monthly budget cap (rules-only signal + one audit entry per month), `GET /usage`, `FakeSender` for tests and the demo, opt-in live smoke test
- [x] 3. Integrations: shared HTTP client (exponential backoff, Retry-After, one OAuth refresh on 401), Google Calendar + Gmail, Microsoft Graph calendar + mail (delta sync), Jira, Asana, ClickUp, OAuth with PKCE and one-time state, Fernet-encrypted token storage, `/oauth/{provider}/start|callback`, sync-cursor store, adapter registry driven by config, `docs/SETUP.md`
- [ ] 4. Ingestion & normalization
- [ ] 5. Priority engine
- [ ] 6. Briefing & scheduler
- [ ] 7. Conversation agent
- [ ] 8. Delegation
- [ ] 9. Notifiers
- [ ] 10. Learning & preferences
- [ ] 11. Security hardening
- [ ] 12. Desktop voice client
- [ ] 13. Mobile PWA
- [ ] 14. Docs
- [ ] 15. Full E2E pass

## Current work

Next: Milestone 4, ingestion and normalization.

## Open issues

- Adapter fixtures are hand-written from each API's documented response shapes, not recorded from live accounts; refresh them from a real account when one is connected.
- Asana lists tasks assigned to the CEO only; its API has no filter for tasks the CEO created across a workspace.

- Starlette warns that `httpx` with its TestClient is deprecated; harmless for now, revisit when upgrading FastAPI.
- The budget-exceeded callback is not yet wired to a notifier; that happens with Milestone 9.
- Desktop and PWA CI jobs will be added with Milestones 12 and 13.
