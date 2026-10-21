#!/bin/zsh
# Bootstrap a new Mac for Beam Farm Ops + Cursor coding + free HTML hosting.
# Safe to re-run. Requires: macOS Terminal, internet, Admin password once for Xcode tools.
set -euo pipefail

echo "==> 1) Xcode Command Line Tools (git, clang, make)"
if ! xcode-select -p >/dev/null 2>&1; then
  xcode-select --install || true
  echo "Finish the on-screen installer, then re-run this script."
  exit 1
fi

echo "==> 2) Homebrew"
if ! command -v brew >/dev/null 2>&1; then
  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
  if [ -x /opt/homebrew/bin/brew ]; then
    eval "$(/opt/homebrew/bin/brew shellenv)"
    echo 'eval "$(/opt/homebrew/bin/brew shellenv)"' >> ~/.zprofile
  elif [ -x /usr/local/bin/brew ]; then
    eval "$(/usr/local/bin/brew shellenv)"
    echo 'eval "$(/usr/local/bin/brew shellenv)"' >> ~/.zprofile
  fi
fi
eval "$(brew shellenv)"

echo "==> 3) Core developer packages"
brew update
brew install git python@3.12 node jq rsync tree gh postgresql@16 2>/dev/null || \
  brew install git python@3.12 node jq rsync tree gh

# Optional but useful for harder tasks
brew install --cask cursor docker 2>/dev/null || true
brew install --cask google-chrome github 2>/dev/null || true

echo "==> 4) HTML / free hosting CLIs"
npm install -g serve http-server vercel netlify-cli wrangler

echo "==> 5) Fast Python tooling"
curl -LsSf https://astral.sh/uv/install.sh | sh

echo "==> 6) This repo: Python venv + deps"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
if [ ! -d .venv ]; then
  python3.12 -m venv .venv
fi
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt

echo
echo "Done. Next:"
echo "  1. Open Cursor and open this folder: $ROOT"
echo "  2. Run the app:  ./run-mac.sh"
echo "  3. Browser:      http://127.0.0.1:8001  (admin / farm2026)"
echo "  4. Free hosting for static HTML folders:"
echo "       npx serve ./my-site"
echo "       vercel   OR   netlify deploy"
echo "  5. Farm Ops cloud sync: see DEPLOY.md (Neon + Render)"
echo
echo "Installed / available:"
command -v brew git python3.12 node npm serve vercel netlify wrangler uv gh 2>/dev/null | sort -u
