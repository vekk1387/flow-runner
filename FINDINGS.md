# Flow Runner — POC Findings

**Date:** 2026-04-06
**Task:** Smoke test — assign a P5 task ("Respond with exactly: FLOW_OK") to agent:data (Gimli) and run through the full 10-step flow with each provider.

## Provider Comparison

| | **Claude Haiku** (Agent-Build dir) | **Claude Haiku** (Sandbox dir) | **Gemini Flash** (Direct API) | **Codex gpt-5.4-mini** (CLI) |
|---|---|---|---|---|
| **Provider** | claude | claude | gemini | codex |
| **Interface** | `claude -p` CLI | `claude -p` CLI | HTTP POST | `codex exec` CLI |
| **Total tokens** | 168,023 | 288,632 | 1,869 | 511,845 |
| **Input tokens** | 42 | 74 | 6 | 504,039 |
| **Output tokens** | 1,382 | 1,791 | 3 | 7,806 |
| **Cache creation** | 13,963 | 6,670 | — | — |
| **Cache read** | 152,636 | 280,097 | — | 477,696 |
| **Thinking tokens** | — | — | 28 | — |
| **Total duration** | 43.8s | 55.7s | 25.1s | 112.6s |
| **API duration** | 16.5s | 27.6s | — | — |
| **Cost (reported)** | $0.040 | $0.045 | $0.00 | $0.00 |
| **Billing model** | Max subscription | Max subscription | Free monthly credits | ChatGPT subscription |
| **Turns** | 6 | 9 | 1 | 1 |
| **Items/Events** | — | — | — | 37 items / 63 events |
| **Service tier** | standard | standard | — | — |
| **Context window** | 200,000 | 200,000 | — | — |

## Key Findings

### 1. CLI overhead dominates simple tasks

Both `claude -p` and `codex exec` inject large system prompts (~30K+ tokens) with tool definitions, safety instructions, and environment context. This is the baseline tax regardless of how small the actual task is. Gemini's direct API call sent only the ~1,800 token prompt we built — **126x fewer tokens** than Claude for the same task.

### 2. Cache temperature matters more than CLAUDE.md presence

Initial hypothesis was that removing the CLAUDE.md file (by running from a bare sandbox directory) would reduce tokens. In reality:

- **Agent-Build dir** (warm cache): 152K cache reads, 14K cache creation → **168K total**
- **Sandbox dir** (cold cache): 280K cache reads, 7K cache creation → **289K total**

The sandbox was actually more expensive because it hit a cold cache and created new entries at the higher cache-creation rate (25% of input price vs 10% for cache reads). The CLAUDE.md file itself is only ~2-3K tokens — negligible compared to the CLI's built-in system prompt.

### 3. Cost model on Max subscription

On Claude Max, there is no per-token dollar cost — it's a flat subscription. The "cost" field reflects what API pricing would be, but the actual constraint is **rate limit windows** (rolling 5-hour session, 7-day weekly). Token counts still matter because they consume rate limit budget, but the optimization target is throughput per window, not dollars.

### 4. Gemini Flash is the clear winner for simple tasks

For tasks that don't require tool use, file access, or multi-turn interaction:
- 1,869 tokens (vs 168K–512K for CLI-based providers)
- $0.00 cost (free monthly credits)
- 25s total duration (fastest)
- Zero CLI overhead — pure HTTP call

### 5. Codex is the heaviest

For a "say FLOW_OK" task, Codex produced 37 items across 63 JSONL events, consumed 512K tokens, and took 113 seconds. It has its own tool framework overhead similar to Claude's CLI. The `gpt-5.4-mini` model was faster than the default model (113s vs 179s, 512K vs 528K tokens).

### 6. Full response capture is essential

Each provider returns different metadata fields. Storing the complete provider response as a JSON string in `flow_step_log.llm_response` captures everything without requiring schema changes per provider:

- **Claude (22 fields):** input/output/cache tokens, cache TTL breakdown (1h vs 5m), duration_ms vs duration_api_ms, service_tier, speed, session_id, uuid, fast_mode, web_search/fetch counts, context_window, max_output_tokens, inference_geo
- **Gemini (13 fields):** input/output/thinking tokens, modality breakdown, response_id, model_version
- **Codex (13 fields):** input/output/cached tokens, thread_id, turn_count, item_count, event_count, errors array

Without the raw capture, 16 of 22 Claude fields were being lost after each run.

## Model Tier Mapping

For comparable "fast/cheap" models across providers:

| Tier | Claude | Gemini | OpenAI (Codex) |
|------|--------|--------|----------------|
| Fast | Haiku (`claude-haiku-4-5-20251001`) | Flash (`gemini-flash-latest`) | `gpt-5.4-mini` |
| Standard | Sonnet (`claude-sonnet-4-6`) | Pro (`gemini-pro-latest`) | — |
| Heavy | Opus (`claude-opus-4-6`) | — | — |

## Routing Implications

Based on these findings, the routing strategy should consider:

1. **Simple, stateless tasks** (status checks, data formatting, classification) → **Gemini Flash** via direct API. Zero overhead, free credits, fastest.
2. **Tasks requiring file access or tool use** → **Claude via `claude -p`**. The CLI overhead is the price for tool access. Run from a warm project directory to maximize cache reads.
3. **Coding tasks requiring sandbox execution** → **Codex CLI** when OpenAI models are preferred, but expect high token overhead from the tool framework.
4. **Budget-constrained periods** → Prefer Gemini (free) or Codex (ChatGPT subscription) over Claude (rate-limited Max subscription).

## Flow Execution Breakdown

All runs followed the same 10-step pipeline:

| Step | Action | Typical Duration |
|------|--------|-----------------|
| check_budget | `budget.check` | ~2.5s (shells out to check-budget.sh) |
| read_inbox | `db.query` | ~60ms |
| load_persona | `db.query` | ~60ms |
| load_instructions | `db.query` | ~60ms |
| load_recent_activity | `db.query` | ~60ms |
| assess_complexity | `routing.assess` | <1ms (in-process) |
| build_prompt | `prompt.build` | <1ms (in-process) |
| call_model | `llm.*` | 15s–180s (provider dependent) |
| store_result | `db.query` | ~60ms |
| log_completion | `db.query` | ~60ms |

The DB query steps are consistent at ~60ms each. The `budget.check` step is slow (~2.5s) because it shells out to a bash script. The LLM call dominates total runtime in all cases.
