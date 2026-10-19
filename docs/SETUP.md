# Setup

Everything here is free. Put each value in `.env` (copy it from `.env.example`, or let `scripts/dev.sh` create it). The only paid service is the Claude API (`ANTHROPIC_API_KEY`).

You don't need all of it. The smallest useful setup is: core secrets (1), the Claude API key, one calendar/email account (2 or 3), your contacts (7) and one way to talk to the assistant: the phone app (12) or Telegram (8). Add the rest when you want it.

## 0. Quick start

```bash
git clone https://github.com/Asrnjan/My-Personal-Assistant.git
cd My-Personal-Assistant
scripts/dev.sh
```

The first run creates `backend/.venv`, copies the example files in `config/` to your own `config/*.yaml`, creates `.env` with a fresh `API_AUTH_TOKEN` and `FERNET_KEY`, and prints the sign-in secret. Open http://127.0.0.1:8000/ for the phone app (it works in a desktop browser too) and http://127.0.0.1:8000/docs for the API.

To see the whole flow without any accounts or API key: `backend/.venv/bin/python scripts/seed_demo_data.py`.

## Claude API key

1. Sign in at https://console.anthropic.com/ and add a payment method (this is the one paid service).
2. **API keys → Create key**. Put it in `ANTHROPIC_API_KEY`.
3. Set a monthly cap in `config/config.yaml` under `budget.monthly_usd` (default 20). Above it, scoring falls back to rules and you get a notice.

## 1. Core secrets

```bash
# Encryption key for OAuth tokens stored in the database
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
# -> FERNET_KEY=...

# Shared secret your devices exchange for their own login token
python -c "import secrets; print(secrets.token_urlsafe(32))"
# -> API_AUTH_TOKEN=...
```

Set `PUBLIC_BASE_URL` to the address the backend is reachable at (for example your Cloudflare Tunnel URL). OAuth providers redirect to `<PUBLIC_BASE_URL>/oauth/<provider>/callback`.

## 2. Google Calendar and Gmail

1. Open https://console.cloud.google.com/ and create a project.
2. **APIs & Services → Library**: enable **Google Calendar API** and **Gmail API**.
3. **APIs & Services → OAuth consent screen**: choose **External**, fill in the app name and your email, and add yourself under **Test users**.
4. **APIs & Services → Credentials → Create credentials → OAuth client ID**: type **Web application**. Under **Authorized redirect URIs** add `<PUBLIC_BASE_URL>/oauth/google/callback`.
5. Copy the client ID and secret into `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET`.
6. In `config/config.yaml`, include `google` under `adapters.calendar` and `adapters.email`.
7. Start the backend, then connect the account: call `GET /oauth/google/start` with your bearer token and open the returned `authorization_url` in a browser. Approve the requested access; the page says "Connected" when done.

While the consent screen is in "Testing" mode Google expires refresh tokens after 7 days; publish the app (no review is needed for personal use with your own account) to keep the connection.

## 3. Microsoft 365 (Outlook calendar and mail)

1. Open https://entra.microsoft.com → **App registrations → New registration**.
2. Name it, choose **Accounts in this organizational directory only** (or any directory), and add a **Web** redirect URI `<PUBLIC_BASE_URL>/oauth/microsoft/callback`.
3. Copy **Application (client) ID** into `MS_CLIENT_ID` and **Directory (tenant) ID** into `MS_TENANT_ID` (or leave `common`).
4. **Certificates & secrets → New client secret**: copy the value into `MS_CLIENT_SECRET`.
5. **API permissions → Add → Microsoft Graph → Delegated**: `offline_access`, `User.Read`, `Calendars.ReadWrite`, `Mail.ReadWrite`, `Mail.Send`. Grant admin consent if your tenant requires it.
6. In `config/config.yaml`, include `microsoft` under `adapters.calendar` and/or `adapters.email`.
7. Connect the account with `GET /oauth/microsoft/start`, as for Google.

## 4. Jira Cloud

