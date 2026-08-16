#!/usr/bin/env bash
# Republish Hold-or-Sell.html to the permanent aired.sh URL.
# Requires: npm, and AIRED_UPDATE_TOKEN in the environment (do not commit the token).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SRC="$ROOT/Hold-or-Sell.html"
TOKEN="${AIRED_UPDATE_TOKEN:-}"
if [[ -z "$TOKEN" ]]; then
  echo "Set AIRED_UPDATE_TOKEN to update the existing site." >&2
  echo "Publishing a new permanent URL instead…" >&2
  npx --yes aired "$SRC" --permanent --title "Grain Marketing Decisions"
  exit 0
fi
npx --yes aired "$SRC" --permanent --title "Grain Marketing Decisions" --update-token "$TOKEN"
