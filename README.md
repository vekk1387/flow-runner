# Flow Runner v0.2.0

YAML-configured pipeline engine with multi-provider LLM routing, automatic fallback, scoring, and eval.

## What It Does

Declarative YAML flows define operations as sequential steps. Each step references a reusable action type (`db.query`, `llm.call`, `llm.call_gemini`, `llm.call_codex`, `routing.assess`, `routing.scorecard`, `eval.judge`, etc.) and passes data forward via `{{template}}` variables. Every step is audited to SurrealDB with timing, token counts, cost, and full LLM provider response.

The auto-router (`routing.select_provider`) picks the cheapest provider that can handle the task, and `llm.call_auto` falls back automatically if the primary provider is rate-limited.

## Providers

| Provider | Action | Interface | Cost Tier | Auth |
|----------|--------|-----------|-----------|------|
| Gemini | `llm.call_gemini` | Direct HTTP API | Free (API credits) | `GEMINI_API_KEY` env var |
| Codex (OpenAI) | `llm.call_codex` | `codex exec` CLI | Subscription (flat) | ChatGPT / API |
| Claude | `llm.call` | `claude -p` CLI | Metered (per-token) | Claude Max / API |

## Routing Decision Tree

The `routing.select_provider` action applies these rules (zero cost -- pure Python):

1. **Explicit override wins** -- `--provider` or `--model` flag bypasses all logic
2. **Gemini first** -- if the task has no tool use, no repo context, and complexity <= medium, route to Gemini (free)
3. **Codex budget relief** -- if Claude budget pressure is high (session > 60% or weekly > 70%) and the task doesn't need repo context, offload to Codex
4. **Claude by complexity** -- low=haiku, medium=sonnet, high=opus

If Gemini returns 429/403 mid-call, `llm.call_auto` automatically falls back through the decision tree with Gemini disabled.

## Scorecard

Every flow run with an LLM call gets an automatic scorecard (zero cost, pure math):

- **score** (0-4): call_succeeded + produced_output + engaged + no_fallback
- **engagement_ratio**: output_tokens / input_tokens
- **likely_misrouted**: call succeeded but produced almost no output

## Eval System

The `eval-routing` flow runs on-demand (typically end-of-week) and uses LLM-as-judge to evaluate routing quality:

- Pulls recent `flow_run` records with stored content from `flow_run_content`
- Gemini Pro judges each run for free
- Optional Opus calibration for quality benchmarking
- Results stored in `routing_eval` table

## Setup

```bash
# Install dependencies
uv sync

# Copy and edit environment config
cp .env.example .env
# Edit .env with your GEMINI_API_KEY and SurrealDB settings

# Apply SurrealDB schema (note: SURREAL_NS=flow_runner, not agent_build)
surreal import --conn http://localhost:8282 --user root --pass root \
  --ns flow_runner --db main schema/018_flows.surql

# Seed stored queries and model configs
surreal import --conn http://localhost:8282 --user root --pass root \
  --ns flow_runner --db main seed/seed_flow_components.surql
```

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `SURREAL_HOST` | `http://localhost:8282` | SurrealDB connection URL |
| `SURREAL_NS` | `flow_runner` | SurrealDB namespace |
| `SURREAL_DB` | `main` | SurrealDB database name |
| `SURREAL_USER` | `root` | SurrealDB username |
| `SURREAL_PASS` | `root` | SurrealDB password |
| `GEMINI_API_KEY` | (none) | Google Gemini API key |
| `FLOW_RUNNER_ROOT` | (project dir) | Project root for budget scripts |
| `FLOW_RUNNER_CWD` | (project dir) | Working directory for Claude CLI |
| `FLOWS_DIR` | `.flows/` | Directory containing YAML flow definitions |
| `BUDGET_CHECK_SCRIPT` | (none) | Path to budget check shell script |
| `CODEX_SANDBOX_DIR` | `~/flow-sandbox` | Working directory for Codex CLI |

## Usage

```bash
# List available flows
uv run flow-run --list

# Inspect a flow without executing
uv run flow-run --inspect demo-prompt.yaml

# Dry run (no LLM calls, validates pipeline)
uv run flow-run demo-prompt.yaml --dry-run

# Run with auto-routing (picks cheapest provider)
uv run flow-run demo-prompt.yaml

# Force a specific provider
uv run flow-run demo-prompt.yaml --provider gemini
uv run flow-run demo-prompt.yaml --provider claude --model opus

# Override model directly
uv run flow-run demo-prompt.yaml --model haiku
uv run flow-run demo-prompt.yaml --model gpt-4o

# Run eval flow
uv run flow-run eval-routing.yaml --agent default

# Verbose output
uv run flow-run demo-prompt.yaml -v
```

## Demo Flow

The `demo-prompt` flow works standalone with zero stored queries:

```yaml
flow: demo-prompt
trigger: manual
steps:
  - budget.check       # returns ok if no script configured
  - routing.assess     # classify complexity from prompt text
  - routing.select_provider  # pick cheapest capable provider
  - llm.call_auto      # dispatch to selected provider with fallback
  - routing.scorecard  # score the result (0-4)
```

## Architecture

```
flow_runner/
  __init__.py  — Package marker
  cli.py       — CLI entry point (flow-run command)
  runner.py    — Core engine: YAML loading, {{var}} resolution, step execution, DB audit
  steps.py     — Action implementations (db.query, budget.check, routing.*, prompt.build, llm.*, eval.*)
  db.py        — SurrealDB HTTP client
  models.py    — Data models (FlowDefinition, FlowRun, StepResult)

.flows/        — YAML flow definitions (source of truth, git-diffable)
schema/        — SurrealDB migration (018_flows.surql)
seed/          — Stored queries and model configs
```

## DB Tables

| Table | Purpose |
|-------|---------|
| `flow` | Registered flow definitions (synced from YAML) |
| `flow_run` | Execution audit trail (one record per run) |
| `flow_step_log` | Per-step audit with full LLM response capture |
| `flow_run_content` | Full prompt + response text for eval/training |
| `routing_eval` | LLM-as-judge evaluation results |
| `stored_query` | Parameterized SurrealQL templates |
| `model_config` | LLM model parameters and cost rates |

## Action Registry

| Action | Description | Cost |
|--------|-------------|------|
| `db.query` | Execute a stored query with parameter binding | Free |
| `budget.check` | Check budget via external script | Free |
| `routing.assess` | Classify task complexity and requirements | Free |
| `routing.select_provider` | Pick cheapest capable provider | Free |
| `prompt.build` | Assemble full prompt from persona, tasks, messages | Free |
| `llm.call_auto` | Dispatch to selected provider with automatic fallback | Varies |
| `llm.call` | Call Claude via CLI | Metered |
| `llm.call_gemini` | Call Gemini via HTTP API | Free |
| `llm.call_codex` | Call OpenAI via Codex CLI | Subscription |
| `routing.scorecard` | Compute routing quality signals | Free |
| `eval.judge` | LLM-as-judge evaluation | Free (Gemini) |
| `eval.store` | Persist eval results to DB | Free |
