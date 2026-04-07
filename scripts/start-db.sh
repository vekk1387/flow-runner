#!/usr/bin/env bash
# Start SurrealDB in the background with file-based storage
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
DATA_DIR="$PROJECT_DIR/data"

# Load env if exists
[ -f "$PROJECT_DIR/.env" ] && { set -a; source "$PROJECT_DIR/.env"; set +a; }

HOST="${SURREAL_HOST:-http://localhost:8282}"
PORT="${HOST##*:}"
USER="${SURREAL_USER:-root}"
PASS="${SURREAL_PASS:-root}"

# Check if already running
if curl -sf "$HOST/health" > /dev/null 2>&1; then
  echo "SurrealDB already running at $HOST"
  exit 0
fi

# Find surreal binary
SURREAL=""
if command -v surreal > /dev/null 2>&1; then
  SURREAL="surreal"
elif [ -f "$HOME/tools/surreal.exe" ]; then
  SURREAL="$HOME/tools/surreal.exe"
elif [ -f "$HOME/tools/surreal" ]; then
  SURREAL="$HOME/tools/surreal"
elif [ -f "/usr/local/bin/surreal" ]; then
  SURREAL="/usr/local/bin/surreal"
fi

if [ -z "$SURREAL" ]; then
  echo "ERROR: surreal binary not found."
  echo ""
  echo "Install options:"
  echo "  macOS/Linux:  curl -sSf https://install.surrealdb.com | sh"
  echo "  Windows:      Download from https://surrealdb.com/install"
  echo "  Then place the binary in ~/tools/ or on your PATH"
  exit 1
fi

echo "Using: $SURREAL"
echo "Data:  $DATA_DIR"

# Create data dir
mkdir -p "$DATA_DIR"

# Start in background
"$SURREAL" start \
  --bind "0.0.0.0:$PORT" \
  --user "$USER" \
  --pass "$PASS" \
  "file:$DATA_DIR/surreal.db" \
  > "$DATA_DIR/surreal.log" 2>&1 &

DB_PID=$!
echo "Started SurrealDB (PID: $DB_PID)"
echo "$DB_PID" > "$DATA_DIR/surreal.pid"

# Wait for it to be ready
for i in $(seq 1 10); do
  if curl -sf "$HOST/health" > /dev/null 2>&1; then
    VERSION=$(curl -sf "$HOST/version" 2>/dev/null || echo "?")
    echo "SurrealDB ready: $VERSION at $HOST"
    exit 0
  fi
  sleep 1
done

echo "WARNING: SurrealDB started but not responding yet. Check $DATA_DIR/surreal.log"
