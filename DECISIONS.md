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

## 2026-10-16: LLM calls go through one wrapper with a swappable sender
- **Decision:** `LLMClient` builds every request; the network sits behind a `MessageSender` protocol (`AnthropicSender` in production, `FakeSender` in tests and the offline demo).
- **Alternatives:** mocking the `anthropic` SDK directly in tests.
- **Reason:** tests can assert on the exact request (cache breakpoints, schema, model) without patching SDK internals, and the demo runs offline.

## 2026-10-16: Structured output via `output_config.format` with our own validation
- **Decision:** requests carry a strict JSON Schema generated from the pydantic model; the reply is parsed with pydantic, and one repair round is attempted on invalid output.
- **Alternatives:** `client.messages.parse()`; forced tool use.
- **Reason:** forced `tool_choice` is rejected by Claude Sonnet 5.5; keeping the parse step ours makes the invalid-JSON fallback testable. Range limits (min/max) are stripped from the schema sent to the API and enforced by pydantic.

## 2026-10-16: Effort defaults
- **Decision:** bulk scoring runs at effort `low`, conversation at `medium`; both are configurable.
- **Reason:** scoring is high-volume classification; conversation needs more judgment but must stay fast for voice.

## 2026-10-16: Refusal fallback on by default for the conversation model
- **Decision:** conversation calls on Claude Sonnet 5.5 send `fallbacks: "default"` with the `server-side-fallback-2026-07-01` beta header, so a refused request is retried on a fallback model server-side. `refusal_fallback: false` turns it off.
- **Reason:** a false-positive refusal mid-conversation would otherwise leave the CEO with no answer.

## 2026-10-16: Cost estimation and budget
- **Decision:** cost is estimated from a price table (USD per million tokens; cache writes at 1.25x input). Unknown models are priced like the most expensive known model. The month is computed in the configured timezone.
- **Reason:** errs toward stopping early rather than overspending.

## 2026-10-16: All stored datetimes are UTC and timezone-aware
- **Decision:** a `UTCDateTime` column type converts to UTC on write, tags UTC on read, and rejects naive datetimes.
- **Reason:** SQLite drops timezone info; without this, comparisons across the configured timezone silently go wrong.

## 2026-10-16: The interface must look futuristic (requested by Ashish)
- **Decision:** the PWA (Milestone 13) and any desktop UI use a futuristic visual style: dark-first, glowing accent colors, glassy translucent panels, an animated voice orb in place of a plain mic button, and smooth motion. Readability at 390px and the light-mode option stay required.
- **Reason:** Ashish asked for the interface to look futuristic.

## 2026-10-16: Sync HTTP client built on httpx, retries in our code
- **Decision:** adapters share `HttpClient` (httpx, synchronous). It retries 429/5xx and network errors with jittered exponential backoff, honours `Retry-After` (seconds or HTTP date), and on a 401 refreshes the OAuth token once before retrying.
- **Alternatives:** each vendor's Python SDK; async httpx.
- **Reason:** one retry policy for every service, no extra paid or heavy SDKs, and `respx` mocks it cleanly. Sync keeps adapters simple; the scheduler runs them in worker threads.

## 2026-10-16: OAuth start returns JSON instead of redirecting
- **Decision:** `GET /oauth/{provider}/start` needs the bearer token and returns `{authorization_url}`; the client opens it. The callback is unauthenticated and protected by a one-time `state` (10-minute TTL) plus PKCE.
- **Reason:** a browser redirect cannot carry the bearer header, and the start endpoint must not be open to anyone who finds the URL.

## 2026-10-16: Microsoft calendar uses `calendarView/delta`
- **Decision:** Outlook events come from the delta endpoint with `Prefer: outlook.timezone="UTC"`; the stored cursor is the full `@odata.deltaLink`. A 410 restarts a full sync. Google uses `nextSyncToken` the same way.
- **Reason:** both providers' supported incremental-sync mechanisms; UTC avoids Windows timezone-name mapping.

## 2026-10-16: Graph send returns no message id
- **Decision:** `OutlookMailAdapter.send` returns an empty string.
- **Reason:** `/me/sendMail` and `/reply` return 202 with no body. Acknowledgement tracking uses the conversation id instead.

## 2026-10-16: Enabled-but-unconfigured adapters are skipped, not fatal
- **Decision:** `build_adapters` skips an enabled adapter whose credentials are missing and logs exactly which variables to set.
- **Reason:** the assistant should run with whatever the CEO has connected so far.
