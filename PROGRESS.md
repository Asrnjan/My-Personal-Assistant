# Progress

Milestones from BUILD_PROMPT.md Section 11.

- [x] 1. Foundation: config loading (env + YAML), SQLite + Alembic, domain models, redacting logger, bearer-token auth, `/health`, CI (ruff, mypy --strict, pytest with 85% coverage gate)
- [ ] 2. LLM layer
- [ ] 3. Integrations
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

Next: Milestone 2, the LLM layer.

## Open issues

- Starlette warns that `httpx` with its TestClient is deprecated; harmless for now, revisit when upgrading FastAPI.
- Desktop and PWA CI jobs will be added with Milestones 12 and 13.
