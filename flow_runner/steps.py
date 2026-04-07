"""Step action implementations.

Each action type (db.query, llm.call, etc.) is a function that receives
resolved params and a RunContext, and returns output data.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .db import SurrealClient

log = logging.getLogger(__name__)

# Project root — configurable via environment
PROJECT_ROOT = Path(os.environ.get("FLOW_RUNNER_ROOT", str(Path(__file__).resolve().parent.parent)))


def _esc(s: str | None, max_len: int = 500) -> str:
    """Escape a string for SurrealQL."""
    if not s:
        return ""
    return s.replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n")[:max_len]


@dataclass
class RunContext:
    """Shared state passed to every step."""
    db: SurrealClient
    agent_id: str
    flow_run_db_id: str | None = None
    dry_run: bool = False


# ── Registry ────────────────────────────────────────────────────────

ACTION_REGISTRY: dict[str, Any] = {}


def register_action(name: str):
    def decorator(fn):
        ACTION_REGISTRY[name] = fn
        return fn
    return decorator


def get_action(name: str):
    fn = ACTION_REGISTRY.get(name)
    if not fn:
        raise ValueError(f"Unknown action type: {name}. Available: {list(ACTION_REGISTRY.keys())}")
    return fn


# ── Actions ─────────────────────────────────────────────────────────

@register_action("db.query")
def action_db_query(params: dict, ctx: RunContext) -> Any:
    """Execute a stored query by key with bound parameters."""
    query_key = params["query_key"]
    bind = params.get("bind", {})

    log.info(f"  db.query: key={query_key}, bind={list(bind.keys())}")

    results = ctx.db.execute_stored_query(query_key, bind)

    # For single-statement queries, unwrap
    if len(results) == 1:
        r = results[0]
        if isinstance(r, list) and len(r) == 1:
            return r[0]
        return r

    return results


@register_action("budget.check")
def action_budget_check(params: dict, ctx: RunContext) -> dict:
    """Check budget via an external script (if configured)."""
    script_path = os.environ.get("BUDGET_CHECK_SCRIPT", "")
    script = Path(script_path) if script_path else None

    if ctx.dry_run:
        return {"status": "ok", "session_pct": 0, "weekly_pct": 0, "adaptive_cap": 100}

    if script is None or not script.exists():
        log.warning("No budget check script configured or script not found, returning ok")
        return {"status": "ok", "session_pct": 0, "weekly_pct": 0, "adaptive_cap": 100}

    try:
        result = subprocess.run(
            ["bash", str(script)],
            capture_output=True, text=True, timeout=15,
            cwd=str(PROJECT_ROOT),
        )

        output = result.stdout.strip()
        log.info(f"  budget.check: exit={result.returncode}, output={output[:120]}")

        if result.returncode == 0:
            # Parse the output line: "GO | Session: X% | Weekly: Y% | ..."
            budget = {"status": "ok", "raw": output}
            for part in output.split("|"):
                part = part.strip()
                if "Session:" in part:
                    try:
                        budget["session_pct"] = float(part.split(":")[1].strip().rstrip("%"))
                    except (ValueError, IndexError):
                        pass
                elif "Weekly:" in part:
                    try:
                        budget["weekly_pct"] = float(part.split(":")[1].strip().rstrip("%"))
                    except (ValueError, IndexError):
                        pass
            return budget
        else:
            return {"status": "blocked", "reason": output, "exit_code": result.returncode}

    except subprocess.TimeoutExpired:
        return {"status": "blocked", "reason": "budget check timed out"}
    except Exception as e:
        log.error(f"  budget.check failed: {e}")
        return {"status": "blocked", "reason": str(e)}


@register_action("routing.assess")
def action_routing_assess(params: dict, ctx: RunContext) -> dict:
    """Assess task complexity and determine model tier."""
    tasks = params.get("tasks", [])
    messages = params.get("messages", [])
    persona = params.get("persona", {})

    # Count work items
    task_count = len(tasks) if isinstance(tasks, list) else 0
    msg_count = len(messages) if isinstance(messages, list) else 0
    total_items = task_count + msg_count

    # Check for high-priority tasks
    has_p1 = any(
        t.get("priority", 5) == 1
        for t in (tasks if isinstance(tasks, list) else [])
    )

    # Detect task characteristics for provider routing
    needs_tool_use = False
    needs_repo_context = False
    is_code_heavy = False

    for t in (tasks if isinstance(tasks, list) else []):
        title = (t.get("title", "") + " " + t.get("description", "")).lower()
        # Tasks that mention file ops, git, or specific paths need tool use
        if any(kw in title for kw in ("edit", "fix", "create file", "refactor",
                                       "commit", "push", "git", "deploy", "test")):
            needs_tool_use = True
        # Tasks referencing infrastructure need repo context
        if any(kw in title for kw in ("config", "schema", "database",
                                       "infrastructure", "deploy")):
            needs_repo_context = True
        # Code-heavy tasks
        if any(kw in title for kw in ("implement", "code", "function", "class",
                                       "module", "script", "api", "endpoint")):
            is_code_heavy = True

    # Estimate complexity
    if total_items == 0:
        return {
            "complexity": "none",
            "model_tier": "fast",
            "estimated_tokens": 0,
            "needs_tool_use": False,
            "needs_repo_context": False,
            "is_code_heavy": False,
            "reason": "No work items found",
        }

    # Simple heuristic: more items or P1 = heavier model
    if has_p1 or total_items > 3:
        complexity = "high"
        model_tier = "heavy"
        est_tokens = 80000
    elif total_items > 1:
        complexity = "medium"
        model_tier = "standard"
        est_tokens = 40000
    else:
        complexity = "low"
        model_tier = "standard"
        est_tokens = 25000

    return {
        "complexity": complexity,
        "model_tier": model_tier,
        "estimated_tokens": est_tokens,
        "needs_tool_use": needs_tool_use,
        "needs_repo_context": needs_repo_context,
        "is_code_heavy": is_code_heavy,
        "reason": f"{task_count} tasks, {msg_count} messages, P1={has_p1}",
    }


# ── Provider Selection (zero-cost decision point) ─────────────────

# Decision hierarchy:
#   1. Gemini Flash — free API credits, use for everything it can handle
#   2. Claude       — primary workhorse (haiku/sonnet/opus by complexity)
#   3. Codex        — relief valve when Claude budget is tight
#
# Codex is NOT a capability tier. It's a budget pressure switch:
# when Claude session/weekly spend is high, offload qualifying work to Codex.

COMPLEXITY_RANK = {"none": 0, "low": 1, "medium": 2, "high": 3}

# Budget thresholds that trigger Codex offload (percentage of cap)
CODEX_SESSION_THRESHOLD = 60   # session spend > 60% → consider Codex
CODEX_WEEKLY_THRESHOLD = 70    # weekly spend > 70% → consider Codex


@register_action("routing.select_provider")
def action_select_provider(params: dict, ctx: RunContext) -> dict:
    """Zero-cost provider selection. Pure rules, no LLM call.

    Logic:
      1. Can Gemini handle it? (no tool use, no repo context, complexity <= medium) → Gemini
      2. Is Claude budget under pressure? → offload to Codex if task doesn't need repo context
      3. Otherwise → Claude (haiku/sonnet/opus scaled by complexity)
    """
    assessment = params.get("assessment", {})
    budget = params.get("budget", {})
    model_override = params.get("model_override")
    provider_override = params.get("provider_override")

    # ── Explicit overrides always win ──
    if provider_override:
        action_map = {"gemini": "llm.call_gemini", "codex": "llm.call_codex", "claude": "llm.call"}
        model_map = {"gemini": "gemini-flash", "codex": "", "claude": "sonnet"}
        return {
            "provider": provider_override,
            "action": action_map.get(provider_override, "llm.call"),
            "model": model_map.get(provider_override, "sonnet"),
            "reason": f"Explicit provider override: {provider_override}",
            "cost_rank": 0 if provider_override == "gemini" else 1,
        }

    if model_override:
        if model_override.startswith("gemini"):
            return {
                "provider": "gemini",
                "action": "llm.call_gemini",
                "model": model_override,
                "reason": f"Model override: {model_override}",
                "cost_rank": 0,
            }
        elif model_override in ("gpt-4o", "gpt-4o-mini", "o3", "o4-mini", "codex"):
            return {
                "provider": "codex",
                "action": "llm.call_codex",
                "model": model_override if model_override != "codex" else "",
                "reason": f"Model override: {model_override}",
                "cost_rank": 1,
            }
        else:
            return {
                "provider": "claude",
                "action": "llm.call",
                "model": model_override,
                "reason": f"Model override: {model_override}",
                "cost_rank": 2,
            }

    # ── Extract signals ──
    complexity = assessment.get("complexity", "low")
    needs_tool_use = assessment.get("needs_tool_use", False)
    needs_repo_context = assessment.get("needs_repo_context", False)
    is_code_heavy = assessment.get("is_code_heavy", False)

    session_pct = budget.get("session_pct", 0)
    weekly_pct = budget.get("weekly_pct", 0)
    budget_pressure = (session_pct >= CODEX_SESSION_THRESHOLD
                       or weekly_pct >= CODEX_WEEKLY_THRESHOLD)

    reason_parts = [f"complexity={complexity}"]
    if needs_tool_use:
        reason_parts.append("tools")
    if needs_repo_context:
        reason_parts.append("repo_ctx")
    if is_code_heavy:
        reason_parts.append("code")
    if budget_pressure:
        reason_parts.append(f"budget_pressure(s={session_pct}%,w={weekly_pct}%)")

    # ── Decision tree ──

    # 1. Gemini: free, handles anything that doesn't need tool use or repo context
    gemini_ok = (not needs_tool_use
                 and not needs_repo_context
                 and COMPLEXITY_RANK.get(complexity, 1) <= COMPLEXITY_RANK["medium"]
                 and gemini_is_available())

    if gemini_ok:
        return {
            "provider": "gemini",
            "action": "llm.call_gemini",
            "model": "gemini-flash",
            "reason": f"Gemini (free): {', '.join(reason_parts)}",
            "cost_rank": 0,
        }

    # 2. Budget pressure + doesn't need repo context → Codex relief valve
    codex_ok = (budget_pressure
                and not needs_repo_context
                and COMPLEXITY_RANK.get(complexity, 1) <= COMPLEXITY_RANK["high"])

    if codex_ok:
        return {
            "provider": "codex",
            "action": "llm.call_codex",
            "model": "",
            "reason": f"Codex (budget relief): {', '.join(reason_parts)}",
            "cost_rank": 1,
        }

    # 3. Claude — pick tier by complexity
    if complexity == "high":
        model = "opus"
    elif complexity == "medium":
        model = "sonnet"
    else:
        model = "haiku"

    return {
        "provider": "claude",
        "action": "llm.call",
        "model": model,
        "reason": f"Claude {model}: {', '.join(reason_parts)}",
        "cost_rank": 2,
    }


@register_action("prompt.build")
def action_prompt_build(params: dict, ctx: RunContext) -> dict:
    """Build the full prompt payload for the LLM call."""
    persona = params.get("persona", {})
    instructions = params.get("instructions", [])
    tasks = params.get("tasks", [])
    messages = params.get("messages", [])
    recent_activity = params.get("recent_activity", [])
    assessment = params.get("assessment", {})

    # Determine model from assessment or config (model_override wins)
    model_override = params.get("model_override")
    model_tier = assessment.get("model_tier", "standard")
    if model_override:
        model_flag = model_override
        tier_map = {"haiku": "fast", "sonnet": "standard", "opus": "heavy"}
        model_tier = tier_map.get(model_override, model_tier)
    else:
        model_map = {"fast": "haiku", "standard": "sonnet", "heavy": "opus"}
        model_flag = model_map.get(model_tier, "sonnet")

    # Agent identity
    agent_name = persona.get("display_name", persona.get("name", ctx.agent_id))
    agent_role = persona.get("role", "Specialist")

    # Build instruction block
    instruction_text = ""
    if isinstance(instructions, list):
        for inst in instructions:
            section = inst.get("section", "general")
            title = inst.get("title", "")
            content = inst.get("content", "")
            instruction_text += f"\n## [{section}] {title}\n{content}\n"

    # Build task block
    task_text = ""
    if isinstance(tasks, list):
        for t in tasks:
            tid = t.get("id", "?")
            title = t.get("title", "Untitled")
            desc = t.get("description", "")
            priority = t.get("priority", 3)
            status = t.get("status", "assigned")
            task_text += f"\n### Task {tid} (P{priority}, {status})\n**{title}**\n{desc}\n"

    # Build message block
    msg_text = ""
    if isinstance(messages, list):
        for m in messages:
            sender = m.get("sender", m.get("from_agent", "?"))
            subject = m.get("subject", "")
            body = m.get("body", "")
            mtype = m.get("type", "info")
            msg_text += f"\n### [{mtype}] From {sender}: {subject}\n{body}\n"

    # Build recent activity block
    activity_text = ""
    if isinstance(recent_activity, list):
        for a in recent_activity:
            action = a.get("action", "?")
            desc = a.get("description", "")
            activity_text += f"- {action}: {desc}\n"

    user_prompt = f"""You are {agent_name}, {agent_role}.

