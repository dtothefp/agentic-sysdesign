"""Laptop demo runner for the Module 5 digest agent: start one session, watch it live.

SUPERSEDED for real runs by POST /digests, which enqueues the same logic as a
Celery task (worker/tasks.py run_digest_session). This script stays as the
teaching artifact and local debugging loop: same session, same stream, but the
transcript prints to your terminal and the digest downloads to output/.

The control plane (apply.sh + the YAML files) made the durable objects once.
This script is the per-run side: create a session pinned to the stored agent
version, mount the memory store, attach the vault, send one kickoff message,
and stream the event feed to the terminal.

It is also the TOOL SERVER. The agent's get_rated_signals tool has no code on
Anthropic's side, just a schema. When the agent calls it, the session emits
agent.custom_tool_use and goes idle; this process (already holding the SSE
stream) runs the real query against Supabase and sends the rows back as a
user.custom_tool_result event. Database credentials never leave this machine,
the agent only ever sees result rows. Same inversion of control as an SQS
request/response pair.

Usage, from backend/:
    uv run python m5_agents/run_digest.py                      # against prod
    uv run python m5_agents/run_digest.py --base-url https://pr-N-....up.railway.app
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

# Run as a plain script, `python m5_agents/run_digest.py` puts m5_agents/ on the
# import path, not backend/. Put backend/ there so common/ imports resolve.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from common.env import load_local_env

load_local_env()  # ANTHROPIC_API_KEY + DATABASE_URL_SUPABASE from backend/.env

import os

import anthropic

HERE = Path(__file__).resolve().parent
RESOURCES = json.loads((HERE / "resources.json").read_text())
OUT_DIR = HERE / "output"  # gitignored, downloaded session outputs land here


# --- the custom tool, executed host-side -----------------------------------------

def get_rated_signals(days: int = 7, min_relevance: float = 0.5) -> str:
    """The join the API doesn't offer: ratings + their source posts. The query lives in
    common/digests.py (shared with the worker task); the explicit Supabase DSN is because
    this laptop's default DATABASE_URL is the local drill db, which has no real ratings."""
    from common.digests import get_rated_signals as query

    rows = query(days, min_relevance, dsn=os.environ["DATABASE_URL_SUPABASE"])
    return json.dumps(rows, default=str)


CUSTOM_TOOLS = {"get_rated_signals": get_rated_signals}


def run_custom_tool(name: str, tool_input: dict) -> str:
    fn = CUSTOM_TOOLS.get(name)
    if fn is None:
        return f"unknown tool: {name}"
    try:
        return fn(**tool_input)
    except Exception as e:  # the agent should hear about failures, not hang
        return f"tool error: {e}"


# --- the session ------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="https://sysdesign.thedefrag.ai",
                    help="API base URL the agent should target (a PR preview domain, or prod)")
    args = ap.parse_args()

    client = anthropic.Anthropic()

    session = client.beta.sessions.create(
        agent={
            "type": "agent",
            "id": RESOURCES["agent_id"],
            "version": RESOURCES["agent_version"],  # pinned: an update mid-run can't change behavior
        },
        environment_id=RESOURCES["environment_id"],
        vault_ids=[RESOURCES["vault_id"]],  # X-API-Key substituted at egress, never visible inside
        title=f"weekly digest {date.today().isoformat()}",
        resources=[
            {
                "type": "memory_store",
                "memory_store_id": RESOURCES["memory_store_id"],
                "access": "read_write",
                "instructions": (
                    "Previous weekly digests, one dated file per week. Read the most "
                    "recent one before writing this week's, then save a dated copy of "
                    "the new digest here."
                ),
            }
        ],
    )
    print(f"session: {session.id}")
    print(f"trace:   https://platform.claude.com/workspaces/default/sessions/{session.id}\n")

    # Stream-first: open the SSE stream BEFORE sending the kickoff, or the first
    # events race past an unattached consumer.
    with client.beta.sessions.events.stream(session_id=session.id) as stream:
        client.beta.sessions.events.send(
            session_id=session.id,
            events=[{
                "type": "user.message",
                "content": [{
                    "type": "text",
                    "text": f"Write this week's digest. API base URL: {args.base_url}",
                }],
            }],
        )

        for event in stream:
            if event.type == "agent.message":
                for block in event.content:
                    if block.type == "text":
                        print(block.text, flush=True)

            elif event.type == "agent.tool_use":
                # sandbox-side tools (bash, read, write...), shown for visibility
                detail = json.dumps(getattr(event, "input", None) or {})[:160]
                print(f"  [tool] {event.name} {detail}", flush=True)

            elif event.type == "agent.custom_tool_use":
                # OUR turn: the session is idle until we answer
                print(f"  [custom tool] {event.name}({json.dumps(event.input)}) -> running host-side",
                      flush=True)
                result = run_custom_tool(event.name, event.input or {})
                client.beta.sessions.events.send(
                    session_id=session.id,
                    events=[{
                        "type": "user.custom_tool_result",
                        "custom_tool_use_id": event.id,  # the sevt_ event id, not a toolu_ id
                        "content": [{"type": "text", "text": result}],
                    }],
                )
                print(f"  [custom tool] sent {len(result)} bytes back", flush=True)

            elif event.type == "session.status_idle":
                # Idle is NOT terminal by itself: the session also idles while waiting
                # for a custom tool result. Only break on a terminal stop_reason.
                if event.stop_reason.type == "requires_action":
                    continue
                print(f"\n--- idle ({event.stop_reason.type}) ---")
                break

            elif event.type == "session.status_terminated":
                print("\n--- terminated ---")
                break

            elif event.type == "session.error":
                print(f"\n[session error] {event}", file=sys.stderr)

    # Pull whatever the agent wrote to /mnt/session/outputs/. Files index with a
    # ~1-3s lag after idle, hence the small retry.
    import time

    OUT_DIR.mkdir(exist_ok=True)
    for attempt in range(5):
        files = client.beta.files.list(scope_id=session.id, betas=["managed-agents-2026-04-01"])
        if files.data:
            break
        time.sleep(2)
    for f in files.data:
        dest = OUT_DIR / f.filename
        client.beta.files.download(f.id).write_to_file(dest)
        print(f"downloaded: {dest}")
    if not files.data:
        print("no output files found (check the trace URL)")


if __name__ == "__main__":
    main()
