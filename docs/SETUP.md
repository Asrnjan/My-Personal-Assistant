# Setup

Everything here is free. Put each value in `.env` (copy it from `.env.example`). The only paid service is the Claude API (`ANTHROPIC_API_KEY`).

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

## Checking what is connected

On startup the backend logs a line for every enabled integration that is missing credentials, naming exactly what to set.
