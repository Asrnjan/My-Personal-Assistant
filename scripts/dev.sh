#!/usr/bin/env bash
# Start the backend locally; it also serves the phone app at http://127.0.0.1:8000/.
#
#   scripts/dev.sh            # first run sets everything up, later runs just start
#   PORT=9000 scripts/dev.sh
#   HOST=0.0.0.0 scripts/dev.sh   # reachable from your phone on the same Wi-Fi
#
# On the first run it creates backend/.venv, copies the example config files you
# don't have yet, and creates .env with a fresh API_AUTH_TOKEN and FERNET_KEY.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8000}"
PYTHON="${PYTHON:-python3}"
cd "$ROOT"

if [ ! -x backend/.venv/bin/python ]; then
  echo "Creating backend/.venv ..."
  "$PYTHON" -m venv backend/.venv
  backend/.venv/bin/pip install --quiet --upgrade pip
  backend/.venv/bin/pip install --quiet -e "backend[dev]"
fi
PY="$ROOT/backend/.venv/bin/python"

for name in config rubric contacts approval_policy; do
  if [ ! -f "config/$name.yaml" ]; then
    cp "config/$name.example.yaml" "config/$name.yaml"
    echo "Created config/$name.yaml from the example. Edit it to fit you."
  fi
done

if [ ! -f .env ]; then
  cp .env.example .env
  TOKEN="$("$PY" -c 'import secrets; print(secrets.token_urlsafe(32))')"
  FERNET="$("$PY" -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')"
  "$PY" - "$TOKEN" "$FERNET" <<'PYEOF'
import re
import sys
from pathlib import Path

token, fernet = sys.argv[1], sys.argv[2]
env = Path(".env")
text = env.read_text()
text = re.sub(r"^API_AUTH_TOKEN=.*$", f"API_AUTH_TOKEN={token}", text, flags=re.M)
text = re.sub(r"^FERNET_KEY=.*$", f"FERNET_KEY={fernet}", text, flags=re.M)
env.write_text(text)
PYEOF
  chmod 600 .env
  echo
  echo "Created .env. Your sign-in secret for the apps (also in .env as API_AUTH_TOKEN):"
  echo "  $TOKEN"
  echo "Then open the app, sign in with that secret, and connect Claude and your accounts in the Apps tab."
  echo
fi

cd backend
"$PY" -m alembic upgrade head
echo "Backend and phone app: http://$HOST:$PORT/   API docs: http://$HOST:$PORT/docs"
exec "$PY" -m uvicorn app.main:create_app --factory --host "$HOST" --port "$PORT" --reload \
  --reload-dir app
