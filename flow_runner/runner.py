"""Flow execution engine.

Loads a YAML flow, resolves template variables, executes steps sequentially,
and writes full audit trail to SurrealDB.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from .db import SurrealClient
from .models import FlowDefinition, FlowRun, FlowStatus, StepResult, StepStatus
from .steps import RunContext, get_action

log = logging.getLogger(__name__)

FLOWS_DIR = Path(os.environ.get("FLOWS_DIR", str(Path(__file__).resolve().parent.parent / ".flows")))


class FlowRunner:
    """Loads and executes YAML-defined flows with full auditability."""

    def __init__(self, db: SurrealClient, dry_run: bool = False):
        self.db = db
        self.dry_run = dry_run

    def load_flow(self, flow_path: str | Path) -> FlowDefinition:
        """Load a flow definition from YAML."""
        path = Path(flow_path)
        if not path.is_absolute():
            path = FLOWS_DIR / path

        if not path.exists():
            raise FileNotFoundError(f"Flow file not found: {path}")

        with open(path, encoding="utf-8") as f:
            raw = yaml.safe_load(f)

        return FlowDefinition.from_dict(raw)

    def run(
        self,
        flow_path: str | Path,
        agent_id: str,
        trigger: str = "manual",
        extra_context: dict[str, Any] | None = None,
    ) -> FlowRun:
        """Execute a flow end-to-end."""
        flow_def = self.load_flow(flow_path)
        yaml_hash = self._file_hash(flow_path)

        # Sync flow to DB
        flow_db_id = self._sync_flow_to_db(flow_def, yaml_hash)

        # Initialize run state
        run = FlowRun(
            flow_name=flow_def.name,
            agent_id=agent_id,
            trigger=trigger,
            flow_db_id=flow_db_id,
        )

        # Context holds all step outputs, keyed by step.output name
        context: dict[str, Any] = {"agent_id": agent_id, "trigger": trigger}
        if extra_context:
            context.update(extra_context)

        # Create flow_run record in DB
        run.db_id = self._create_flow_run(run, flow_db_id)

        ctx = RunContext(
            db=self.db,
            agent_id=agent_id,
            flow_run_db_id=run.db_id,
            dry_run=self.dry_run,
        )

        log.info(f"Starting flow: {flow_def.name} v{flow_def.version} for agent:{agent_id}")
        log.info(f"  Run ID: {run.db_id}")
        log.info(f"  Steps: {len(flow_def.steps)}")

        # Execute steps
        for seq, step in enumerate(flow_def.steps):
            step_result = StepResult(
                step_id=step.id,
                action=step.action,
                seq=seq,
                started_at=datetime.now(),
            )

            # Resolve template variables in params
            resolved_params = self._resolve_params(step.params, context)
            step_result.compute_input_hash(resolved_params)

            log.info(f"Step {seq}: {step.id} ({step.action})")

            # Log step start to DB
            step_log_id = self._log_step_start(run.db_id, step_result)

            try:
                action_fn = get_action(step.action)
                start = time.time()
                output = action_fn(resolved_params, ctx)
                elapsed_ms = int((time.time() - start) * 1000)

                step_result.status = StepStatus.OK
                step_result.ended_at = datetime.now()
                step_result.duration_ms = elapsed_ms
                step_result.output_data = output
                step_result.output_summary = _summarize(output)

                # Extract tokens/cost from any LLM call (llm.call, llm.call_gemini, etc.)
                if step.action.startswith("llm.") and isinstance(output, dict):
                    step_result.tokens = output.get("total_tokens", 0)
                    step_result.cost = output.get("cost", 0.0)
                    run.total_tokens += step_result.tokens or 0
                    run.total_cost += step_result.cost or 0.0

                    # Store full provider response (minus the text body to save space)
                    llm_meta = {k: v for k, v in output.items() if k != "text"}
                    step_result.llm_response = llm_meta

                    # Track provider at the run level
                    if output.get("provider"):
                        run.context["_provider"] = output["provider"]

                    # Store full prompt + response for eval/training
                    prompt_text = context.get("prompt_payload", {}).get("user_prompt", "") if isinstance(context.get("prompt_payload"), dict) else ""
                    response_text = output.get("text", "")
                    provider = output.get("provider", "unknown")
                    model_used = output.get("model", "unknown")
                    if prompt_text or response_text:
                        self._store_content(run.db_id, prompt_text, response_text, provider, model_used)

                    # Check for budget block or error
                    if output.get("stop_reason") == "budget_blocked":
                        step_result.status = StepStatus.ERROR
                        step_result.error = output.get("error", "Budget blocked")
                        run.steps.append(step_result)
                        self._log_step_end(step_log_id, step_result)
                        run.status = FlowStatus.CANCELLED
                        run.error = "Budget blocked"
                        break

                # Store output in context
                if step.output:
                    context[step.output] = output

                log.info(f"  -> {step_result.status.value} ({elapsed_ms}ms)")

            except Exception as e:
                step_result.status = StepStatus.ERROR
                step_result.ended_at = datetime.now()
                step_result.duration_ms = int((time.time() - start) * 1000)
                step_result.error = str(e)

                log.error(f"  -> ERROR: {e}")

                run.steps.append(step_result)
                self._log_step_end(step_log_id, step_result)

                run.status = FlowStatus.FAILED
                run.error = f"Step {step.id} failed: {e}"
                break

            run.steps.append(step_result)
            self._log_step_end(step_log_id, step_result)

        else:
            # All steps completed without break
            run.status = FlowStatus.COMPLETED

        # Finalize
        run.ended_at = datetime.now()
        run.duration_ms = int((run.ended_at - run.started_at).total_seconds() * 1000)
        run.context = {k: _summarize(v) for k, v in context.items() if k != "agent_id"}

        self._finalize_flow_run(run)

        log.info(f"Flow {flow_def.name} {run.status.value}: "
                 f"{run.duration_ms}ms, {run.total_tokens} tokens, ${run.total_cost:.4f}")

        return run

    # ── Template Resolution ─────────────────────────────────────────

    def _resolve_params(self, params: dict, context: dict) -> dict:
        """Replace {{var}} references with values from context."""
        resolved = {}
        for key, value in params.items():
            if isinstance(value, str):
                resolved[key] = self._resolve_string(value, context)
            elif isinstance(value, dict):
                resolved[key] = self._resolve_params(value, context)
            else:
                resolved[key] = value
        return resolved

    def _resolve_string(self, template: str, context: dict) -> Any:
        """Resolve a template string. If the entire string is one {{ref}}, return the raw value."""
        # Check if the entire string is a single reference
        match = re.fullmatch(r"\{\{(.+?)\}\}", template.strip())
        if match:
            return self._lookup(match.group(1), context)

        # Otherwise, do string interpolation
        def replacer(m):
            val = self._lookup(m.group(1), context)
            return str(val) if val is not None else ""

        return re.sub(r"\{\{(.+?)\}\}", replacer, template)

    def _lookup(self, path: str, context: dict) -> Any:
        """Resolve a dotted path like 'inbox.tasks' or 'inbox.tasks[0].id'."""
        parts = re.split(r"\.|(?=\[)", path.strip())
        current = context

        for part in parts:
            if not part:
                continue

            # Handle array index: [0], [1], etc.
            idx_match = re.match(r"\[(\d+)\]", part)
            if idx_match:
                idx = int(idx_match.group(1))
                if isinstance(current, list) and idx < len(current):
                    current = current[idx]
                else:
                    return None
                continue

            if isinstance(current, dict):
                current = current.get(part)
            else:
                return None

            if current is None:
                return None

        return current

    # ── DB Audit Trail ──────────────────────────────────────────────

    def _sync_flow_to_db(self, flow_def: FlowDefinition, yaml_hash: str) -> str:
        """Upsert flow definition to DB. Returns flow record ID."""
        # Check if flow exists
        existing = self.db.query_one(
            f"SELECT id FROM flow WHERE name = '{flow_def.name}' LIMIT 1;"
        )

        if existing and len(existing) > 0:
            flow_id = existing[0]["id"]
            self.db.query(
                f"UPDATE {flow_id} SET "
                f"version = {flow_def.version}, "
                f"description = '{_esc(flow_def.description)}', "
                f"trigger = '{flow_def.trigger}', "
                f"step_count = {len(flow_def.steps)}, "
                f"yaml_hash = '{yaml_hash}', "
                f"synced_at = time::now();"
            )
            return str(flow_id)
        else:
            result = self.db.query_one(
                f"CREATE flow SET "
                f"name = '{flow_def.name}', "
                f"version = {flow_def.version}, "
                f"description = '{_esc(flow_def.description)}', "
                f"trigger = '{flow_def.trigger}', "
                f"step_count = {len(flow_def.steps)}, "
                f"yaml_hash = '{yaml_hash}', "
                f"active = true;"
            )
            if result and len(result) > 0:
                return str(result[0]["id"])
            return "flow:unknown"

    def _create_flow_run(self, run: FlowRun, flow_db_id: str) -> str:
        """Insert flow_run record, return its ID."""
        result = self.db.query_one(
            f"CREATE flow_run SET "
            f"flow = {flow_db_id}, "
            f"flow_name = '{run.flow_name}', "
            f"agent = agent:{run.agent_id}, "
            f"trigger = '{run.trigger}', "
            f"status = 'running';"
        )
        if result and len(result) > 0:
            return str(result[0]["id"])
        return "flow_run:unknown"

    def _log_step_start(self, flow_run_id: str, step: StepResult) -> str:
        """Insert flow_step_log record at step start."""
        result = self.db.query_one(
            f"CREATE flow_step_log SET "
            f"flow_run = {flow_run_id}, "
            f"step_id = '{step.step_id}', "
            f"action = '{step.action}', "
            f"seq = {step.seq}, "
            f"status = 'running', "
            f"input_hash = '{step.input_hash}';"
        )
        if result and len(result) > 0:
            return str(result[0]["id"])
        return "flow_step_log:unknown"

    def _log_step_end(self, step_log_id: str, step: StepResult) -> None:
        """Update flow_step_log record with results."""
        error_clause = f", error = '{_esc(step.error)}'" if step.error else ""
        tokens_clause = f", tokens = {step.tokens}" if step.tokens is not None else ""
        cost_clause = f", cost = {step.cost}" if step.cost is not None else ""

        # Serialize full LLM response as JSON string for the DB
        llm_clause = ""
        if step.llm_response:
            llm_json = json.dumps(step.llm_response, default=str)
            llm_clause = f", llm_response = '{_esc(llm_json, max_len=4000)}'"

        self.db.query(
            f"UPDATE {step_log_id} SET "
            f"status = '{step.status.value}', "
            f"ended_at = time::now(), "
            f"duration_ms = {step.duration_ms}, "
            f"output_summary = '{_esc(step.output_summary)}'"
            f"{error_clause}{tokens_clause}{cost_clause}{llm_clause};"
        )

    def _store_content(self, flow_run_id: str, prompt: str, response: str,
                        provider: str, model: str) -> None:
        """Store full prompt + response text in flow_run_content for eval/training."""
        try:
            self.db.query(
                f"CREATE flow_run_content SET "
                f"flow_run = {flow_run_id}, "
                f"prompt_text = '{_esc(prompt, max_len=50000)}', "
                f"response_text = '{_esc(response, max_len=50000)}', "
                f"provider = '{provider}', "
                f"model = '{_esc(model)}';"
            )
        except Exception as e:
            log.warning(f"Failed to store flow_run_content: {e}")

    def _finalize_flow_run(self, run: FlowRun) -> None:
        """Update flow_run record with final state."""
        error_clause = f", error = '{_esc(run.error)}'" if run.error else ""
        provider = run.context.get("_provider", "")
        provider_clause = f", provider = '{provider}'" if provider else ""

        self.db.query(
            f"UPDATE {run.db_id} SET "
            f"status = '{run.status.value}', "
            f"ended_at = time::now(), "
            f"duration_ms = {run.duration_ms}, "
            f"total_tokens = {run.total_tokens}, "
            f"total_cost = {run.total_cost}"
            f"{error_clause}{provider_clause};"
        )

    def _file_hash(self, path: str | Path) -> str:
        p = Path(path)
        if not p.is_absolute():
            p = FLOWS_DIR / p
        content = p.read_bytes()
        return hashlib.sha256(content).hexdigest()[:16]


def _summarize(value: Any, max_len: int = 200) -> str:
    """Create a short summary string for audit logging."""
    if value is None:
        return "null"
    if isinstance(value, str):
        return value[:max_len]
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, dict):
        keys = list(value.keys())
        return f"dict({len(keys)} keys: {', '.join(keys[:5])})"
    if isinstance(value, list):
        return f"list({len(value)} items)"
    return str(value)[:max_len]


def _esc(s: str | None, max_len: int = 500) -> str:
    """Escape a string for SurrealQL."""
    if not s:
        return ""
    return s.replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n")[:max_len]
