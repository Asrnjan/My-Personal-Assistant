"""First-run setup shared by the Windows launcher: config files and a fresh .env.

    python scripts/setup_local.py            # create what's missing, print the sign-in secret
    python scripts/setup_local.py --secret   # just print the sign-in secret

Copies each ``config/*.example.yaml`` you don't have yet, creates ``.env`` from
``.env.example`` with a new ``API_AUTH_TOKEN`` and ``FERNET_KEY``, and fills in a Web Push
key pair (``VAPID_*``) when ``.env`` has none, so phone notifications work without a manual
step. Nothing that already has a value is changed, except that an old ``config.yaml``
still carrying the earlier shipped default ``conversation_effort: medium`` is moved to the
faster ``low``. Uses only the standard library plus
``cryptography`` (installed with the backend).
"""

from __future__ import annotations

import re
import secrets
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIGS = ("config", "rubric", "contacts", "approval_policy")


def read_secret(env: Path) -> str:
    if not env.exists():
        return ""
    match = re.search(r"^API_AUTH_TOKEN=(.*)$", env.read_text(encoding="utf-8"), flags=re.M)
    return match.group(1).strip() if match else ""


def ensure_vapid(env: Path) -> bool:
    """Add a Web Push key pair to .env when it has none. Returns True when keys were added."""
    text = env.read_text(encoding="utf-8")
    current = re.search(r"^VAPID_PRIVATE_KEY=(.*)$", text, flags=re.M)
    if current and current.group(1).strip():
        return False
    sys.path.insert(0, str(ROOT / "scripts"))
    from generate_vapid_keys import generate

    public, private = generate()
    for name, value in (("VAPID_PUBLIC_KEY", public), ("VAPID_PRIVATE_KEY", private)):
        line = f"{name}={value}"
        if re.search(rf"^{name}=.*$", text, flags=re.M):
            text = re.sub(rf"^{name}=.*$", line, text, flags=re.M)
        else:
            text = text.rstrip("\n") + f"\n{line}\n"
    env.write_text(text, encoding="utf-8", newline="\n")
    print("Added Web Push keys to .env for phone notifications.")
    return True


# The first versions shipped "medium", which made spoken replies noticeably slow.
_OLD_EFFORT = re.compile(r"^([ \t]*conversation_effort:[ \t]*)medium(?=[ \t\r]*$)", flags=re.M)


def speed_up_config(config: Path) -> bool:
    """Move the old default conversation effort to "low". Returns True when changed."""
    if not config.exists():
        return False
    data = config.read_bytes().decode("utf-8")
    if not _OLD_EFFORT.search(data):
        return False
    # Bytes in and out, so the file keeps its own line endings.
    config.write_bytes(_OLD_EFFORT.sub(r"\g<1>low", data, count=1).encode("utf-8"))
    print("Switched config/config.yaml to quicker replies (conversation_effort: low).")
    return True


def setup() -> bool:
    """Returns True when .env was created on this run."""
    for name in CONFIGS:
        target = ROOT / "config" / f"{name}.yaml"
        if not target.exists():
            shutil.copyfile(ROOT / "config" / f"{name}.example.yaml", target)
            print(f"Created config/{name}.yaml from the example.")
    speed_up_config(ROOT / "config" / "config.yaml")

    env = ROOT / ".env"
    if env.exists():
        ensure_vapid(env)
        return False
    from cryptography.fernet import Fernet

    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    text = re.sub(
        r"^API_AUTH_TOKEN=.*$", f"API_AUTH_TOKEN={secrets.token_urlsafe(32)}", text, flags=re.M
    )
    text = re.sub(
        r"^FERNET_KEY=.*$", f"FERNET_KEY={Fernet.generate_key().decode()}", text, flags=re.M
    )
    # Written as plain UTF-8 without a byte-order mark, whatever the platform.
    env.write_text(text, encoding="utf-8", newline="\n")
    print("Created .env with a new sign-in secret and encryption key.")
    ensure_vapid(env)
    return True


def main(argv: list[str]) -> int:
    if "--secret" not in argv:
        setup()
    secret = read_secret(ROOT / ".env")
    if not secret:
        print("No sign-in secret found in .env (API_AUTH_TOKEN is empty).", file=sys.stderr)
        return 1
    print(secret)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
