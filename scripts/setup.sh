#!/usr/bin/env bash
# One-time setup: start DB if needed, apply schema, seed data, create .env
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

# ── .env ──────────────────────────────────────────────────────────
if [ ! -f "$PROJECT_DIR/.env" ]; then
  echo "Creating .env from .env.example..."
  cp "$PROJECT_DIR/.env.example" "$PROJECT_DIR/.env"
  echo "  Edit .env to set your GEMINI_API_KEY"
else
  echo ".env already exists"
fi

# Load env vars
set -a
source "$PROJECT_DIR/.env"
set +a

SURREAL_HOST="${SURREAL_HOST:-http://localhost:8282}"
SURREAL_NS="${SURREAL_NS:-flow_runner}"
SURREAL_DB="${SURREAL_DB:-main}"
SURREAL_USER="${SURREAL_USER:-root}"
SURREAL_PASS="${SURREAL_PASS:-root}"

# ── SurrealDB ─────────────────────────────────────────────────────
echo ""
echo "Checking SurrealDB..."

if curl -sf "$SURREAL_HOST/health" > /dev/null 2>&1; then
  VERSION=$(curl -sf "$SURREAL_HOST/version" 2>/dev/null || echo "unknown")
  echo "  SurrealDB running: $VERSION at $SURREAL_HOST"
else
  echo "  SurrealDB not running. Starting..."
  bash "$SCRIPT_DIR/start-db.sh"
  sleep 2
  if curl -sf "$SURREAL_HOST/health" > /dev/null 2>&1; then
    echo "  SurrealDB started."
  else
    echo "  ERROR: Failed to start SurrealDB."
    echo "  Install: https://surrealdb.com/install"
    echo "  Or start manually: surreal start --bind 0.0.0.0:8282 --user root --pass root file:data/surreal.db"
    exit 1
  fi
fi

# ── Schema ────────────────────────────────────────────────────────
echo ""
echo "Applying schema..."
SCHEMA_SQL=$(cat "$PROJECT_DIR/schema/018_flows.surql")

RESULT=$(curl -sf -X POST "$SURREAL_HOST/sql" \
  -H "Content-Type: application/json" \
  -H "surreal-ns: $SURREAL_NS" \
  -H "surreal-db: $SURREAL_DB" \
  -u "$SURREAL_USER:$SURREAL_PASS" \
  -H "Accept: application/json" \
  --data-raw "$SCHEMA_SQL" 2>&1) || true

if echo "$RESULT" | grep -q '"status":"ERR"'; then
  echo "  Schema warning (may already exist): $(echo "$RESULT" | head -c 200)"
else
  echo "  Schema applied."
fi

# ── Seed ──────────────────────────────────────────────────────────
echo ""
echo "Seeding stored queries and model configs..."
SEED_SQL=$(cat "$PROJECT_DIR/seed/seed_flow_components.surql")

RESULT=$(curl -sf -X POST "$SURREAL_HOST/sql" \
  -H "Content-Type: application/json" \
  -H "surreal-ns: $SURREAL_NS" \
  -H "surreal-db: $SURREAL_DB" \
  -u "$SURREAL_USER:$SURREAL_PASS" \
  -H "Accept: application/json" \
  --data-raw "$SEED_SQL" 2>&1) || true

echo "  Seed data applied."

# ── Python deps ───────────────────────────────────────────────────
echo ""
echo "Installing Python dependencies..."
cd "$PROJECT_DIR"
uv sync 2>&1 | tail -3

# ── Verify ────────────────────────────────────────────────────────
echo ""
echo "Verifying..."
uv run flow-run --list

echo ""
echo "================================================"
echo " Setup complete!"
echo ""
echo " Quick start:"
echo "   uv run flow-prompt 'Explain how LLM routing works'"
echo "   uv run flow-run demo-prompt.yaml --dry-run"
echo "   uv run flow-run --list"
echo "================================================"
