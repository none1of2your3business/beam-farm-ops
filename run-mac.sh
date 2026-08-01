#!/bin/zsh
# Start Beam Farm Ops on this Mac (old web version)
set -e
cd "$(dirname "$0")"
if [ ! -d .venv ]; then
  python3.12 -m venv .venv
  .venv/bin/pip install -r requirements.txt
fi
echo "Starting Beam Farm Ops at http://127.0.0.1:8001"
echo "Login: admin / farm2026  (change this after first login)"
exec .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8001 --reload