1. Create an API token at https://id.atlassian.com/manage-profile/security/api-tokens.
2. Set `JIRA_BASE_URL` (e.g. `https://yourcompany.atlassian.net`), `JIRA_EMAIL` (your Atlassian login) and `JIRA_API_TOKEN`.
3. Each delegate's Jira **account id** goes in `contacts.yaml` as `jira_account_id`. Find it by opening the person's profile in Jira; the id is the last part of the URL.

## 5. Asana

1. Create a personal access token: Asana → profile photo → **Settings → Apps → Developer apps → Create new token**.
2. Set `ASANA_ACCESS_TOKEN`.
3. Find your workspace gid at https://app.asana.com/api/1.0/workspaces (open it while logged in) and set `ASANA_WORKSPACE_GID`.
4. Each delegate's Asana user gid goes in `contacts.yaml` as `asana_gid` (listed at https://app.asana.com/api/1.0/users?workspace=YOUR_WORKSPACE_GID).

## 6. ClickUp

1. ClickUp → avatar → **Settings → Apps → API Token → Generate**. Set `CLICKUP_API_TOKEN`.
2. Your team (workspace) id is the number after `app.clickup.com/` in the URL; set `CLICKUP_TEAM_ID`.
3. Your user id: open https://api.clickup.com/api/v2/user with the token as the `Authorization` header (or ask the assistant's logs) and set `CLICKUP_USER_ID`.
4. If your space calls the final status something other than `complete`, set `CLICKUP_DONE_STATUS`.
5. Each delegate's ClickUp user id goes in `contacts.yaml` as `clickup_id`.

## 7. Your contacts, rubric and policy

- `config/contacts.yaml`: the people you delegate to. For each: `id`, `full_name`, `aliases` (what you call them out loud), `email`, `preferred_channels` (any of `email`, `telegram`, `slack`, `teams`, in order), `ownership_areas` (used to suggest who gets what and to tell two "Rahul"s apart), and their ids in your tools: `telegram_chat_id`, `slack_user_id`, `teams_webhook`, `jira_account_id`, `asana_gid`, `clickup_id`. Keep `email` in everyone's channels: replies to it are how Teams delegations get acknowledged.
- `config/rubric.yaml`: your company goals, requester tiers (board, investors, key customers), weights and hard rules. The assistant reads this every time it scores.
- `config/approval_policy.yaml`: what may happen automatically, what needs your yes, and what is never done (payments, purchases, deleting data). The defaults ask before every delegation, decline or close.
- `config/config.yaml`: timezone, brief times, which integrations are on, models, budget, wake word and voices.

Restart the backend after editing them. On startup everyone in `contacts.yaml` is added or updated to match the file; people you added only through `POST /delegates` are kept.

## 8. Telegram (your chat with the assistant, and delegate notifications)

1. In Telegram, open **@BotFather**, send `/newbot`, pick a name and a username ending in `bot`. BotFather replies with a token: put it in `TELEGRAM_BOT_TOKEN`.
2. Start the backend, open your new bot in Telegram and press **Start**. It answers with your chat id: put it in `TELEGRAM_CEO_CHAT_ID` and restart.
3. Make sure `telegram` is in `adapters.notify` in `config/config.yaml`.
4. Each delegate who should get Telegram messages opens the bot, presses **Start**, and sends you the chat id it shows them. Put it in their `telegram_chat_id` in `contacts.yaml`. (Telegram bots can't message someone who hasn't pressed Start.)

Now you can type or send voice notes to the bot. Voice notes need the backend's speech extra (section 11); replies to a voice note come back as text plus a voice note (needs `ffmpeg`, e.g. `sudo apt install ffmpeg` or `brew install ffmpeg`). Only your chat id can give the assistant instructions; anyone else only gets their chat id back.

The bot uses long polling, so it works without a public URL. If two copies of the backend run with the same token, set `TELEGRAM_POLLING=false` on one of them.

## 9. Slack

