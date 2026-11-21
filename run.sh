#!/usr/bin/env bash
# Start the Frappe/ERPNext Docker Manager web UI.
#   ./run.sh                      -> http://127.0.0.1:8765
#   FDM_HOST=0.0.0.0 FDM_PASSWORD=secret ./run.sh   (remote access: ALWAYS set a password)
set -euo pipefail
cd "$(dirname "$0")"
HOST="${FDM_HOST:-127.0.0.1}"
PORT="${FDM_PORT:-8765}"
PY="${PYTHON:-python3}"

if [[ "$HOST" != "127.0.0.1" && "$HOST" != "localhost" && -z "${FDM_PASSWORD:-}" ]]; then
  echo "Refusing to listen on $HOST without FDM_PASSWORD - this UI controls Docker (root-equivalent)." >&2
  exit 1
fi
if ! command -v docker >/dev/null; then echo "docker CLI not found" >&2; exit 1; fi

if [[ ! -x .venv/bin/python ]]; then
  echo "Creating virtualenv..."
  "$PY" -m venv .venv
fi
if [[ ! -f .venv/.deps-ok || requirements.txt -nt .venv/.deps-ok ]]; then
  .venv/bin/pip install --disable-pip-version-check -q -r requirements.txt
  touch .venv/.deps-ok
fi
echo "Frappe Docker Manager UI → http://${HOST}:${PORT}"
exec .venv/bin/uvicorn app.main:app --host "$HOST" --port "$PORT" ${FDM_RELOAD:+--reload}