# Instructions
{instruction_text}

# Your Recent Activity
{activity_text}

# Current Work
{task_text}
{msg_text}

Work through the items below. Execute each task within your boundaries.

Start now."""

    return {
        "user_prompt": user_prompt,
        "model": model_flag,
        "model_tier": model_tier,
    }


@register_action("llm.call_auto")
def action_llm_call_auto(params: dict, ctx: RunContext) -> dict:
    """Auto-dispatch to the right provider based on routing.select_provider output.

    This is the single entry point — flows no longer pick a provider at the YAML level.
    If the selected provider hits a rate limit (Gemini 429/403), automatically falls
    back to the next cheapest provider.
    """
    routing = params.get("routing", {})
    provider = routing.get("provider", "claude")
    action_name = routing.get("action", "llm.call")
    model = routing.get("model", "sonnet")

    log.info(f"  llm.call_auto: provider={provider}, model={model}, "
             f"reason={routing.get('reason', '?')}")

    # Build params for the actual provider call
    call_params = {
        "model": model,
        "prompt": params.get("prompt", ""),
        "budget": params.get("budget", {}),
        "agent_id": params.get("agent_id", ctx.agent_id),
    }
    if params.get("cwd_override"):
        call_params["cwd_override"] = params["cwd_override"]

    # Dispatch
    action_fn = get_action(action_name)
    result = action_fn(call_params, ctx)

    # If Gemini got capped, fall back automatically
    if result.get("gemini_capped"):
        log.warning("Gemini capped mid-call, falling back")

        # Re-run provider selection — gemini_is_available() now returns False
        assessment = params.get("assessment", routing)  # best we have
        fallback_routing = action_select_provider({
            "assessment": assessment,
            "budget": params.get("budget", {}),
        }, ctx)

        log.info(f"  Fallback: {fallback_routing['provider']} ({fallback_routing['reason']})")

        call_params["model"] = fallback_routing["model"]
        fallback_fn = get_action(fallback_routing["action"])
        result = fallback_fn(call_params, ctx)

        # Record that we fell back
        result["_routing"] = {
            **routing,
            "fallback_from": "gemini",
            "fallback_to": fallback_routing["provider"],
            "fallback_reason": "gemini_rate_limited",
        }
    else:
        result["_routing"] = routing

    return result


@register_action("llm.call")
def action_llm_call(params: dict, ctx: RunContext) -> dict:
    """Call Claude via `claude -p` and capture full telemetry."""
    model = params.get("model", "sonnet")
    prompt = params.get("prompt", "")
    agent_id = params.get("agent_id", ctx.agent_id)
    budget = params.get("budget", {})

    # Budget gate
    if budget.get("status") == "blocked":
        return {
            "text": "",
            "model": model,
            "total_tokens": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "duration_ms": 0,
            "cost": 0.0,
            "num_turns": 0,
            "stop_reason": "budget_blocked",
            "error": budget.get("reason", "Budget blocked"),
        }

    if ctx.dry_run:
        return {
            "text": "[DRY RUN] No LLM call made",
            "model": model,
            "total_tokens": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "duration_ms": 0,
            "cost": 0.0,
            "num_turns": 0,
            "stop_reason": "dry_run",
        }

    # Build claude command
    # Model flag mapping
    model_flags = {
        "opus": "--model claude-opus-4-6",
        "sonnet": "--model claude-sonnet-4-6",
        "haiku": "--model claude-haiku-4-5-20251001",
    }
    model_flag = model_flags.get(model, "")

    # Write prompt to temp file to avoid arg length limits on Windows
    import tempfile
    prompt_file = Path(tempfile.mktemp(suffix=".txt", prefix="flow_prompt_"))
    prompt_file.write_text(prompt, encoding="utf-8")

    try:
        # Working directory from params, env, or project root
        cwd = params.get("cwd_override", os.environ.get("FLOW_RUNNER_CWD", str(PROJECT_ROOT)))
        add_dirs = ""

        cmd = f'cat "{prompt_file}" | claude -p --output-format json {model_flag}{add_dirs}'

        log.info(f"  llm.call: model={model}, agent={agent_id}")
        start_time = time.time()

        result = subprocess.run(
            ["bash", "-c", cmd],
            capture_output=True,
            timeout=600,  # 10 min max
            cwd=cwd,
        )
        # Decode as UTF-8 explicitly (Windows defaults to cp1252)
        result.stdout = result.stdout.decode("utf-8", errors="replace") if isinstance(result.stdout, bytes) else (result.stdout or "")
        result.stderr = result.stderr.decode("utf-8", errors="replace") if isinstance(result.stderr, bytes) else (result.stderr or "")

        wall_ms = int((time.time() - start_time) * 1000)

        if result.returncode != 0 and not result.stdout.strip():
            return {
                "text": "",
                "model": model,
                "total_tokens": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "duration_ms": wall_ms,
                "cost": 0.0,
                "num_turns": 0,
                "stop_reason": "error",
                "error": result.stderr[:500] if result.stderr else f"exit code {result.returncode}",
            }

        # Parse JSON output from claude
        return _parse_claude_output(result.stdout, model, wall_ms)

    finally:
        prompt_file.unlink(missing_ok=True)


def _parse_claude_output(raw: str, model: str, wall_ms: int) -> dict:
    """Parse claude --output-format json output."""
    try:
        data = json.loads(raw)

        # --verbose returns a list of events; result is the last element
        if isinstance(data, list):
            data = next((d for d in reversed(data) if isinstance(d, dict) and d.get("type") == "result"), data[-1])

        # Usage is nested under data["usage"]
        usage = data.get("usage", {})
        if isinstance(usage, list):
            usage = usage[0] if usage else {}

        input_tokens = usage.get("input_tokens", 0)
        output_tokens = usage.get("output_tokens", 0)
        cache_creation = usage.get("cache_creation_input_tokens", 0)
        cache_read = usage.get("cache_read_input_tokens", 0)
        total_tokens = input_tokens + output_tokens + cache_creation + cache_read

        # Model may be in modelUsage keys
        model_usage = data.get("modelUsage", {})
        model_used = list(model_usage.keys())[0] if model_usage else data.get("model", model)

        # Per-model usage details
        model_detail = {}
        if model_usage:
            mk = list(model_usage.keys())[0]
            md = model_usage[mk]
            model_detail = {
                "context_window": md.get("contextWindow", 0),
                "max_output_tokens": md.get("maxOutputTokens", 0),
                "web_search_requests": md.get("webSearchRequests", 0),
            }

        # Cache breakdown
        cache_detail = usage.get("cache_creation", {})

        return {
            # Core
            "provider": "claude",
            "text": data.get("result", ""),
            "model": model_used,
            "stop_reason": data.get("stop_reason", "end_turn"),
            "is_error": data.get("is_error", False),
            # Tokens
            "total_tokens": total_tokens,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_creation_tokens": cache_creation,
            "cache_read_tokens": cache_read,
            "cache_creation_1h": cache_detail.get("ephemeral_1h_input_tokens", 0),
            "cache_creation_5m": cache_detail.get("ephemeral_5m_input_tokens", 0),
            # Timing
            "duration_ms": data.get("duration_ms", wall_ms),
            "duration_api_ms": data.get("duration_api_ms", 0),
            # Cost & billing
            "cost": data.get("total_cost_usd", 0.0),
            "service_tier": usage.get("service_tier", ""),
            "speed": usage.get("speed", ""),
            # Session
            "num_turns": data.get("num_turns", 1),
            "session_id": data.get("session_id", ""),
            "uuid": data.get("uuid", ""),
            "fast_mode": data.get("fast_mode_state", "off"),
            # Server tool use
            "web_search_requests": usage.get("server_tool_use", {}).get("web_search_requests", 0),
            "web_fetch_requests": usage.get("server_tool_use", {}).get("web_fetch_requests", 0),
            # Model capacity
            **model_detail,
            # Inference
            "inference_geo": usage.get("inference_geo", ""),
        }

    except json.JSONDecodeError:
        return {
            "provider": "claude",
            "text": raw[:8000],
            "model": model,
            "total_tokens": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "duration_ms": wall_ms,
            "cost": 0.0,
            "num_turns": 1,
            "stop_reason": "json_parse_error",
            "is_error": True,
        }


# ── Gemini ──────────────────────────────────────────────────────────

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODELS = {
    "gemini-flash": "gemini-flash-latest",
    "gemini-pro": "gemini-pro-latest",
}

# Gemini rate-limit state — in-memory flag with cooldown
_gemini_capped = False
_gemini_capped_until = 0.0  # epoch timestamp
GEMINI_CAP_COOLDOWN_S = 60  # retry after 60s for RPM, longer for quota


def gemini_is_available() -> bool:
    """Check if Gemini is currently available (not rate-capped)."""
    global _gemini_capped, _gemini_capped_until
    if not _gemini_capped:
        return bool(GEMINI_API_KEY)
    if time.time() >= _gemini_capped_until:
        _gemini_capped = False
        log.info("Gemini cap cooldown expired, re-enabling")
        return bool(GEMINI_API_KEY)
    return False


def _gemini_set_capped(status_code: int):
    """Flag Gemini as capped after a 429 or 403."""
    global _gemini_capped, _gemini_capped_until
    _gemini_capped = True
    if status_code == 429:
        # RPM/TPM limit — short cooldown
        _gemini_capped_until = time.time() + GEMINI_CAP_COOLDOWN_S
        log.warning(f"Gemini rate-limited (429), cooling down for {GEMINI_CAP_COOLDOWN_S}s")
    else:
        # 403 = quota exhausted — longer cooldown (1 hour)
        _gemini_capped_until = time.time() + 3600
        log.warning("Gemini quota exhausted (403), cooling down for 1h")


@register_action("llm.call_gemini")
def action_llm_call_gemini(params: dict, ctx: RunContext) -> dict:
    """Call Gemini API directly via HTTP. Zero overhead — no CLI, no system prompt."""
    import httpx

    model_key = params.get("model", "gemini-flash")
    prompt = params.get("prompt", "")
    budget = params.get("budget", {})

    if budget.get("status") == "blocked":
        return {
            "provider": "gemini",
            "text": "",
            "model": model_key,
            "total_tokens": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "thinking_tokens": 0,
            "duration_ms": 0,
            "cost": 0.0,
            "stop_reason": "budget_blocked",
            "is_error": True,
            "error": budget.get("reason", "Budget blocked"),
        }

    if ctx.dry_run:
        return {
            "provider": "gemini",
            "text": "[DRY RUN] No Gemini call made",
            "model": model_key,
            "total_tokens": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "thinking_tokens": 0,
            "duration_ms": 0,
            "cost": 0.0,
            "stop_reason": "dry_run",
            "is_error": False,
        }

    if not GEMINI_API_KEY:
        return {
            "provider": "gemini",
            "text": "",
            "model": model_key,
            "total_tokens": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "thinking_tokens": 0,
            "duration_ms": 0,
            "cost": 0.0,
            "stop_reason": "error",
            "is_error": True,
            "error": "GEMINI_API_KEY not set in environment",
        }

    model_id = GEMINI_MODELS.get(model_key, model_key)
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_id}:generateContent"

    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
    }

    log.info(f"  llm.call_gemini: model={model_id}")
    start_time = time.time()

    try:
        resp = httpx.post(
            url,
            json=payload,
            headers={
                "Content-Type": "application/json",
                "X-goog-api-key": GEMINI_API_KEY,
            },
            timeout=120.0,
        )
        wall_ms = int((time.time() - start_time) * 1000)

        if resp.status_code in (429, 403):
            _gemini_set_capped(resp.status_code)
            return {
                "provider": "gemini",
                "text": "",
                "model": model_id,
                "total_tokens": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "thinking_tokens": 0,
                "duration_ms": wall_ms,
                "cost": 0.0,
                "stop_reason": "rate_limited",
                "is_error": True,
                "gemini_capped": True,
                "error": f"HTTP {resp.status_code}: {resp.text[:300]}",
            }

        if resp.status_code != 200:
            return {
                "provider": "gemini",
                "text": "",
                "model": model_id,
                "total_tokens": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "thinking_tokens": 0,
                "duration_ms": wall_ms,
                "cost": 0.0,
                "stop_reason": "error",
                "is_error": True,
                "error": f"HTTP {resp.status_code}: {resp.text[:500]}",
            }

        data = resp.json()
        return _parse_gemini_output(data, model_id, wall_ms)

    except Exception as e:
        wall_ms = int((time.time() - start_time) * 1000)
        return {
            "provider": "gemini",
            "text": "",
            "model": model_id,
            "total_tokens": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "thinking_tokens": 0,
            "duration_ms": wall_ms,
            "cost": 0.0,
            "stop_reason": "error",
            "is_error": True,
            "error": str(e),
        }


def _parse_gemini_output(data: dict, model: str, wall_ms: int) -> dict:
    """Parse Gemini API response."""
    # Extract text from candidates
    candidates = data.get("candidates", [])
    text = ""
    finish_reason = ""
    if candidates:
        parts = candidates[0].get("content", {}).get("parts", [])
        text = "".join(p.get("text", "") for p in parts)
        finish_reason = candidates[0].get("finishReason", "")

    # Usage metadata
    usage = data.get("usageMetadata", {})
    input_tokens = usage.get("promptTokenCount", 0)
    output_tokens = usage.get("candidatesTokenCount", 0)
    thinking_tokens = usage.get("thoughtsTokenCount", 0)
    total_tokens = usage.get("totalTokenCount", 0)

    # Per-modality breakdown
    modality_breakdown = {}
    for detail in usage.get("promptTokensDetails", []):
        modality = detail.get("modality", "unknown").lower()
        modality_breakdown[modality] = detail.get("tokenCount", 0)

    return {
        # Core
        "provider": "gemini",
        "text": text,
        "model": data.get("modelVersion", model),
        "stop_reason": finish_reason,
        "is_error": False,
        # Tokens
        "total_tokens": total_tokens,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "thinking_tokens": thinking_tokens,
        "modality_breakdown": modality_breakdown,
        # Timing
        "duration_ms": wall_ms,
        # Cost (Gemini free tier / monthly credits — track for accounting)
        "cost": 0.0,
        # Gemini-specific
        "response_id": data.get("responseId", ""),
    }


# ── Codex CLI (OpenAI) ─────────────────────────────────────────────

@register_action("llm.call_codex")
def action_llm_call_codex(params: dict, ctx: RunContext) -> dict:
    """Call OpenAI via Codex CLI (codex exec --json). JSONL output parsed for full metrics."""
    model = params.get("model", "")  # empty = default (ChatGPT account default)
    prompt = params.get("prompt", "")
    budget = params.get("budget", {})
    cwd = params.get("cwd_override", os.environ.get("CODEX_SANDBOX_DIR", str(Path.home() / "flow-sandbox")))

    if budget.get("status") == "blocked":
        return {
            "provider": "codex",
            "text": "",
            "model": model or "default",
            "total_tokens": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "cached_input_tokens": 0,
            "duration_ms": 0,
            "cost": 0.0,
            "stop_reason": "budget_blocked",
            "is_error": True,
            "error": budget.get("reason", "Budget blocked"),
        }

    if ctx.dry_run:
        return {
            "provider": "codex",
            "text": "[DRY RUN] No Codex call made",
            "model": model or "default",
            "total_tokens": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "cached_input_tokens": 0,
            "duration_ms": 0,
            "cost": 0.0,
            "stop_reason": "dry_run",
            "is_error": False,
        }

    # Write prompt to temp file
    import tempfile
    prompt_file = Path(tempfile.mktemp(suffix=".txt", prefix="flow_codex_"))
    prompt_file.write_text(prompt, encoding="utf-8")

    try:
        cmd_parts = [
            f'cat "{prompt_file}" | codex exec --json --full-auto --skip-git-repo-check',
            f'-C "{cwd}"',
        ]
        if model:
            cmd_parts.append(f'-m {model}')

        cmd = " ".join(cmd_parts)

        log.info(f"  llm.call_codex: model={model or 'default'}")
        start_time = time.time()

        result = subprocess.run(
            ["bash", "-c", cmd],
            capture_output=True,
            timeout=300,
            cwd=cwd,
        )
        result.stdout = result.stdout.decode("utf-8", errors="replace") if isinstance(result.stdout, bytes) else (result.stdout or "")
        result.stderr = result.stderr.decode("utf-8", errors="replace") if isinstance(result.stderr, bytes) else (result.stderr or "")

        wall_ms = int((time.time() - start_time) * 1000)

        return _parse_codex_output(result.stdout, model, wall_ms)

    except subprocess.TimeoutExpired:
        wall_ms = int((time.time() - start_time) * 1000)
        return {
            "provider": "codex",
            "text": "",
            "model": model or "default",
            "total_tokens": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "cached_input_tokens": 0,
            "duration_ms": wall_ms,
            "cost": 0.0,
            "stop_reason": "timeout",
            "is_error": True,
            "error": "Codex exec timed out after 300s",
        }
    except Exception as e:
        return {
            "provider": "codex",
            "text": "",
            "model": model or "default",
            "total_tokens": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "cached_input_tokens": 0,
            "duration_ms": int((time.time() - start_time) * 1000),
            "cost": 0.0,
            "stop_reason": "error",
            "is_error": True,
            "error": str(e),
        }
    finally:
        prompt_file.unlink(missing_ok=True)


def _parse_codex_output(raw: str, model: str, wall_ms: int) -> dict:
    """Parse Codex CLI JSONL output (one JSON object per line)."""
    events = []
    for line in raw.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue

    if not events:
        return {
            "provider": "codex",
            "text": raw[:2000],
            "model": model or "default",
            "total_tokens": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "cached_input_tokens": 0,
            "duration_ms": wall_ms,
            "cost": 0.0,
            "stop_reason": "no_events",
            "is_error": True,
        }

    # Extract data from events
    thread_id = ""
    text_parts = []
    input_tokens = 0
    output_tokens = 0
    cached_input_tokens = 0
    turn_count = 0
    errors = []
    items = []

    for evt in events:
        evt_type = evt.get("type", "")

        if evt_type == "thread.started":
            thread_id = evt.get("thread_id", "")

        elif evt_type == "item.completed":
            item = evt.get("item", {})
            items.append(item)
            if item.get("type") == "agent_message" and item.get("text"):
                text_parts.append(item["text"])

        elif evt_type == "turn.completed":
            turn_count += 1
            usage = evt.get("usage", {})
            input_tokens += usage.get("input_tokens", 0)
            output_tokens += usage.get("output_tokens", 0)
            cached_input_tokens += usage.get("cached_input_tokens", 0)

        elif evt_type == "turn.failed":
            err = evt.get("error", {})
            errors.append(err.get("message", str(err)))

        elif evt_type == "error":
            errors.append(evt.get("message", "Unknown error"))

    total_tokens = input_tokens + output_tokens
    text = "\n".join(text_parts)
    is_error = len(errors) > 0

    return {
        # Core
        "provider": "codex",
        "text": text,
        "model": model or "default",
        "stop_reason": "error" if is_error else "end_turn",
        "is_error": is_error,
        # Tokens
        "total_tokens": total_tokens,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cached_input_tokens": cached_input_tokens,
        # Timing
        "duration_ms": wall_ms,
        # Cost (ChatGPT subscription — tracked for accounting)
        "cost": 0.0,
        # Session
        "thread_id": thread_id,
        "turn_count": turn_count,
        "item_count": len(items),
        # Errors
        "errors": errors if errors else None,
        # Raw event count for debugging
        "event_count": len(events),
    }


# ── Routing Scorecard (automatic, zero-cost signals) ─────────────

@register_action("routing.scorecard")
def action_routing_scorecard(params: dict, ctx: RunContext) -> dict:
    """Compute objective routing quality signals from the LLM result.

    No LLM call — pure math on data we already have.
    """
    llm_result = params.get("llm_result", {})
    routing = params.get("routing", {})
    assessment = params.get("assessment", {})

    input_tokens = llm_result.get("input_tokens", 0)
    output_tokens = llm_result.get("output_tokens", 0)
    is_error = llm_result.get("is_error", False)
    stop_reason = llm_result.get("stop_reason", "")

    # Engagement ratio: did the model actually produce meaningful output?
    engagement_ratio = (output_tokens / input_tokens) if input_tokens > 0 else 0.0

    # Signals
    call_succeeded = not is_error and stop_reason not in ("error", "budget_blocked", "rate_limited", "timeout")
    produced_output = output_tokens > 50  # more than a refusal
    engaged = engagement_ratio > 0.02     # at least 2% ratio
    fell_back = bool(routing.get("fallback_from"))

    # Composite score: 0-4 (each signal is worth 1 point)
    score = sum([call_succeeded, produced_output, engaged, not fell_back])

    # Suspect misroute if the model produced almost nothing or errored
    likely_misrouted = call_succeeded and not produced_output

    scorecard = {
        "score": score,
        "max_score": 4,
        "call_succeeded": call_succeeded,
        "produced_output": produced_output,
        "engaged": engaged,
        "engagement_ratio": round(engagement_ratio, 4),
        "fell_back": fell_back,
        "likely_misrouted": likely_misrouted,
        "provider": routing.get("provider", "unknown"),
        "model": llm_result.get("model", "unknown"),
        "complexity": assessment.get("complexity", "unknown"),
        "cost_rank": routing.get("cost_rank", -1),
    }

    log.info(f"  routing.scorecard: score={score}/4, "
             f"provider={scorecard['provider']}, "
             f"engaged={engaged}, "
             f"misrouted={likely_misrouted}")

    return scorecard


# ── Eval: LLM-as-Judge (on-demand, end-of-week) ──────────────────
#
# Not run automatically. Triggered via:
#   uv run flow-run eval-routing.yaml
# when the week is closing and there's usage budget left to burn.

EVAL_PROMPT_TEMPLATE = """You are evaluating whether an AI model was a good fit for a task.

