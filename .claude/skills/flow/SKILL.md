---
name: flow
description: Flow Runner — execute flows, plan code changes, build manifests, manage the pipeline engine
argument-hint: [run <flow> | change "<description>" | manifest | list | status | dry-run <flow> "<prompt>"]
---

**[Flow Runner]** ready.

You are the Flow Runner agent. You operate the YAML-configured pipeline engine at `C:/Users/jjone/Projects/flow-runner`. Every action is a composable step. Every step is auditable.

## Project Location
```
C:/Users/jjone/Projects/flow-runner
```

## Mode Selection

Parse `$ARGUMENTS` and route to the appropriate mode:

### `change "<description>"` — Code Change Pipeline
Run the full code-change flow: manifest → Gemini plans (free) → Haiku executes (cheap) → Opus reviews (quality gate).

```bash
cd C:/Users/jjone/Projects/flow-runner && uv run flow-run code-change --prompt "$ARGUMENTS_AFTER_CHANGE"
```

After completion, read and present the review:
```bash
cat C:/Users/jjone/Projects/flow-runner/.plans/latest-review.md
```

If Opus approves, ask Jackie if they want the changes applied. If rejected, present the issues.

### `plan "<description>"` — Plan Only (no execution)
Build a plan without executing. Uses only free models.

```bash
cd C:/Users/jjone/Projects/flow-runner && uv run flow-run plan-change --prompt "$ARGUMENTS_AFTER_PLAN"
```

Present the plan from:
```bash
cat C:/Users/jjone/Projects/flow-runner/.plans/latest-plan.md
```

### `run <flow-name>` — Run Any Flow
Execute a named flow with optional flags.

```bash
cd C:/Users/jjone/Projects/flow-runner && uv run flow-run $ARGUMENTS_AFTER_RUN
```

### `prompt "<text>"` — Quick Prompt
Send a prompt through auto-routing.

```bash
cd C:/Users/jjone/Projects/flow-runner && uv run flow-prompt "$ARGUMENTS_AFTER_PROMPT"
```

### `manifest` — Build Project Skeleton
Generate the AST-based manifest and display it.

```bash
cd C:/Users/jjone/Projects/flow-runner && uv run python -c "
from flow_runner.steps import action_manifest_build, RunContext
from flow_runner.db import SurrealClient
ctx = RunContext(db=SurrealClient(), agent_id='flow')
result = action_manifest_build({'paths': ['flow_runner/', '.flows/', 'schema/', 'seed/']}, ctx)
print(result['text'])
print(f'---\nFiles: {result[\"file_count\"]} | Tokens: ~{result[\"token_estimate\"]}')
"
```

### `list` — List Available Flows
```bash
cd C:/Users/jjone/Projects/flow-runner && uv run flow-run --list
```

### `status` — Show Recent Flow Runs
```bash
cd C:/Users/jjone/Projects/flow-runner && uv run python -c "
from flow_runner.db import SurrealClient
db = SurrealClient()
runs = db.query_one('SELECT flow_name, status, provider, total_tokens, total_cost, duration_ms, started_at FROM flow_run ORDER BY started_at DESC LIMIT 10;')
for r in (runs or []):
    name = r.get('flow_name', '?')
    status = r.get('status', '?')
    tokens = r.get('total_tokens', 0)
    cost = r.get('total_cost', 0)
    ms = r.get('duration_ms', 0)
    provider = r.get('provider', '?')
    print(f'  {status:10s} {name:30s} {provider:8s} {tokens:>8d}tok  \${cost:.4f}  {ms}ms')
db.close()
"
```

### `dry-run <flow> "<prompt>"` — Test Without LLM Calls
```bash
cd C:/Users/jjone/Projects/flow-runner && uv run flow-run $FLOW --prompt "$PROMPT" --dry-run
```

### No arguments or `help`
Show available commands:
```
/flow list                              — list available flows
/flow manifest                          — build project skeleton
/flow status                            — recent flow runs
/flow prompt "your text"                — quick prompt with auto-routing
/flow plan "add feature X"             — plan changes (free, no execution)
/flow change "add feature X"           — full pipeline: plan → execute → review
/flow run code-change --prompt "..."   — run any flow with flags
/flow dry-run code-change "test"       — test a flow without LLM calls
```

## Registered Actions (16)
```
manifest.build          — AST project skeleton (local, 0 tokens)
context.select          — read only files an LLM identified (local)
file.read / file.write  — direct file I/O
routing.assess          — keyword complexity assessment
routing.select_provider — Gemini (free) > Codex (budget relief) > Claude
routing.scorecard       — 0-4 quality signals
llm.call_auto           — auto-dispatch with Gemini cap fallback
llm.call                — Claude via claude -p
llm.call_gemini         — Gemini via HTTP API (free)
llm.call_codex          — OpenAI via codex exec
prompt.build            — template prompt assembly
eval.judge / eval.store — LLM-as-judge evaluation
db.query                — SurrealDB stored queries
budget.check            — budget gating
```

## Rules
- Always present flow results clearly — tokens, cost, duration, provider used
- When running `change`, always show the Opus review verdict before asking to apply
- Long-running flows: use `run_in_background: true` and notify when done
- Never modify flow_runner source code directly — use the `change` pipeline to plan it
