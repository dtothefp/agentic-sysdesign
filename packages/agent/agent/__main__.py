"""CLI transport: stream one agent turn to stdout. The curl-free way to watch the loop.

    PROMPT='what creators do we track?' moon run agent:chat
    # or directly:
    uv run --package sysdesign-agent python -m agent "what creators do we track?"

Points at SYSDESIGN_API_URL (default http://localhost:8000), so run services/api first
(moon run api:dev) or export SYSDESIGN_API_URL=https://sysdesign.thedefrag.ai to talk to prod.
"""

from __future__ import annotations

import sys
from typing import Any

from .loop import run_agent


def _short(value: Any, n: int = 200) -> str:
    s = str(value)
    return s if len(s) <= n else s[:n] + "..."


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        print('usage: python -m agent "your question"', file=sys.stderr)
        return 2
    prompt = " ".join(argv)

    for ev in run_agent(prompt):
        kind = ev["type"]
        if kind == "text":
            print(ev["text"], end="", flush=True)
        elif kind == "tool_use":
            print(f"\n  → {ev['name']}({ev['input']})", flush=True)
        elif kind == "tool_result":
            status = "ok" if ev["ok"] else "ERR"
            print(f"  ← [{status}] {_short(ev['result'])}", flush=True)
        elif kind == "done":
            print(f"\n\n[done: {ev['stop_reason']} in {ev['turns']} turn(s)]", flush=True)
        elif kind == "error":
            print(f"\n[error] {ev['error']}", file=sys.stderr, flush=True)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
