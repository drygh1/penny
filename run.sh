#!/usr/bin/env bash
# Start Penny at http://127.0.0.1:8000
# Binds to localhost only, so nothing else on your network can see your data.
set -e
cd "$(dirname "$0")"
if [ ! -d .venv ]; then
  uv venv .venv --python 3.13
  uv pip install --python .venv -r requirements.txt
fi
(sleep 1.5 && open http://127.0.0.1:8000) &
exec .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000 "$@"
