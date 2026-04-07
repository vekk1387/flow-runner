"""Quick-prompt entry point. Shortcut for running demo-prompt with a text prompt.

Usage:
    uv run flow-prompt "Explain how LLM routing works"
    uv run flow-prompt "Write a Python sort" --provider claude --model sonnet
    uv run flow-prompt "Summarize this data" --provider gemini
    uv run flow-prompt "Fix the bug in main.py" --dry-run
"""

from __future__ import annotations

import sys


def main():
    # Rewrite args to: flow-run demo-prompt --prompt "..."
    # Take the first positional arg as the prompt text
    args = sys.argv[1:]

    if not args or args[0].startswith("-"):
        print("Usage: flow-prompt 'your prompt here' [--provider P] [--model M] [--dry-run]")
        print("")
        print("Examples:")
        print("  flow-prompt 'Explain how AI works'")
        print("  flow-prompt 'Write a Python sort function' --provider claude")
        print("  flow-prompt 'Summarize this' --provider gemini")
        print("  flow-prompt 'Test prompt' --dry-run")
        sys.exit(1)

    prompt_text = args[0]
    remaining = args[1:]

    # Inject as flow-run args
    sys.argv = [
        "flow-run",
        "demo-prompt",
        "--prompt", prompt_text,
    ] + remaining

    from .cli import main as flow_main
    flow_main()


if __name__ == "__main__":
    main()
