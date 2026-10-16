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

## 2026-10-16: Deduplication rules
- **Decision:** a meeting with the same iCalUID as one already stored from another calendar is skipped. An email whose subject, with invite prefixes ("Invitation:", "Updated invitation:", "Accepted:") and Google's " @ date" suffix removed, matches a stored meeting's title and whose sender is that meeting's organizer or a participant is treated as the invite and skipped.
- **Alternatives:** parsing `.ics` attachments from emails.
- **Reason:** covers the common Google and Outlook invite formats without downloading attachments; a stranger's email with the same title still becomes its own item.

## 2026-10-16: What a sync may change
- **Decision:** a sync updates content (title, times, people) on every item but changes status only to a terminal state reported by the source (cancelled → dropped, declined at source → declined, done at source → done), and never reopens an item the CEO already decided on.
- **Reason:** the CEO's choices must not be undone by a background job.

## 2026-10-16: Cancelled and vanished meetings become `dropped`
- **Decision:** meetings cancelled at the source, or missing from a full resync of the window, get status `dropped`.
- **Reason:** the domain has no "cancelled" status; `dropped` already means "no longer needs attention".

## 2026-10-16: Re-scoring is driven by a content hash
- **Decision:** each item stores a hash of the fields the priority engine reads; when it changes, `scored_at` is cleared and the previous score is kept until re-scoring.
- **Reason:** avoids paying to re-score unchanged items on every sync while keeping a usable score in the meantime.

## 2026-10-16: Email items use a `mail:` id prefix
- **Decision:** email items are stored with `source_id = "mail:<provider id>"`.
- **Reason:** Outlook mail and Outlook calendar share the `outlook` source, so ids need a namespace to stay unique.

## 2026-10-16: Deterministic pre-score before the LLM
- **Decision:** every item gets a rubric-weighted pre-score from keyword, requester-tier, deadline and CEO-required heuristics. It is sent to Claude as a hint and is the whole score in rules-only mode.
- **Reason:** gives the model a consistent anchor and keeps the assistant usable with no LLM budget left.

## 2026-10-16: Rules-only thresholds
- **Decision:** in rules-only mode a matching hard rule sets the band; otherwise a pre-score at or above `p1_min_score` (65) is P1, below `drop_below` (25) is P3 for meetings and P4 for everything else, and anything between is P2 with the ownership-area delegate match. Confidence is fixed at 0.5.
- **Reason:** simple, explainable bands; the low confidence flags these for review once the budget resets.

## 2026-10-16: Hard rules override the model
- **Decision:** a matching hard rule (for example, anything from a board member) forces its band and clamps the score to its `min_score`/`max_score`, whatever Claude said.
- **Reason:** some priorities are non-negotiable and must not depend on model judgement.

## 2026-10-16: Escalation to Sonnet
- **Decision:** after the Haiku pass, the top 5 items by score plus any item with confidence below 0.6 get a Sonnet second opinion that sees the first pass. If escalation fails, the first pass stands.
- **Reason:** spends the expensive model only where a mistake costs the most or the cheap model is unsure.

## 2026-10-16: Delegate suggestions are validated
- **Decision:** a suggested delegate id not in the contacts list is replaced by the pre-score's ownership-area match for P2 items and cleared otherwise. Only P2 items keep a delegate.
- **Reason:** the model must never route work to someone who isn't a known delegate.

## 2026-10-16: Contacts seeding is insert-only
- **Decision:** at startup, delegates in `contacts.yaml` that are missing from the database are inserted; existing rows are left alone.
- **Reason:** later milestones edit delegates through the API, and a restart must not overwrite those edits.

## 2026-10-16: `POST /sync` scores after ingesting
- **Decision:** the sync endpoint runs ingestion and then scores every open item whose `scored_at` is empty, and reports scored, escalated and rules-only counts.
- **Reason:** one call leaves the item list ready to brief from; the scheduler in Milestone 6 reuses the same path.

## 2026-10-16: Briefs are built from templates, not by the LLM
- **Decision:** morning, evening and alert briefs are composed in code from already-scored items.
- **Alternatives:** asking Sonnet to write each brief.
- **Reason:** free, instant and predictable; the judgement already happened during scoring, and the conversation agent can elaborate when asked.

## 2026-10-16: What counts as today's agenda
- **Decision:** the morning brief covers open meetings starting that day that haven't ended yet, open tasks due by the end of that day (overdue included), undated tasks scored P1, and open email actions.
- **Reason:** undated backlog tasks would otherwise flood every brief; undated P1s still need to be heard.

## 2026-10-16: Brief length
- **Decision:** each brief is trimmed to `briefing.max_spoken_seconds` (default 45) at `words_per_minute` (default 150) by shortening the longest item list first, replacing the tail with "and N more", down to a bare count. Every item stays in `details` for "tell me more".
- **Reason:** keeps the spoken brief inside the spec's 45-second budget without losing anything.

