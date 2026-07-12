"""The deployer for the Module 5 digest agent.

Creates a MANUAL-ONLY Managed Agents deployment (no schedule) per tier, then triggers and
observes runs. The point of the migration lives here: the local flow is the prod flow with a
different baked URL, not a different code path. You create one `depl_` object per tier (local
dev, preview, prod) and fire it with `deployments.run(id)`, the same call an eventual cron
would make, so "test locally" and "run in prod" differ by which deployment you pick, nothing
else.

Why the tier can't be a run argument. A manual run replays the deployment's fixed
`initial_events` and takes no parameters, so the target has to be baked into the deployment,
not passed at trigger time. The one field that differs per tier is
`agent_with_overrides.mcp_servers[0].url`: the session dials that tier's co-mounted MCP server
(/mcp), and because that server reads its own process's DATABASE_URL, the URL is what selects
the tier's database. The agent resource (resources.json) stays shared and version-pinned; the
override swaps only the URL, no new agent version. The agent's baked `mcp_toolset` references
the server by name (`sysdesign`), so a URL-only override keeps the tool wired.

Auth rides the vault: a static_bearer credential keyed to each tier's /mcp URL injects the
bearer at egress (m5_agents/apply.sh). The sandbox never sees the token, same contract as the
X-API-Key the agent's plain curl uses.

Read-only observation. Unlike the retired run_digest.py, this process does NOT answer tools.
get_rated_signals is now served by the remote MCP server, so the trigger side just watches the
event stream and closes. The digest is delivered by the agent to the API (PUT), readable at
GET BASE_URL/digests; the platform trace is the durable record.

Usage, from backend/:
    uv run python m5_agents/deploy.py create --tier local   # create the local-dev deployment
    uv run python m5_agents/deploy.py run    --tier local   # trigger it, stream the session
    uv run python m5_agents/deploy.py list                  # show recorded deployments

Only `local` is wired for now (step 1). preview and prod are placeholders; the same two
commands will stand them up once their turn comes. No schedule anywhere yet, all manual.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Run as a plain script: put backend/ on the path so common/ imports resolve.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from common.env import load_local_env

load_local_env()  # ANTHROPIC_API_KEY from backend/.env

import anthropic

HERE = Path(__file__).resolve().parent
RESOURCES = json.loads((HERE / "resources.json").read_text())
DEPLOYMENTS_FILE = HERE / "deployments.json"  # gitignored: {tier: depl_id}, per-machine runtime state

# Per-tier base URL. The MCP URL is base_url + "/mcp" (the co-mounted server). prod and preview
# are placeholders until their step; local is the Cloudflare tunnel to the laptop API, whose
# /mcp reads the local drill/Supabase DB (infra/README.md).
TIERS = {
    "local": "https://sysdesign-local.thedefrag.ai",
    "preview": None,  # per-PR *.up.railway.app; filled with --base-url when that step lands
    "prod": "https://sysdesign.thedefrag.ai",
}


def _client() -> anthropic.Anthropic:
    return anthropic.Anthropic()


def _load_deployments() -> dict:
    return json.loads(DEPLOYMENTS_FILE.read_text()) if DEPLOYMENTS_FILE.exists() else {}


def _save_deployments(d: dict) -> None:
    DEPLOYMENTS_FILE.write_text(json.dumps(d, indent=2) + "\n")


def _base_url(tier: str, override: str | None) -> str:
    base = override or TIERS.get(tier)
    if not base:
        sys.exit(f"tier '{tier}' has no base URL; pass --base-url (preview is per-PR).")
    return base.rstrip("/")


# --- create -----------------------------------------------------------------------

def cmd_create(args: argparse.Namespace) -> None:
    deployments = _load_deployments()
    if args.tier in deployments and not args.replace:
        sys.exit(
            f"deployment for '{args.tier}' already recorded ({deployments[args.tier]}).\n"
            f"Archive it first (deploy.py archive --tier {args.tier}) or pass --replace to\n"
            "record a new one (the old depl_ is orphaned, not deleted; archive it yourself)."
        )

    base = _base_url(args.tier, args.base_url)
    mcp_url = f"{base}/mcp"
    client = _client()

    # agent_with_overrides: same {id, version} as the shared agent, plus a full-replacement
    # mcp_servers list carrying only this tier's URL. Name stays `sysdesign` so the agent's
    # baked mcp_toolset still resolves. Tools/model/system are omitted, so they're preserved
    # from the pinned agent version.
    deployment = client.beta.deployments.create(
        name=f"sysdesign digest ({args.tier})",
        description=f"Manual-only digest run targeting the {args.tier} tier ({base}).",
        agent={
            "type": "agent_with_overrides",
            "id": RESOURCES["agent_id"],
            "version": RESOURCES["agent_version"],
            "mcp_servers": [{"type": "url", "name": "sysdesign", "url": mcp_url}],
        },
        environment_id=RESOURCES["environment_id"],
        vault_ids=[RESOURCES["vault_id"]],
        resources=[
            {
                "type": "memory_store",
                "memory_store_id": RESOURCES["memory_store_id"],
                "access": "read_write",
                "instructions": (
                    "Previous weekly digests, one dated file per week. Read the most recent "
                    "one before writing this week's, then save a dated copy of the new digest here."
                ),
            }
        ],
        # The kickoff is fixed for every manual run of this deployment. It names the tier's
        # base URL for the agent's REST curl; the MCP URL is baked above, separately.
        initial_events=[
            {
                "type": "user.message",
                "content": [{"type": "text", "text": f"Write this week's digest. API base URL: {base}"}],
            }
        ],
        # No schedule => manual-only. Trigger with `deploy.py run`.
    )

    deployments[args.tier] = deployment.id
    _save_deployments(deployments)
    print(f"created {args.tier} deployment: {deployment.id}")
    print(f"  base URL:  {base}")
    print(f"  MCP URL:   {mcp_url}")
    print(f"  status:    {deployment.status}")
    print(f"\nTrigger it:  uv run python m5_agents/deploy.py run --tier {args.tier}")


# --- run --------------------------------------------------------------------------

def cmd_run(args: argparse.Namespace) -> None:
    deployments = _load_deployments()
    depl_id = deployments.get(args.tier)
    if not depl_id:
        sys.exit(f"no deployment recorded for '{args.tier}'. Create it first (deploy.py create).")

    client = _client()
    run = client.beta.deployments.run(depl_id)  # manual run: no arguments, replays initial_events

    if run.session_id is None:
        err = run.error
        etype = getattr(err, "type", "unknown")
        emsg = getattr(err, "message", "")
        sys.exit(f"run failed at session creation: {etype} {emsg}\n  run id: {run.id}")

    session_id = run.session_id
    base = _base_url(args.tier, args.base_url)
    print(f"run:     {run.id}")
    print(f"session: {session_id}")
    print(f"trace:   https://platform.claude.com/workspaces/default/sessions/{session_id}")
    print(f"result:  GET {base}/digests once it completes\n")

    if args.no_stream:
        return

    # Read-only observation. We don't answer tools (the MCP server does); we just relay the
    # transcript until a terminal idle. If we miss the first events (the stream opens after the
    # deployment already sent the kickoff), the trace URL above is the authoritative record.
    with client.beta.sessions.events.stream(session_id=session_id) as stream:
        for event in stream:
            if event.type == "agent.message":
                for block in event.content:
                    if block.type == "text":
                        print(block.text, flush=True)
            elif event.type == "agent.tool_use":
                detail = json.dumps(getattr(event, "input", None) or {})[:160]
                print(f"  [tool] {event.name} {detail}", flush=True)
            elif event.type == "session.status_idle":
                # Idle also fires mid-run while a tool resolves; only a terminal stop_reason ends it.
                if event.stop_reason.type == "requires_action":
                    continue
                print(f"\n--- idle ({event.stop_reason.type}) ---")
                break
            elif event.type == "session.status_terminated":
                print("\n--- terminated ---")
                break
            elif event.type == "session.error":
                print(f"\n[session error] {event}", file=sys.stderr)


# --- list -------------------------------------------------------------------------

def cmd_list(args: argparse.Namespace) -> None:
    deployments = _load_deployments()
    if not deployments:
        print("no deployments recorded (deployments.json absent). Create one with deploy.py create.")
        return
    for tier, depl_id in deployments.items():
        print(f"{tier:8} {depl_id}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Module 5 digest agent deployer (manual-only, per tier).")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_create = sub.add_parser("create", help="create a manual-only deployment for a tier")
    p_create.add_argument("--tier", default="local", choices=list(TIERS))
    p_create.add_argument("--base-url", default=None, help="override the tier base URL (preview PR domain)")
    p_create.add_argument("--replace", action="store_true", help="record a new deployment even if one exists")
    p_create.set_defaults(func=cmd_create)

    p_run = sub.add_parser("run", help="trigger a manual run and stream the session")
    p_run.add_argument("--tier", default="local", choices=list(TIERS))
    p_run.add_argument("--base-url", default=None, help="override for the 'result:' hint URL")
    p_run.add_argument("--no-stream", action="store_true", help="trigger and print ids, don't stream")
    p_run.set_defaults(func=cmd_run)

    p_list = sub.add_parser("list", help="show recorded deployments")
    p_list.set_defaults(func=cmd_list)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
