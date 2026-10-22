"""First-run setup shared by the Windows launcher: config files and a fresh .env.

    python scripts/setup_local.py            # create what's missing, print the sign-in secret
    python scripts/setup_local.py --secret   # just print the sign-in secret

Copies each ``config/*.example.yaml`` you don't have yet, and creates ``.env`` from
``.env.example`` with a new ``API_AUTH_TOKEN`` and ``FERNET_KEY``. Nothing that already
exists is changed. Uses only the standard library plus ``cryptography`` (installed with
the backend).
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


def setup() -> bool:
    """Returns True when .env was created on this run."""
    for name in CONFIGS:
        target = ROOT / "config" / f"{name}.yaml"
        if not target.exists():
            shutil.copyfile(ROOT / "config" / f"{name}.example.yaml", target)
            print(f"Created config/{name}.yaml from the example.")

    env = ROOT / ".env"
    if env.exists():
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