## 2026-10-16: When P1 alerts fire
- **Decision:** after each sync, open P1 items whose fingerprint (title, start, due, band) differs from the one stored when the CEO last heard about them produce one combined alert. Delivering a morning brief marks its P1 items as heard. Meetings already over are skipped.
- **Reason:** "only when a new P1 appears or a P1 changes", without a flood of separate alerts on the first sync or after the morning brief.

## 2026-10-16: Push via an in-process event bus
- **Decision:** delivered briefs are stored in a `briefs` table and published on an `EventBus`. The WebSocket and notifiers subscribe to it in later milestones; `GET /brief/latest` covers clients that were offline.
- **Reason:** keeps the scheduler independent of how briefs reach the CEO.

## 2026-10-16: Scheduler settings
- **Decision:** APScheduler `BackgroundScheduler` in the configured timezone, with coalescing, one instance per job and a 15-minute misfire grace. `SCHEDULER_ENABLED=false` turns it off, and `POST /sync` plus `POST /brief/deliver` let cron or GitHub Actions drive the same jobs. A lock stops a scheduled sync and a manual `/sync` from ingesting at the same time.
- **Reason:** the spec asks for APScheduler plus a documented cron alternative.

## 2026-10-16: Common dialogue turns are handled in code
- **Decision:** "brief me", plain yes/no, "go ahead with all" and "skip the second one" are matched with patterns and handled without a model call; everything else goes to Sonnet with tools.
- **Reason:** they are the most frequent voice turns, they must be instant and exact, and they keep working when the budget cap is hit.

## 2026-10-16: The CEO's own yes is checked in code
- **Decision:** `confirm_pending_action` only runs when the CEO's current utterance contains an affirmation, and never on an action proposed in the same turn. Pending actions exist only through tool calls or brief suggestions.
- **Reason:** defence in depth against prompt injection; a model fooled by email content still cannot execute anything.

## 2026-10-16: Brief suggestions become pending actions
- **Decision:** "brief me" stages the brief's delegations, declines and drops as pending actions, so "yes" right after it executes them; the brief is their read-back. P3 tasks (not meetings) are staged as drops.
- **Reason:** the brief ends with "Shall I go ahead?", which must mean something.

## 2026-10-16: "Go ahead with all" reads back once
- **Decision:** if the conversation already heard the full set of pending actions (a brief or a summary), "go ahead with all" executes them; otherwise it reads back a numbered summary and waits for one yes.
- **Reason:** matches the spec's "after one read-back summary" without repeating it.

## 2026-10-16: Financial requests and delegation
- **Decision:** requests that ask the assistant itself to pay, wire, buy, refund, approve invoices or delete data are refused before any model call (patterns in `approval_policy.yaml`). Asking a delegate to handle such work ("Delegate paying the invoice to Meera") is allowed, because the assistant only sends a brief.
- **Reason:** the never-list is about the assistant moving money or deleting data, while delegating finance work to the finance owner is the core use case.

## 2026-10-16: Name resolution rules
- **Decision:** an exact full name or alias wins outright. Otherwise rapidfuzz WRatio plus a soundex/metaphone match per word scores each delegate; matches at 80 or more within 6 points of the best are ties, broken by the delegate whose ownership areas fit the item, else the CEO is asked. Addresses are accepted only if they belong to a contact.
- **Reason:** handles speech-to-text errors ("Raul" → Rahul) while never guessing between two real people.

## 2026-10-16: Delegated deadlines
- **Decision:** a deadline given as a date means 18:00 that day in the configured timezone; without one, the item's own due date is used.
- **Reason:** "due Monday" means by the end of Monday's working day.

## 2026-10-16: Email acknowledgement tracking
- **Decision:** `EmailAdapter.send` now returns the message and thread ids (Outlook looks the sent message up among recent sent items, since Graph returns no ids). Delegation subjects carry a short `[ref xxxxxxxx]`. Only replies from the delegate's own address count; a reply that isn't a clear yes is passed to the CEO as a question.
- **Reason:** replies are matched by thread, and strangers on the thread can't close a follow-up.

## 2026-10-16: Nudges
- **Decision:** one reminder per delegation, 24 hours before the deadline, on every channel the brief went out on, if nobody acknowledged. The policy key `nudge_delegate` is `auto` by default; set to `confirm` the CEO is told instead, `never` turns it off. Unacknowledged delegations past their deadline become `overdue` and the CEO is told.
- **Reason:** the spec's nudge rule, with the approval policy deciding how automatic it is.

## 2026-10-16: Deleting a delegate keeps history
- **Decision:** `DELETE /delegates/{id}` clears the delegate from suggestions and past decisions, then deletes the row; decisions keep their brief and the audit log keeps the id.
- **Reason:** people leave; their past delegations must not block removing them.

