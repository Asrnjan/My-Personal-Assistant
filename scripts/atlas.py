"""The ``atlas`` command: set up and use Atlas from a command prompt.

On Windows run it as ``atlas ...`` from the main folder (``atlas.cmd`` calls this with the
backend's Python). Everything except ``start`` and ``secret`` talks to the running Atlas at
http://127.0.0.1:8000 (or ``ATLAS_URL``), signing in with the secret in ``.env``.
"""

from __future__ import annotations

import contextlib
import os
import re
import sys
import webbrowser
from pathlib import Path
from typing import Any, NoReturn

ROOT = Path(__file__).resolve().parent.parent
ENV = ROOT / ".env"
CONTACTS = ROOT / "config" / "contacts.yaml"
BASE_URL = os.environ.get("ATLAS_URL", "http://127.0.0.1:8000").rstrip("/")
ACCOUNTS = {"outlook": "microsoft", "teams": "microsoft", "google": "google"}

HELP = """\
Atlas from the command prompt. Start Atlas in one window, run these in another.

  atlas start                         start Atlas (keep that window open)
  atlas status                        what is connected
  atlas secret                        show the sign-in secret

  atlas connect claude KEY            add Claude (key starts with sk-ant-)
  atlas connect telegram BOT_TOKEN    then press Start in your bot and run it again
  atlas connect slack BOT_TOKEN
  atlas connect jira ADDRESS EMAIL TOKEN
  atlas connect asana TOKEN
  atlas connect clickup TOKEN
  atlas connect teams-webhook URL
  atlas disconnect APP

  atlas keys microsoft                show the steps and the redirect address
  atlas keys microsoft CLIENT_ID CLIENT_SECRET [TENANT_ID]
  atlas keys google CLIENT_ID CLIENT_SECRET
  atlas signin outlook|google|teams   open the sign-in page in your browser

  atlas team                          list your team
  atlas team add ID "Full Name" EMAIL "area, area"
  atlas team remove ID
  atlas team clear                    remove everyone (start with the example people gone)

  atlas say "brief me"                talk to Atlas
  atlas phone                         give Atlas a phone address with Tailscale
"""

KEY_STEPS = {
    "microsoft": (
        "Open https://entra.microsoft.com and choose App registrations, New registration.",
        "Name: Atlas. Account types: Accounts in any organizational directory and personal"
        " Microsoft accounts.",
        "Redirect URI: choose Web and paste the address below. Click Register.",
        "Copy the Application (client) ID.",
        "Certificates & secrets, New client secret, 24 months, Add. Copy the Value.",
        "Then run: atlas keys microsoft CLIENT_ID CLIENT_SECRET",
    ),
    "google": (
        "Open https://console.cloud.google.com, create a project called Atlas.",
        "Enable the Gmail API and the Google Calendar API.",
        "OAuth consent screen: External, add yourself as a test user.",
        "Clients (or Credentials), Create, Web application.",
        "Add the redirect address below under Authorized redirect URIs. Click Create.",
        "Then run: atlas keys google CLIENT_ID CLIENT_SECRET",
    ),
}


def fail(message: str) -> NoReturn:
    print(message, file=sys.stderr)
    raise SystemExit(1)


def read_secret() -> str:
    if not ENV.exists():
        fail("Atlas isn't set up yet. Run: atlas start")
    match = re.search(r"^API_AUTH_TOKEN=(.*)$", ENV.read_text(encoding="utf-8"), flags=re.M)
    secret = match.group(1).strip() if match else ""
    if not secret:
        fail("No sign-in secret in .env. Run: atlas start")
    return secret


class Atlas:
    """A signed-in connection to the running Atlas.

    The login token is kept next to the backend's Python and reused, because Atlas allows
    only a few sign-ins a minute from one address (against guessing the secret)."""

    TOKEN_FILE = ROOT / "backend" / ".venv" / "atlas-cli-token.txt"

    def __init__(self) -> None:
        import httpx

        self._httpx = httpx
        self.http = httpx.Client(base_url=BASE_URL, timeout=60)
        token = ""
        with contextlib.suppress(OSError):
            token = self.TOKEN_FILE.read_text(encoding="ascii").strip()
        if token:
            self.http.headers["Authorization"] = f"Bearer {token}"
        else:
            self._sign_in()

    def _sign_in(self) -> None:
        try:
            r = self.http.post(
                "/auth/token", json={"secret": read_secret(), "client_name": "atlas-cli"}
            )
        except self._httpx.HTTPError:
            fail(
                f"Atlas isn't running at {BASE_URL}.\nStart it in another window first: atlas start"
            )
        if r.status_code == 429:
            fail("Too many sign-ins in a minute. Wait a minute and try again.")
        if r.status_code != 200:
            fail(f"Atlas didn't accept the sign-in secret ({r.status_code}).")
        token = r.json()["access_token"]
        self.http.headers["Authorization"] = f"Bearer {token}"
        # Without the cache it still works; it just signs in every time.
        with contextlib.suppress(OSError):
            self.TOKEN_FILE.write_text(token, encoding="ascii")

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            return self.http.request(method, path, **kwargs)
        except self._httpx.HTTPError:
            fail(
                f"Atlas isn't running at {BASE_URL}.\nStart it in another window first: atlas start"
            )

    def call(self, method: str, path: str, **kwargs: Any) -> Any:
        r = self._request(method, path, **kwargs)
        if r.status_code == 401:
            # The saved login expired or Atlas was set up again: sign in once more.
            self._sign_in()
            r = self._request(method, path, **kwargs)
        if r.status_code >= 400:
            try:
                detail = r.json().get("detail", r.text)
            except ValueError:
                detail = r.text
            fail(str(detail))
        return r.json() if r.content else None