## Task given to the model
{task_summary}

## Model used
Provider: {provider}, Model: {model}
Complexity assessed as: {complexity}
Routing reason: {routing_reason}

## Model output (first 2000 chars)
{output_preview}

## Evaluation
Answer these three questions with ONLY the JSON format shown below. No other text.

```json
{{
  "quality": <1-5 integer, 1=useless 3=adequate 5=excellent>,
  "could_simpler_handle": <true if a cheaper/simpler model would produce equivalent results>,
  "was_misrouted": <true if this task needed a more capable model than was used>,
  "reasoning": "<one sentence explaining your rating>"
}}
```"""


@register_action("eval.judge")
def action_eval_judge(params: dict, ctx: RunContext) -> dict:
    """On-demand LLM-as-judge evaluation of routing quality.

    Run at end-of-week when usage budget has headroom. Evaluates flow_run
    records that have stored content (prompt_text + response_text from
    flow_run_content table).

    Tiered judging:
      - Gemini Pro as primary judge (free)
      - Opus as calibration judge (optional, set calibrate=true)
    """
    flow_runs = params.get("flow_runs", [])
    calibrate = params.get("calibrate", False)

    if not flow_runs:
        return {"evaluated": 0, "reason": "no flow_runs provided"}

    results = []

    for run in (flow_runs if isinstance(flow_runs, list) else []):
        run_id = run.get("id", "?")
        provider = run.get("provider", "?")
        model = run.get("model", "?")
        flow_name = run.get("flow_name", "?")

        # Full content from flow_run_content join
        prompt_text = run.get("prompt_text", "")
        response_text = run.get("response_text", "")

        if not response_text:
            log.info(f"  eval.judge: skipping run {run_id} — no stored content")
            continue

        # Use the prompt as the task summary (first 1000 chars),
        # and the response as the output to judge
        eval_prompt = EVAL_PROMPT_TEMPLATE.format(
            task_summary=prompt_text[:1000],
            provider=provider,
            model=model,
            complexity=run.get("complexity", "unknown"),
            routing_reason=run.get("routing_reason", "unknown"),
            output_preview=response_text[:2000],
        )

        entry = {"run_id": run_id, "provider": provider, "model": model}

        # Gemini Pro eval (free)
        if gemini_is_available():
            log.info(f"  eval.judge: Gemini Pro evaluating run {run_id}")
            gemini_result = action_llm_call_gemini(
                {"model": "gemini-pro", "prompt": eval_prompt, "budget": {"status": "ok"}},
                ctx,
            )
            gemini_eval = _parse_eval_response(gemini_result.get("text", ""))
            gemini_eval["judge_model"] = "gemini-pro"
            gemini_eval["judge_tokens"] = gemini_result.get("total_tokens", 0)
            gemini_eval["judge_cost"] = 0.0
            entry["gemini_pro"] = gemini_eval
        else:
            log.warning(f"  eval.judge: Gemini unavailable for run {run_id}")

        # Opus calibration (optional)
        if calibrate and not ctx.dry_run:
            log.info(f"  eval.judge: Opus calibrating run {run_id}")
            opus_result = action_llm_call(
                {"model": "opus", "prompt": eval_prompt, "budget": {"status": "ok"},
                 "agent_id": ctx.agent_id},
                ctx,
            )
            opus_eval = _parse_eval_response(opus_result.get("text", ""))
            opus_eval["judge_model"] = "opus"
            opus_eval["judge_tokens"] = opus_result.get("total_tokens", 0)
            opus_eval["judge_cost"] = opus_result.get("cost", 0.0)
            entry["opus"] = opus_eval

        results.append(entry)

    return {
        "evaluated": len(results),
        "calibrated": calibrate,
        "results": results,
    }


@register_action("eval.store")
def action_eval_store(params: dict, ctx: RunContext) -> dict:
    """Store eval results to routing_eval table."""
    eval_results = params.get("results", {})
    entries = eval_results.get("results", [])
    stored = 0

    for entry in entries:
        run_id = entry.get("run_id", "")
        if not run_id or run_id == "?":
            continue

        for judge_key in ("gemini_pro", "opus"):
            eval_data = entry.get(judge_key)
            if not eval_data:
                continue

            quality = eval_data.get("quality", -1)
            could_simpler = eval_data.get("could_simpler_handle")
            was_misrouted = eval_data.get("was_misrouted")
            reasoning = eval_data.get("reasoning", "")
            judge_model = eval_data.get("judge_model", judge_key)
            judge_tokens = eval_data.get("judge_tokens", 0)
            judge_cost = eval_data.get("judge_cost", 0.0)
            parse_error = eval_data.get("parse_error", False)

            could_simpler_sql = str(could_simpler).lower() if could_simpler is not None else "NONE"
            was_misrouted_sql = str(was_misrouted).lower() if was_misrouted is not None else "NONE"

            try:
                ctx.db.query(
                    f"CREATE routing_eval SET "
                    f"flow_run = {run_id}, "
                    f"judge_model = '{judge_model}', "
                    f"quality = {quality}, "
                    f"could_simpler_handle = {could_simpler_sql}, "
                    f"was_misrouted = {was_misrouted_sql}, "
                    f"reasoning = '{_esc(reasoning)}', "
                    f"judge_tokens = {judge_tokens}, "
                    f"judge_cost = {judge_cost}, "
                    f"parse_error = {str(parse_error).lower()};"
                )
                stored += 1
            except Exception as e:
                log.warning(f"  eval.store: failed to store eval for {run_id}/{judge_key}: {e}")

    return {"stored": stored, "total_entries": len(entries)}


def _parse_eval_response(text: str) -> dict:
    """Extract JSON eval from judge response."""
    # Try to find JSON block in response
    try:
        # Look for ```json ... ``` block
        import re
        json_match = re.search(r"```json\s*(\{.*?\})\s*```", text, re.DOTALL)
        if json_match:
            return json.loads(json_match.group(1))

        # Try parsing the whole thing as JSON
        # Strip any leading/trailing non-JSON
        stripped = text.strip()
        if stripped.startswith("{"):
            brace_depth = 0
            for i, c in enumerate(stripped):
                if c == "{":
                    brace_depth += 1
                elif c == "}":
                    brace_depth -= 1
                    if brace_depth == 0:
                        return json.loads(stripped[:i + 1])

    except (json.JSONDecodeError, Exception) as e:
        log.warning(f"  eval: failed to parse judge response: {e}")

    return {
        "quality": -1,
        "could_simpler_handle": None,
        "was_misrouted": None,
        "reasoning": f"Parse error: {text[:200]}",
        "parse_error": True,
    }


# ── Manifest: AST-based project skeleton ──────────────────────────

import ast
import re as _re


@register_action("manifest.build")
def action_manifest_build(params: dict, ctx: RunContext) -> dict:
    """Build a structural manifest of the project via AST parsing.

    Scans Python, YAML, SQL, TOML files and extracts classes, functions,
    decorators, flow steps, DB tables — no file contents, just structure.

    Returns {text: str, files: list, token_estimate: int}.
    """
    import yaml as _yaml

    paths = params.get("paths", ["."])
    root = Path(params.get("root", str(PROJECT_ROOT)))
    include_ext = set(params.get("extensions", [".py", ".yaml", ".yml", ".surql", ".toml", ".sh"]))
    exclude_dirs = set(params.get("exclude", ["__pycache__", ".venv", ".git", "node_modules", "data"]))

    lines = []
    file_index = []

    # Collect all matching files
    all_files = []
    for p in paths:
        scan = root / p
        if scan.is_file():
            all_files.append(scan)
        elif scan.is_dir():
            for f in sorted(scan.rglob("*")):
                if f.is_file() and f.suffix in include_ext:
                    if not any(ex in f.parts for ex in exclude_dirs):
                        all_files.append(f)

    for filepath in all_files:
        rel = filepath.relative_to(root)
        total_lines = len(filepath.read_text(encoding="utf-8", errors="replace").splitlines())
        file_entry = {"path": str(rel), "lines": total_lines, "type": filepath.suffix}

        if filepath.suffix == ".py":
            skel = _skeleton_python(filepath)
            lines.append(f"{rel} ({total_lines} lines)")
            for s in skel:
                lines.append(s)
            lines.append("")
            file_entry["symbols"] = len(skel)

        elif filepath.suffix in (".yaml", ".yml"):
            try:
                data = _yaml.safe_load(filepath.read_text(encoding="utf-8"))
                if isinstance(data, dict) and "steps" in data:
                    steps = data["steps"]
                    step_list = ", ".join(s["id"] + ":" + s["action"] for s in steps)
                    desc = data.get("description", "").strip()[:80]
                    lines.append(f"{rel} -- {data.get('flow', rel.stem)} ({len(steps)} steps: {step_list})")
                    if desc:
                        lines.append(f"  {desc}")
                    file_entry["flow_name"] = data.get("flow", "")
                    file_entry["step_count"] = len(steps)
                else:
                    lines.append(f"{rel} ({total_lines} lines, config)")
            except Exception:
                lines.append(f"{rel} ({total_lines} lines)")
            lines.append("")

        elif filepath.suffix == ".surql":
            tables = _extract_surql_tables(filepath)
            lines.append(f"{rel} -- tables: {', '.join(tables) if tables else 'none'}")
            lines.append("")
            file_entry["tables"] = tables

        elif filepath.suffix == ".toml":
            skel = _skeleton_toml(filepath)
            lines.append(f"{rel} ({total_lines} lines)")
            for s in skel:
                lines.append(s)
            lines.append("")

        else:
            lines.append(f"{rel} ({total_lines} lines)")
            lines.append("")

        file_index.append(file_entry)

    text = "\n".join(lines)
    token_est = len(text) // 4  # rough estimate

    log.info(f"  manifest.build: {len(file_index)} files, ~{token_est} tokens")

    return {
        "text": text,
        "files": file_index,
        "file_count": len(file_index),
        "token_estimate": token_est,
    }


def _skeleton_python(filepath: Path) -> list[str]:
    """Extract structural skeleton from a Python file via AST."""
    try:
        source = filepath.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source)
    except SyntaxError:
        return ["  (syntax error, cannot parse)"]

    lines = []
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.ClassDef):
            bases = ", ".join(
                a.id if isinstance(a, ast.Name) else str(a) for a in node.bases
            )
            base_str = f"({bases})" if bases else ""
            lines.append(f"  L{node.lineno:4d}  class {node.name}{base_str}")
            for child in ast.iter_child_nodes(node):
                if isinstance(child, ast.FunctionDef):
                    args = ", ".join(a.arg for a in child.args.args)
                    lines.append(f"  L{child.lineno:4d}    def {child.name}({args})")

        elif isinstance(node, ast.FunctionDef):
            args = ", ".join(a.arg for a in node.args.args)
            deco = ""
            for d in node.decorator_list:
                if isinstance(d, ast.Call) and isinstance(d.func, ast.Name):
                    if d.args and isinstance(d.args[0], ast.Constant):
                        deco = f'@{d.func.id}("{d.args[0].value}") '
                elif isinstance(d, ast.Name):
                    deco = f"@{d.id} "
            doc = ""
            if (node.body and isinstance(node.body[0], ast.Expr)
                    and isinstance(node.body[0].value, ast.Constant)):
                doc = node.body[0].value.value.split("\n")[0].strip()
                doc = f"  -- {doc}"
            lines.append(f"  L{node.lineno:4d}  {deco}def {node.name}({args}){doc}")

        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id.isupper():
                    lines.append(f"  L{node.lineno:4d}  {target.id} = ...")

    return lines


def _extract_surql_tables(filepath: Path) -> list[str]:
    """Extract DEFINE TABLE names from SurrealQL."""
    text = filepath.read_text(encoding="utf-8", errors="replace")
    tables = []
    for line in text.splitlines():
        m = _re.match(r"DEFINE TABLE\s+(\w+)", line)
        if m:
            tables.append(m.group(1))
    return tables


def _skeleton_toml(filepath: Path) -> list[str]:
    """Extract section headers and key fields from TOML."""
    lines = []
    for raw_line in filepath.read_text(encoding="utf-8", errors="replace").splitlines():
        stripped = raw_line.strip()
        if stripped.startswith("["):
            lines.append(f"  {stripped}")
        elif "=" in stripped and not stripped.startswith("#"):
            key = stripped.split("=")[0].strip()
            val = stripped.split("=", 1)[1].strip()[:60]
            lines.append(f"    {key} = {val}")
    return lines


# ── File Operations ───────────────────────────────────────────────

@register_action("file.read")
def action_file_read(params: dict, ctx: RunContext) -> dict:
    """Read one or more files and return their contents.

    Params:
        paths: list of file paths (relative to project root)
        line_range: optional {start, end} to read a slice
    """
    paths = params.get("paths", [])
    root = Path(params.get("root", str(PROJECT_ROOT)))
    line_range = params.get("line_range")

    if isinstance(paths, str):
        paths = [paths]

    results = {}
    total_chars = 0

    for p in paths:
        filepath = root / p
        if not filepath.exists():
            results[p] = {"error": f"not found: {filepath}"}
            continue

        text = filepath.read_text(encoding="utf-8", errors="replace")

        if line_range:
            file_lines = text.splitlines()
            start = line_range.get("start", 1) - 1
            end = line_range.get("end", len(file_lines))
            text = "\n".join(file_lines[start:end])

        results[p] = {
            "content": text,
            "lines": len(text.splitlines()),
            "chars": len(text),
        }
        total_chars += len(text)

    log.info(f"  file.read: {len(paths)} files, {total_chars} chars")

    return {
        "files": results,
        "file_count": len(results),
        "total_chars": total_chars,
        "token_estimate": total_chars // 4,
    }


@register_action("file.write")
def action_file_write(params: dict, ctx: RunContext) -> dict:
    """Write content to a file.

    Params:
        path: file path (relative to project root)
        content: string to write
        mode: "overwrite" (default) or "append"
    """
    path = params.get("path", "")
    content = params.get("content", "")
    mode = params.get("mode", "overwrite")
    root = Path(params.get("root", str(PROJECT_ROOT)))

    if not path:
        return {"error": "no path specified"}

    if ctx.dry_run:
        return {"path": path, "status": "dry_run", "chars": len(content)}

    filepath = root / path
    filepath.parent.mkdir(parents=True, exist_ok=True)

    if mode == "append":
        with open(filepath, "a", encoding="utf-8") as f:
            f.write(content)
    else:
        filepath.write_text(content, encoding="utf-8")

    log.info(f"  file.write: {path} ({len(content)} chars, mode={mode})")

    return {
        "path": path,
        "status": "written",
        "chars": len(content),
        "lines": len(content.splitlines()),
    }


# ── Context Selection ─────────────────────────────────────────────

@register_action("context.select")
def action_context_select(params: dict, ctx: RunContext) -> dict:
    """Given a manifest and an LLM's file selection, read only the relevant files.

    This is the glue between manifest.build → LLM planning → file.read.
    Accepts either a structured list or raw LLM text and extracts file paths.

    Params:
        manifest: output from manifest.build
        selection: list of file paths OR dict with "read"/"modify" keys
                   OR raw text containing file paths
    """
    manifest = params.get("manifest", {})
    selection = params.get("selection", [])
    root = Path(params.get("root", str(PROJECT_ROOT)))

    # Known file paths from manifest
    known_paths = {f["path"] for f in manifest.get("files", [])}

    # Extract paths from selection (flexible input)
    selected = set()

    if isinstance(selection, list):
        selected = set(selection)
    elif isinstance(selection, dict):
        for key in ("read", "modify", "files"):
            val = selection.get(key, [])
            if isinstance(val, list):
                selected.update(val)
            elif isinstance(val, dict):
                selected.update(val.keys())
    elif isinstance(selection, str):
        # Extract paths from raw text — look for anything matching known paths
        for kp in known_paths:
            if kp in selection:
                selected.add(kp)

    # Validate against manifest
    valid = selected & known_paths
    invalid = selected - known_paths

    if invalid:
        log.warning(f"  context.select: unknown paths ignored: {invalid}")

    # Read the selected files
    if valid:
        file_contents = action_file_read({"paths": list(valid), "root": str(root)}, ctx)
    else:
        file_contents = {"files": {}, "file_count": 0, "total_chars": 0, "token_estimate": 0}

    log.info(f"  context.select: {len(valid)} files selected, "
             f"~{file_contents.get('token_estimate', 0)} tokens")

    return {
        "selected_paths": sorted(valid),
        "invalid_paths": sorted(invalid),
        "files": file_contents.get("files", {}),
        "file_count": len(valid),
        "token_estimate": file_contents.get("token_estimate", 0),
    }
