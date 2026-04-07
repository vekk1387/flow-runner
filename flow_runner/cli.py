"""CLI entry point for the flow runner.

Usage:
    uv run flow-prompt "Explain how LLM routing works"
    uv run flow-prompt "Write a Python sort function" --provider claude --model sonnet
    uv run flow-run demo-prompt.yaml --dry-run
    uv run flow-run agent-receive-work-auto.yaml --agent sap
    uv run flow-run --list
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

from .db import SurrealClient
from .runner import FlowRunner, FLOWS_DIR


def main():
    parser = argparse.ArgumentParser(
        prog="flow-run",
        description="Execute YAML-configured flows with multi-provider LLM routing",
    )
    parser.add_argument("flow", nargs="?", help="Flow YAML filename (from .flows/)")
    parser.add_argument("--agent", "-a", help="Agent ID (default: 'default')")
    parser.add_argument("--provider", "-P", help="Override provider (gemini, codex, claude)")
    parser.add_argument("--trigger", "-t", default="manual", help="Trigger type (default: manual)")
    parser.add_argument("--dry-run", action="store_true", help="Run without LLM calls")
    parser.add_argument("--model", "-m", help="Override model selection (e.g. opus, sonnet, haiku, gemini-flash)")
    parser.add_argument("--prompt", "-p", help="Prompt text (used with demo-prompt flow)")
    parser.add_argument("--cwd", help="Override working directory for LLM call")
    parser.add_argument("--list", action="store_true", help="List available flows")
    parser.add_argument("--inspect", action="store_true", help="Show flow details without executing")
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose logging")
    parser.add_argument("--db-host", default=os.environ.get("SURREAL_HOST", "http://localhost:8282"), help="SurrealDB host")

    args = parser.parse_args()

    # Setup logging
    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-5s %(message)s",
        datefmt="%H:%M:%S",
    )

    # List flows
    if args.list:
        _list_flows()
        return

    if not args.flow:
        parser.error("Flow filename required (or use --list)")

    flow_path = args.flow
    if not flow_path.endswith((".yaml", ".yml")):
        flow_path += ".yaml"

    # Inspect mode
    if args.inspect:
        _inspect_flow(flow_path)
        return

    # Run mode — default agent to "default" if not provided
    if not args.agent:
        args.agent = "default"

    db = SurrealClient(host=args.db_host)
    runner = FlowRunner(db=db, dry_run=args.dry_run)

    extra_context = {}
    if args.model:
        extra_context["model_override"] = args.model
    if args.cwd:
        extra_context["cwd_override"] = args.cwd
    if args.provider:
        extra_context["provider_override"] = args.provider
    if args.prompt:
        extra_context["prompt_text"] = args.prompt

    try:
        result = runner.run(
            flow_path=flow_path,
            agent_id=args.agent,
            trigger=args.trigger,
            extra_context=extra_context or None,
        )

        # Output summary
        print()
        print("=" * 60)
        print(f"Flow:     {result.flow_name}")
        print(f"Agent:    {result.agent_id}")
        print(f"Status:   {result.status.value}")
        print(f"Duration: {result.duration_ms}ms")
        print(f"Tokens:   {result.total_tokens}")
        print(f"Cost:     ${result.total_cost:.4f}")
        print(f"Run ID:   {result.db_id}")
        print("-" * 60)
        for step in result.steps:
            status_icon = {"ok": "+", "error": "!", "skipped": "-", "running": "?"}
            icon = status_icon.get(step.status.value, "?")
            tokens_str = f" [{step.tokens}tok]" if step.tokens else ""
            print(f"  [{icon}] {step.step_id:24s} {step.duration_ms:>6d}ms  {step.action}{tokens_str}")
            if step.error:
                print(f"      ERROR: {step.error}")
        print("=" * 60)

        if result.error:
            print(f"\nFlow error: {result.error}")
            sys.exit(1)

    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        logging.exception("Flow execution failed")
        sys.exit(1)
    finally:
        db.close()


def _list_flows():
    """List all YAML flows in .flows/ directory."""
    if not FLOWS_DIR.exists():
        print("No .flows/ directory found")
        return

    import yaml

    flows = sorted(FLOWS_DIR.glob("*.yaml")) + sorted(FLOWS_DIR.glob("*.yml"))
    if not flows:
        print("No flow files found in .flows/")
        return

    print(f"Available flows ({len(flows)}):")
    print()
    for f in flows:
        try:
            with open(f, encoding="utf-8") as fh:
                data = yaml.safe_load(fh)
            name = data.get("flow", f.stem)
            desc = data.get("description", "").strip()[:60]
            steps = len(data.get("steps", []))
            trigger = data.get("trigger", "manual")
            print(f"  {f.name:35s}  {name:25s}  {steps} steps  trigger={trigger}")
            if desc:
                print(f"  {'':35s}  {desc}")
        except Exception as e:
            print(f"  {f.name:35s}  ERROR: {e}")
    print()


def _inspect_flow(flow_path: str):
    """Show detailed flow structure."""
    import yaml

    path = Path(flow_path)
    if not path.is_absolute():
        path = FLOWS_DIR / path

    if not path.exists():
        print(f"Not found: {path}", file=sys.stderr)
        sys.exit(1)

    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)

    print(f"Flow: {data.get('flow')}")
    print(f"Version: {data.get('version', 1)}")
    print(f"Trigger: {data.get('trigger', 'manual')}")
    print(f"Description: {data.get('description', '').strip()}")
    print()
    print("Steps:")
    for i, step in enumerate(data.get("steps", [])):
        print(f"  {i}. [{step['id']}]")
        print(f"     action: {step['action']}")
        if step.get("params"):
            for k, v in step["params"].items():
                v_str = str(v)[:60]
                print(f"     param.{k}: {v_str}")
        if step.get("output"):
            print(f"     -> output: {step['output']}")
        if step.get("audit"):
            print(f"     audit: {step['audit']}")
        print()


if __name__ == "__main__":
    main()