def mark(ok: bool) -> str:
    return "[x]" if ok else "[ ]"


# ---------------------------------------------------------------- commands


def cmd_secret(_args: list[str]) -> None:
    print(read_secret())


def cmd_status(_args: list[str]) -> None:
    atlas = Atlas()
    accounts = atlas.call("GET", "/accounts")
    apps = atlas.call("GET", "/connections")
    print("Email, calendar and Teams")
    for a in accounts["accounts"]:
        note = "" if a["connected"] or a["ready"] else "  (needs: atlas keys " + a["provider"] + ")"
        print(f"  {mark(a['connected'])} {a['name']}{note}")
    if not apps.get("ready"):
        print("Apps can't be connected yet: FERNET_KEY is missing in .env.")
        return
    groups = {"brain": "Brain", "tasks": "Task boards", "chat": "Chat and notifications"}
    for group, title in groups.items():
        print(title)
        for app in apps["apps"]:
            if app["group"] == group:
                print(f"  {mark(app['connected'])} {app['name']:<14} atlas connect {app['id']}")


def cmd_connect(args: list[str]) -> None:
    if not args:
        fail("Which app? For example: atlas connect claude sk-ant-...")
    app_id, values = args[0].lower(), args[1:]
    atlas = Atlas()
    apps = {a["id"]: a for a in atlas.call("GET", "/connections")["apps"]}
    app = apps.get(app_id)
    if app is None:
        fail(f"Unknown app '{app_id}'. Choose one of: {', '.join(apps)}")
    fields = app["fields"]
    if not values or len(values) > len(fields):
        usage = " ".join(f["label"].upper().replace(" ", "_") for f in fields)
        steps = "\n".join(f"  {i}. {s}" for i, s in enumerate(app["steps"], 1))
        link = f"\n  Link: {app['link']}" if app["link"] else ""
        fail(
            f"{app['name']}: {app['description']}\n{steps}{link}\n\n"
            f"Usage: atlas connect {app_id} {usage}"
        )
    body = {"values": {f["key"]: v for f, v in zip(fields, values, strict=False)}}
    print(f"Checking with {app['name']}...")
    result = atlas.call("PUT", f"/connections/{app_id}", json=body)
    print(result["message"])


def cmd_disconnect(args: list[str]) -> None:
    if not args:
        fail("Which app? For example: atlas disconnect jira")
    Atlas().call("DELETE", f"/connections/{args[0].lower()}")
    print(f"{args[0]} disconnected.")


def cmd_keys(args: list[str]) -> None:
    if not args or args[0].lower() not in KEY_STEPS:
        fail("Usage: atlas keys microsoft|google [CLIENT_ID CLIENT_SECRET [TENANT_ID]]")
    provider, rest = args[0].lower(), args[1:]
    atlas = Atlas()
    info = next(
        (p for p in atlas.call("GET", "/accounts")["providers"] if p["id"] == provider), None
    )
    if info is None:
        fail("Accounts can't be connected yet: FERNET_KEY is missing in .env.")
    if len(rest) < 2:
        print("\n".join(f"  {i}. {s}" for i, s in enumerate(KEY_STEPS[provider], 1)))
        print(f"\nRedirect address: {info['redirect_uri']}")
        return
    body: dict[str, Any] = {"client_id": rest[0], "client_secret": rest[1]}
    if provider == "microsoft" and len(rest) > 2:
        body["tenant_id"] = rest[2]
    atlas.call("PUT", f"/accounts/apps/{provider}", json=body)
    first = "outlook" if provider == "microsoft" else "google"
    print(f"Keys saved. Now run: atlas signin {first}")


