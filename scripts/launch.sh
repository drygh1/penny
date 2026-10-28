#!/bin/bash
# Start Penny in the background if it isn't running, then open it in the browser.
# The Penny.app bundle (made by scripts/make_app.sh) runs this when you click it.
cd "$(dirname "$0")/.." || exit 1
PORT="${PENNY_PORT:-8000}"
URL="http://127.0.0.1:$PORT"

if ! curl -sf "$URL/api/status" >/dev/null; then
  if [ ! -x .venv/bin/uvicorn ]; then
    osascript -e 'display alert "Penny isn'"'"'t installed yet" message "Run ./run.sh once in Terminal to finish setup."'
    exit 1
  fi
  mkdir -p data
  nohup .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port "$PORT" >> data/penny.log 2>&1 &
  # Wait up to 10 seconds for the server to come up.
  for _ in $(seq 1 40); do
    curl -sf "$URL/api/status" >/dev/null && break
    sleep 0.25
  done
fi
open "$URL"