## 2026-10-16: Learning signals
- **Decision:** explicit rules are stored verbatim. Overrides are stored as plain sentences ("The CEO delegated X from P1 to P2") with the requester and direction; when two or more go the same way for one requester, a summary line is added. The block holds at most 20 rules and the 15 latest overrides, and is part of the cached system prompt for scoring and conversation.
- **Reason:** deterministic and cheap, and the model reads plain sentences well.

## 2026-10-16: Deferred items
- **Decision:** deferring sets status `deferred` with a date; the next sync after 08:00 that day reopens it for re-scoring.
- **Reason:** sync never reopens items the CEO decided on, so deferral needs its own way back.

## 2026-10-16: Telegram is the CEO's chat channel
- **Decision:** only `TELEGRAM_CEO_CHAT_ID` can talk to the assistant through the bot; other chats get their chat id on `/start` and are otherwise ignored. A delegate's Accept or Question press counts only from the chat id in their contact entry. Voice notes are transcribed with the same local faster-whisper as `/stt`; the reply comes back as text plus a voice note (Piper → ffmpeg → OGG/Opus) when the CEO spoke.
- **Reason:** the bot is reachable by anyone who finds it, so identity is the chat id, and replying in the CEO's own modality keeps it voice-first.

## 2026-10-16: Telegram long polling, not a webhook
- **Decision:** the bot long-polls `getUpdates` in a daemon thread (`TELEGRAM_POLLING=true`).
- **Reason:** works behind NAT and on a laptop with no public URL; a webhook would need the tunnel to be up.

## 2026-10-16: Teams acknowledgement goes through email
- **Decision:** Teams messages use incoming webhooks (send-only), so the card asks the delegate to reply "Accepted" to the email copy.
- **Reason:** a Teams bot needs Azure Bot registration, which isn't free-tier simple; incoming webhooks are free.

## 2026-10-16: Server-side speech is optional
- **Decision:** faster-whisper and Piper live in the backend's `voice` extra and load lazily; without them `/stt` and `/tts` answer 503 and clients use their own speech engines.
- **Reason:** keeps the core install small and the tests offline, while a phone without speech recognition can still use the backend's.

## 2026-10-16: The backend serves the PWA
- **Decision:** `mobile-pwa/` is mounted at `/` after every API route (`PWA_DIR` overrides the folder). The service worker and API share one origin, so no CORS is needed for the phone app.
- **Reason:** one URL behind the tunnel for both the app and its API.

## 2026-10-16: Web Push keys
- **Decision:** VAPID keys are generated locally (`scripts/generate_vapid_keys.py`, raw base64url keys) and push is sent with pywebpush; a 404 or 410 from the push service deletes the subscription.
- **Reason:** free, standard Web Push with no third-party service.

## 2026-10-16: Rate limits
- **Decision:** an in-process token bucket per client (hashed bearer token, else address): 120 requests a minute with the same burst, and 10 a minute per address on `POST /auth/token`. Behind Cloudflare Tunnel every request comes from 127.0.0.1, so `TRUST_PROXY_HEADERS=true` reads `CF-Connecting-IP`.
- **Reason:** one user with a few clients needs no shared store; the tight auth limit stops guessing the shared secret.

## 2026-10-16: Browser hardening
- **Decision:** every response gets nosniff, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer` and a Permissions-Policy allowing only the microphone; the app gets a CSP (`script-src 'self'`, Google Fonts allowed, `connect-src` open to https/wss so the PWA can use a backend on another origin). `/docs` has no CSP because Swagger UI loads from a CDN. CORS is off unless `CORS_ORIGINS` lists origins.
- **Reason:** the PWA is served from the backend's own origin, so the defaults can be strict.

## 2026-10-16: Paths relative to the repository
- **Decision:** relative paths in `.env` (`CONFIG_PATH`, `CONTACTS_PATH`, `PIPER_VOICES_DIR`, ...) are resolved from the repository root, and `.env` is read from the repository root and then the working directory.
- **Reason:** the backend is started from `backend/`, the docs and `.env.example` talk about `config/...` at the top level, and both must agree.

## 2026-10-16: Your own config files are not committed
- **Decision:** `config/*.yaml` (other than the `*.example.yaml` templates), `.env` and `voices/` are git-ignored; `scripts/dev.sh` creates them on first run.
- **Reason:** contacts, rubric and secrets are personal; the repository only ships templates.

## 2026-10-16: Offline demo
- **Decision:** `scripts/seed_demo_data.py` uses the in-memory adapters and a deterministic stand-in for Claude that bands the demo items by title; it prints what would have been sent.
- **Reason:** the Definition of Done asks for a fully offline demonstration, and a fixed script makes it reproducible and testable.

## 2026-10-16: Docker image
- **Decision:** one image (`backend/Dockerfile`) holds the backend, the phone app and ffmpeg, runs as a non-root user, keeps data in `/data`, and installs the `voice` extra by default (`EXTRAS=dev` for a slim build). Cloudflare's tunnel is an opt-in Compose profile.
- **Reason:** a single free `docker compose up` for people who don't want a Python setup.
