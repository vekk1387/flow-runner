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

# Root of the agent-build repo
REPO_ROOT = Path(__file__).resolve().parent.parent


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

    # For agent_inbox, reshape into {messages: [...], tasks: [...]}
    if query_key == "agent_inbox" and len(results) == 2:
        return {"messages": results[0] or [], "tasks": results[1] or []}

    # For single-statement queries, unwrap
    if len(results) == 1:
        r = results[0]
        if isinstance(r, list) and len(r) == 1:
            return r[0]
        return r

    return results


@register_action("budget.check")
def action_budget_check(params: dict, ctx: RunContext) -> dict:
    """Check budget via the existing check-budget.sh script."""
    script = REPO_ROOT / ".agents" / "check-budget.sh"

    if ctx.dry_run:
        return {"status": "ok", "session_pct": 0, "weekly_pct": 0, "adaptive_cap": 100}

    if not script.exists():
        log.warning("check-budget.sh not found, returning ok")
        return {"status": "ok", "session_pct": 0, "weekly_pct": 0, "adaptive_cap": 100}

    try:
        result = subprocess.run(
            ["bash", str(script)],
            capture_output=True, text=True, timeout=15,
            cwd=str(REPO_ROOT),
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

    # Estimate complexity
    if total_items == 0:
        return {
            "complexity": "none",
            "model_tier": "fast",
            "estimated_tokens": 0,
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
        "reason": f"{task_count} tasks, {msg_count} messages, P1={has_p1}",
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

Work through your assigned tasks and messages. For each task:
1. Read the requirements carefully
2. Execute the work within your boundaries
3. Mark tasks complete when done (use complete-task.sh)
4. Send messages to other agents if you need handoffs

Start now."""

    return {
        "user_prompt": user_prompt,
        "model": model_flag,
        "model_tier": model_tier,
    }


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
        # cwd_override skips agent config and CLAUDE.md loading entirely
        cwd_override = params.get("cwd_override")

        # Read agent config for cwd and additional directories
        config_path = REPO_ROOT / ".agents" / _agent_dir(agent_id) / "config.json"
        cwd = str(REPO_ROOT)
        add_dirs = ""

        if cwd_override:
            cwd = cwd_override
            # No add_dirs when using override — clean environment
        elif config_path.exists():
            try:
                config = json.loads(config_path.read_text(encoding="utf-8"))
                session_cfg = config.get("session_config", {})
                if session_cfg.get("cwd"):
                    cwd = session_cfg["cwd"]
                for d in session_cfg.get("additional_directories", []):
                    add_dirs += f' --add-dir "{d}"'
            except Exception:
                pass

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


def _agent_dir(agent_id: str) -> str:
    """Map agent_id (e.g. 'sap', 'coord') to directory name."""
    dir_map = {
        "coord": "saruman", "sap": "radagast", "dotnet": "gandalf",
        "ui": "legolas", "data": "gimli", "qa": "aragorn",
        "devex": "elrond", "optim": "frodo", "docs": "bilbo",
        "ideas": "pippin", "architect": "merry", "finance": "theoden",
        "abap": "galadriel", "notes": "sam", "strategy": "faramir",
    }
    return dir_map.get(agent_id, agent_id)


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
    cwd = params.get("cwd_override", "C:/tmp/flow-sandbox")

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
