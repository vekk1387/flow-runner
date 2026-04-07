# Flow Runner

YAML-configured pipeline engine for multi-agent orchestration with full LLM auditability.

## What It Does

Declarative YAML flows define agent operations as sequential steps. Each step references a reusable action type (`db.query`, `llm.call`, `llm.call_gemini`, `llm.call_codex`, etc.) and passes data forward via `{{template}}` variables. Every step is audited to SurrealDB with timing, token counts, cost, and the full LLM provider response.

## Providers

| Provider | Action | Interface | Auth |
|----------|--------|-----------|------|
| Claude | `llm.call` | `claude -p` CLI | Claude Max / API |
| Gemini | `llm.call_gemini` | Direct HTTP API | `GEMINI_API_KEY` env var |
| Codex (OpenAI) | `llm.call_codex` | `codex exec` CLI | ChatGPT / API |

## Setup

```bash
# Install
uv sync

# Set Gemini key (if using Gemini provider)
export GEMINI_API_KEY="your-key-here"

# Apply SurrealDB schema
surreal import --conn http://localhost:8282 --user root --pass root --ns agent_build --db main schema/018_flows.surql

# Seed stored queries and model configs
surreal import --conn http://localhost:8282 --user root --pass root --ns agent_build --db main seed/seed_flow_components.surql
```

## Usage

```bash
# List available flows
uv run flow-run --list

# Inspect a flow without executing
uv run flow-run --inspect agent-receive-work.yaml

# Dry run (no LLM calls, validates pipeline)
uv run flow-run agent-receive-work.yaml --agent sap --dry-run

# Real run with Claude Haiku
uv run flow-run agent-receive-work.yaml --agent data --model haiku

# Run with Gemini Flash
uv run flow-run agent-receive-work-gemini.yaml --agent data

# Run with Codex (OpenAI)
uv run flow-run agent-receive-work-codex.yaml --agent data --model gpt-5.4-mini

# Override working directory (skip CLAUDE.md loading)
uv run flow-run agent-receive-work.yaml --agent data --model haiku --cwd /tmp/sandbox
```

## Flow Structure

```yaml
flow: agent-receive-work
version: 1
trigger: dispatch_go

steps:
  - id: read_inbox
    action: db.query
    params:
      query_key: agent_inbox
      bind: { agent_id: "{{agent_id}}" }
    output: inbox

  - id: call_model
    action: llm.call
    params:
      model: "{{assessment.model_tier}}"
      prompt: "{{prompt_payload.user_prompt}}"
    output: llm_result
    audit: full
```

## Audit Trail

Every flow execution creates:
- **`flow_run`** — run-level record (agent, status, duration, total tokens/cost, provider)
- **`flow_step_log`** — per-step record (action, timing, input hash, output summary, tokens, cost)
- **`flow_step_log.llm_response`** — full JSON blob from the LLM provider (all fields, nothing lost)

## Architecture

```
flow_runner/
  cli.py       — CLI entry point (flow-run command)
  runner.py    — Core engine: YAML loading, {{var}} resolution, step execution, DB audit
  steps.py     — Action implementations (db.query, budget.check, routing.assess, prompt.build, llm.*)
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
| `flow_run` | Execution audit trail |
| `flow_step_log` | Per-step audit with full LLM response capture |
| `stored_query` | Parameterized SurrealQL templates |
| `model_config` | LLM model parameters and cost rates |
