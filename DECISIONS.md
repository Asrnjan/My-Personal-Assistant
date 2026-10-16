# Decisions

Each entry records a decision, the alternatives considered, and the reason.

## 2026-10-16: Repository root is the project root
- **Decision:** `backend/`, `config/`, `docs/` etc. live at the repo root instead of under a `ceo-assistant/` folder as sketched in BUILD_PROMPT.md Section 4.
- **Alternatives:** a nested `ceo-assistant/` folder.
- **Reason:** the repo is dedicated to this app, so the extra level only lengthens every path.

## 2026-10-16: Client tokens are issued per client and stored hashed
- **Decision:** `POST /auth/token` exchanges the shared `API_AUTH_TOKEN` for a random per-client bearer token; only its SHA-256 hash is stored, and tokens can be revoked.
- **Alternatives:** every client sends `API_AUTH_TOKEN` directly; JWTs.
- **Reason:** per-client tokens can be revoked one at a time, a leaked database leaks no usable token, and there is no signing key to manage.

## 2026-10-16: Audit log immutability enforced in the ORM
- **Decision:** SQLAlchemy `before_update`/`before_delete` hooks on `AuditLogRow` raise `ImmutableRecordError`.
- **Alternatives:** SQLite triggers.
- **Reason:** works the same on any database and is easy to test; a trigger can be added later if raw SQL access becomes a concern.

## 2026-10-16: Enums stored as strings
- **Decision:** enum columns use `native_enum=False` and store the enum value.
- **Alternatives:** native database enums.
- **Reason:** SQLite has no native enums, and string storage keeps migrations simple when values are added.

## 2026-10-16: CI runs on Python 3.11 and 3.12
- **Decision:** the backend CI matrix covers 3.11 (the minimum in the spec) and 3.12.
- **Reason:** catches incompatibilities on both ends without slowing CI much.