def cmd_signin(args: list[str]) -> None:
    account = args[0].lower() if args else ""
    if account not in ACCOUNTS:
        fail("Usage: atlas signin outlook|google|teams")
    url = Atlas().call("POST", f"/accounts/{account}/connect")["authorization_url"]
    print("Opening the sign-in page. Sign in, click Accept, then come back here.")
    print(url)
    webbrowser.open(url)


def _load_contacts() -> dict[str, Any]:
    import yaml

    if not CONTACTS.exists():
        return {"delegates": []}
    data = yaml.safe_load(CONTACTS.read_text(encoding="utf-8")) or {}
    data.setdefault("delegates", [])
    return data


def _save_contacts(data: dict[str, Any]) -> None:
    import yaml

    header = "# Your team. Edited by `atlas team`; you can also edit it in Notepad.\n"
    CONTACTS.write_text(
        header + yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )


def cmd_team(args: list[str]) -> None:
    action = args[0].lower() if args else "list"
    if action == "list":
        people = _load_contacts()["delegates"]
        if not people:
            print(
                "No one yet. Add someone: "
                'atlas team add priya "Priya Sharma" priya@co.com "finance"'
            )
        for p in people:
            areas = ", ".join(p.get("ownership_areas") or [])
            print(f"  {p['id']:<12} {p['full_name']:<22} {p['email']:<30} {areas}")
        return
    atlas = Atlas()
    data = _load_contacts()
    if action == "add":
        if len(args) < 4:
            fail('Usage: atlas team add ID "Full Name" EMAIL ["area, area"]')
        pid, name, email = args[1].lower(), args[2], args[3]
        areas = [a.strip() for a in (args[4] if len(args) > 4 else "").split(",") if a.strip()]
        person = {
            "id": pid,
            "full_name": name,
            "aliases": [name.split()[0]],
            "email": email,
            "preferred_channels": ["email"],
            "ownership_areas": areas,
        }
        exists = any(p["id"] == pid for p in data["delegates"])
        if exists:
            atlas.call(
                "PUT", f"/delegates/{pid}", json={k: v for k, v in person.items() if k != "id"}
            )
            data["delegates"] = [person if p["id"] == pid else p for p in data["delegates"]]
        else:
            atlas.call("POST", "/delegates", json=person)
            data["delegates"].append(person)
        _save_contacts(data)
        print(f"{'Updated' if exists else 'Added'} {name}.")
    elif action in ("remove", "clear"):
        ids = (
            [p["id"] for p in data["delegates"]]
            if action == "clear"
            else [a.lower() for a in args[1:]]
        )
        if not ids:
            fail("Usage: atlas team remove ID")
        current = {p["id"] for p in atlas.call("GET", "/delegates")}
        for pid in ids:
            if pid in current:
                atlas.call("DELETE", f"/delegates/{pid}")
        data["delegates"] = [p for p in data["delegates"] if p["id"] not in ids]
        _save_contacts(data)
        print(f"Removed {', '.join(ids)}.")
    else:
        fail("Usage: atlas team [list | add | remove | clear]")


def cmd_say(args: list[str]) -> None:
    text = " ".join(args).strip()
    if not text:
        fail('Usage: atlas say "brief me"')
    reply = Atlas().call("POST", "/converse", json={"text": text})
    print(reply.get("reply") or "(no reply)")
    for action in reply.get("pending_actions") or []:
        if action.get("status") == "pending":
            print(f"  Waiting for your OK: {action.get('readback') or action.get('title')}")
    if reply.get("needs_confirmation"):
        print('  Say yes with: atlas say "yes"')


def cmd_phone(_args: list[str]) -> None:
    import shutil
    import subprocess

    exe = shutil.which("tailscale") or r"C:\Program Files\Tailscale\tailscale.exe"
    if not Path(exe).exists() and not shutil.which("tailscale"):
        fail(
            "Tailscale isn't installed. Install it with:\n"
            "  winget install --exact --id Tailscale.Tailscale\n"
            "then sign in from the Tailscale icon near the clock, and run: atlas phone"
        )
    port = BASE_URL.rsplit(":", 1)[-1]
    # Fixed arguments; the only input is the port from ATLAS_URL.
    raise SystemExit(subprocess.call([exe, "funnel", "--bg", port]))  # noqa: S603


COMMANDS = {
    "secret": cmd_secret,
    "status": cmd_status,
    "connect": cmd_connect,
    "disconnect": cmd_disconnect,
    "keys": cmd_keys,
    "signin": cmd_signin,
    "team": cmd_team,
    "say": cmd_say,
    "phone": cmd_phone,
}


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("help", "-h", "--help", "/?"):
        print(HELP)
        return 0
    command = COMMANDS.get(argv[0].lower())
    if command is None:
        print(f"Unknown command '{argv[0]}'.\n", file=sys.stderr)
        print(HELP)
        return 1
    command(argv[1:])
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
