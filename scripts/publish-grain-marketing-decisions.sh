#!/usr/bin/env bash
# Republish Hold-or-Sell.html to the permanent aired.sh URL (same page id).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SRC="$ROOT/Hold-or-Sell.html"
PAGE_ID="${AIRED_PAGE_ID:-MBccqyEYV0}"
TOKEN_FILE="$ROOT/.aired-update-token"
TOKEN="${AIRED_UPDATE_TOKEN:-}"
if [[ -z "$TOKEN" && -f "$TOKEN_FILE" ]]; then
  TOKEN="$(tr -d '[:space:]' < "$TOKEN_FILE")"
fi
if [[ -z "$TOKEN" ]]; then
  echo "Missing AIRED_UPDATE_TOKEN or $TOKEN_FILE" >&2
  exit 1
fi
python3 - "$SRC" "$PAGE_ID" "$TOKEN" <<'PY'
import json, sys, urllib.request
src, page_id, token = sys.argv[1:4]
html = open(src, encoding="utf-8").read()
body = json.dumps({
    "html": html,
    "id": page_id,
    "update_token": token,
    "title": "Grain Marketing Decisions",
    "permanent": True,
}).encode()
req = urllib.request.Request(
    "https://aired.sh/api/publish",
    data=body,
    method="POST",
    headers={"Content-Type": "application/json", "User-Agent": "beam-farm-ops-publish"},
)
with urllib.request.urlopen(req, timeout=120) as r:
    print(r.read().decode())
PY
echo "Updated https://aired.sh/p/${PAGE_ID}"
