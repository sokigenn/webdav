#!/bin/bash
set -e
cd "$(dirname "$0")"
PORT=8765

echo ""
echo " WebDAV Downloader v1.0"
echo " =============================="
echo ""

# Step 1: Find Python 3 (prefer 3.12, accept 3.10+)
PY=""
for candidate in python3.12 python3.11 python3.10 python3; do
  if command -v "$candidate" &>/dev/null; then
    VER=$("$candidate" -c "import sys; print(sys.version_info[:2] >= (3,10))" 2>/dev/null)
    if [ "$VER" = "True" ]; then
      PY="$candidate"
      break
    fi
  fi
done

if [ -z "$PY" ]; then
  echo " Python 3.10+ not found."
  echo " Install via Homebrew:  brew install python@3.12"
  echo " Or download from:      https://www.python.org/downloads/"
  exit 1
fi

echo " Using: $($PY --version)"

# Step 2: Create virtual environment
if [ -f ".venv/bin/python" ]; then
  .venv/bin/python --version &>/dev/null || rm -rf .venv
fi
if [ ! -f ".venv/bin/python" ]; then
  echo " Creating virtual environment..."
  "$PY" -m venv .venv
fi

# Step 3: Install dependencies
echo " Checking dependencies..."
.venv/bin/pip install -r requirements.txt -q --disable-pip-version-check

# Step 4: Start server
mkdir -p downloads
echo ""
echo " Server started!"
echo " Open browser: http://localhost:$PORT"
echo " Press Ctrl+C to stop."
echo ""

# Open browser after 2s delay
(sleep 2 && open "http://localhost:$PORT") &

.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port $PORT
