#!/usr/bin/env bash
# Stop SurrealDB
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
PID_FILE="$PROJECT_DIR/data/surreal.pid"

if [ -f "$PID_FILE" ]; then
  PID=$(cat "$PID_FILE")
  if kill -0 "$PID" 2>/dev/null; then
    echo "Stopping SurrealDB (PID: $PID)..."
    kill "$PID"
    rm -f "$PID_FILE"
    echo "Stopped."
  else
    echo "PID $PID not running. Cleaning up."
    rm -f "$PID_FILE"
  fi
else
  echo "No PID file found. Trying to find process..."
  if command -v taskkill.exe > /dev/null 2>&1; then
    taskkill.exe /F /IM surreal.exe 2>/dev/null && echo "Stopped." || echo "Not running."
  else
    pkill -f "surreal start" 2>/dev/null && echo "Stopped." || echo "Not running."
  fi
fi