1. Go to https://api.slack.com/apps → **Create New App → From scratch**, pick your workspace.
2. **OAuth & Permissions → Bot Token Scopes**: add `chat:write`, `im:write`, `im:history` and `reactions:read`.
3. **Install to Workspace**, then copy the **Bot User OAuth Token** (`xoxb-...`) into `SLACK_BOT_TOKEN`.
4. Your own member id (profile → ⋮ → **Copy member ID**) goes in `SLACK_CEO_USER_ID` if you want notices in Slack.
5. Each delegate's member id goes in `slack_user_id` in `contacts.yaml`, and add `slack` to `adapters.notify`.

Delegates accept by replying "accepted", "ok" or "on it" in the DM, or by reacting with ✅ or 👍. Anything else they write reaches you as a question.

## 10. Microsoft Teams

Teams uses incoming webhooks, which are free and need no app registration.

1. In the Teams channel you want, choose **⋯ → Workflows → Post to a channel when a webhook request is received** (or, on older tenants, **Connectors → Incoming Webhook**). Copy the URL.
2. For notices to you, put a channel's URL in `TEAMS_WEBHOOK_URL`. For a delegate, put the URL of a channel they watch in their `teams_webhook`.
3. Add `teams` to `adapters.notify`.

Webhooks are send-only, so a delegate accepts by replying "Accepted" to the email copy of the delegation.

## 11. Voices (server-side speech)

The phone app uses the phone's own speech engines when it can. The backend can also listen and speak, for phones without speech recognition and for Telegram voice notes:

```bash
cd backend
.venv/bin/pip install -e ".[voice]"                 # faster-whisper + Piper
mkdir -p ../voices
.venv/bin/python -m piper.download_voices --download-dir ../voices en_US-lessac-medium
```

`PIPER_VOICES_DIR` points at that folder (default `voices/` in the repository). Pick another voice from https://huggingface.co/rhasspy/piper-voices and set `tts.voice` in `config/config.yaml`. For Kokoro instead, `pip install -e ".[kokoro]"` and set `tts.engine: kokoro`, `tts.voice: af_heart`. The Whisper model (`stt.model_size`, default `small`) downloads on first use.

## 12. The phone app (Android and iOS)

1. Make the backend reachable from your phone over HTTPS: see [DEPLOY.md](DEPLOY.md) (Cloudflare Tunnel is free and takes about ten minutes).
2. **Android (Chrome):** open your assistant's URL, tap **⋮ → Add to Home screen → Install**.
3. **iPhone (iOS 16.4 or later, Safari):** open the URL, tap **Share → Add to Home Screen**, then open the app from the home screen icon. Notifications only work from the installed icon, not from a Safari tab.
   On iPhone, tapping the orb records your voice and the backend transcribes it with faster-whisper (Safari's own speech recognition hangs in home-screen apps). It stops by itself when you go quiet, or tap the orb again. The first time, the backend downloads the speech model, so the first answer can take a minute.
4. Open the app, tap the gear, and enter your `API_AUTH_TOKEN` as the shared secret. Leave **API base URL** empty: the backend serves the app, so they share one address.
5. Tap **Enable push notifications** in Settings to get the morning brief, evening wrap-up and P1 alerts as notifications. This needs Web Push keys on the backend:

```bash
backend/.venv/bin/python scripts/generate_vapid_keys.py
```

Paste the two lines into `.env`, set `VAPID_SUBJECT=mailto:<your email>`, and restart.

To start the brief by voice ("Hey Siri, daily brief"), see [VOICE_SHORTCUTS.md](VOICE_SHORTCUTS.md).

## 13. The desktop app (Windows, macOS, Linux)

```bash
pipx install "./desktop[voice,tray]"     # or: uv tool install "./desktop[voice,tray]"
```

Linux needs PortAudio first (`sudo apt install libportaudio2`). Copy `desktop/desktop.example.toml` to `~/.config/ceo-assistant/desktop.toml` and set `backend.url` and `backend.secret` (your `API_AUTH_TOKEN`). Download a Piper voice as in section 11, then run `assistant-desktop --check` and `assistant-desktop`. Say "hey jarvis" or press Ctrl+Alt+Space. Full details: [desktop/README.md](../desktop/README.md).

## Checking what is connected

On startup the backend logs a line for every enabled integration that is missing credentials, naming exactly what to set.
