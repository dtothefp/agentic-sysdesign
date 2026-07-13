#!/usr/bin/env python3
"""agentctl - declarative deploy of the Module 5 digest agent via the `ant` CLI.

Not the Anthropic SDK and not a reimplementation of the control plane: a thin
orchestrator that renders the declarative YAML in this directory (agent.yaml,
deployment.yaml, environment.yaml, vault/*.yaml) and drives the `ant` CLI
(create / update / archive / run) idempotently, keyed by NAME. Same command is
safe to re-run, and every object is identifiable in the Anthropic Console.

Why one agent AND one deployment per environment: a deployment's `agent` field
only pins {id, version}; it CANNOT override the agent config. The tier-selecting
/mcp URL therefore has to live in the immutable agent version, so each tier gets
its own named agent, and each agent gets its own named deployment. Names carry
identity, metadata carries the detail:

    tier=prod     agent sysdesign-digest-prod              deployment digest-prod
    tier=preview  agent sysdesign-digest-preview-pr<N>     deployment digest-preview-pr<N>
    tier=local    agent sysdesign-digest-local-<branch>    deployment digest-local-<branch>

All deployments are MANUAL (no schedule). A real prod system would add a cron to
the prod deployment (see deployment.yaml); prod runs are hand-triggered for now.

Usage (also driven by .github/workflows/agent-deploy.yml and `make deploy`):

    agentctl.py deploy   --tier prod    [--sha S] [--run]
    agentctl.py deploy   --tier local   --branch B [--sha S] [--run]
    agentctl.py deploy   --tier preview --pr N --base-url URL [--sha S] [--run]
    agentctl.py run      --tier local   --branch B
    agentctl.py teardown --tier preview --pr N
    agentctl.py list

Shared durable resources (environment, vault, prod memory store) are created
ONCE, out of band, and their ids read from resources.json; agentctl never mints
them (that would risk a second memory store splitting prod's week-over-week
memory). The only per-environment vault object, the static_bearer MCP
credential, is created lazily on deploy for any /mcp URL that doesn't have one.

The MCP bearer + API key secret both come from $SYSDESIGN_API_KEY (a CI secret),
injected at apply time, never committed.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent

# Run as a plain script (`python m5_agents/agentctl.py`), so put backend/ on the
# import path for `common`. Then load backend/.env so a local run has both
# ANTHROPIC_API_KEY (which `ant` reads) and SYSDESIGN_API_KEY (injected into the
# vault credentials). load_local_env is env-first, so CI's real env vars win and
# it's a no-op when there's no .env (the CI case).
sys.path.insert(0, str(HERE.parent))
from common.env import load_local_env  # noqa: E402

load_local_env()

RESOURCES = json.loads((HERE / "resources.json").read_text())

ENV_ID = RESOURCES["environment_id"]
VAULT_ID = RESOURCES["vault_id"]
MEMORY_ID = RESOURCES["memory_store_id"]

# Per-tier API base URLs. Preview has no fixed URL (per-PR *.up.railway.app), so
# it must be passed with --base-url. The MCP URL is always base + "/mcp".
TIER_BASE_URLS = {
    "local": "https://sysdesign-local.thedefrag.ai",
    "prod": "https://sysdesign.thedefrag.ai",
    "preview": None,
}

AGENT_BASE = "sysdesign-digest"  # agent name prefix; agentctl appends the tier suffix
DEPLOY_BASE = "digest"  # deployment name prefix

TRACE = "https://platform.claude.com/workspaces/default/sessions/{sid}"


# --- `ant` plumbing --------------------------------------------------------------

class AntError(RuntimeError):
    def __init__(self, args: list[str], stdout: str, stderr: str):
        super().__init__(f"`ant {' '.join(args)}` failed:\n{stderr or stdout}")
        self.stdout, self.stderr = stdout, stderr


def ant(args: list[str]) -> str:
    """Run `ant <args>` and return stdout, raising on non-zero exit."""
    proc = subprocess.run(["ant", *args], capture_output=True, text=True)
    if proc.returncode != 0:
        raise AntError(args, proc.stdout, proc.stderr)
    return proc.stdout


def ant_json(args: list[str]):
    """Run `ant <args>` and parse its stdout. `ant` prints one JSON value, or a
    stream of concatenated JSON values for list endpoints, so decode greedily."""
    out = ant(args).strip()
    if not out:
        return None
    dec, vals, i = json.JSONDecoder(), [], 0
    while i < len(out):
        while i < len(out) and out[i].isspace():
            i += 1
        if i >= len(out):
            break
        obj, i = dec.raw_decode(out, i)
        vals.append(obj)
    return vals[0] if len(vals) == 1 else vals


def ant_list(prefix: list[str], extra: list[str] | None = None) -> list[dict]:
    """List helper that normalizes the shapes `ant ... list` can return (a bare
    array, concatenated objects, or a {data: [...]} envelope) into a list."""
    val = ant_json([*prefix, "list", "--max-items", "-1", *(extra or [])])
    if val is None:
        return []
    if isinstance(val, dict):
        data = val.get("data")
        return data if isinstance(data, list) else [val]
    return val


def load_yaml(rel: str) -> dict:
    return yaml.safe_load((HERE / rel).read_text())


def require_secret() -> str:
    key = os.environ.get("SYSDESIGN_API_KEY")
    if not key:
        sys.exit("SYSDESIGN_API_KEY is not set (needed to inject vault credentials).")
    return key


# --- naming ----------------------------------------------------------------------

def _suffix(tier: str, branch: str | None, pr: int | None) -> str:
    if tier == "prod":
        return "prod"
    if tier == "preview":
        if pr is None:
            sys.exit("--pr is required for tier=preview")
        return f"preview-pr{pr}"
    if tier == "local":
        if not branch:
            sys.exit("--branch is required for tier=local")
        return f"local-{_slug(branch)}"
    sys.exit(f"unknown tier: {tier}")


def _slug(s: str) -> str:
    return "".join(c if c.isalnum() else "-" for c in s).strip("-").lower()


def names(tier: str, branch: str | None, pr: int | None) -> tuple[str, str]:
    suf = _suffix(tier, branch, pr)
    return f"{AGENT_BASE}-{suf}", f"{DEPLOY_BASE}-{suf}"


def resolve_base_url(tier: str, base_url: str | None) -> str:
    url = base_url or TIER_BASE_URLS.get(tier)
    if not url:
        sys.exit(f"--base-url is required for tier={tier}")
    return url.rstrip("/")


def build_metadata(tier: str, base_url: str, branch, pr, sha) -> dict:
    meta = {
        "tier": tier,
        "base_url": base_url,
        "deployed_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    if branch:
        meta["branch"] = branch
    if pr is not None:
        meta["pr"] = str(pr)
    if sha:
        meta["sha"] = sha
    return meta


# --- find-by-name ----------------------------------------------------------------

def find_agent(name: str) -> dict | None:
    return next((a for a in ant_list(["beta:agents"]) if a.get("name") == name), None)


def find_deployment(name: str) -> dict | None:
    return next((d for d in ant_list(["beta:deployments"]) if d.get("name") == name), None)


def find_mcp_credential(mcp_url: str) -> dict | None:
    """Match on auth.mcp_server_url, the credential's true uniqueness key (a
    static_bearer is one-per-URL), so it's immune to display-name drift."""
    for c in ant_list(["beta:vaults:credentials"], ["--vault-id", VAULT_ID]):
        auth = c.get("auth") or {}
        if auth.get("type") == "static_bearer" and auth.get("mcp_server_url") == mcp_url:
            return c
    return None


# --- upserts ---------------------------------------------------------------------

def ensure_mcp_credential(base_url: str, meta: dict) -> str:
    """A static_bearer is keyed to one exact /mcp URL and can't wildcard, so each
    tier's URL needs its own credential. Create it if this URL doesn't have one.
    The tier/pr/branch metadata rides along so teardown can find it by PR number
    without having to re-derive the (by then dead) preview URL."""
    mcp_url = f"{base_url}/mcp"
    found = find_mcp_credential(mcp_url)
    if found:
        print(f"  vault: reusing MCP bearer for {mcp_url} ({found['id']})")
        return found["id"]
    tmpl = load_yaml("vault/mcp-bearer.yaml")
    display = tmpl["display_name"].replace("{{BASE_URL}}", base_url)
    token = require_secret()
    # `--auth` and `--metadata` each take one JSON/YAML mapping (not repeated
    # key=value pairs). Metadata values are stored as strings.
    auth = {"type": "static_bearer", "mcp_server_url": f"{base_url}/mcp", "token": token}
    res = ant_json([
        "beta:vaults:credentials", "create",
        "--vault-id", VAULT_ID,
        "--display-name", display,
        "--auth", json.dumps(auth),
        "--metadata", json.dumps({k: str(v) for k, v in meta.items()}),
    ])
    print(f"  vault: created MCP bearer for {base_url}/mcp -> {res['id']}")
    return res["id"]


def upsert_agent(name: str, base_url: str, meta: dict) -> tuple[str, int]:
    """Create (or update, on the optimistic version lock) the named agent from
    agent.yaml, with this tier's /mcp URL swapped into the server entry.

    Create sends the full config. Update sends only name/model/system/metadata:
    the tools and the mcp_servers URL are invariant for a fixed tier's agent
    (the URL is what makes it a distinct agent), so they're set once at create
    and preserved on update by omission. That also sidesteps a CLI quirk, the
    update subcommand's `--tool`/`--mcp-server` flags serialize differently from
    create's and reject the same toolset the create accepts."""
    cfg = load_yaml("agent.yaml")
    # `--model` is a mapping flag (a model_config), not a bare string, and the
    # key is `id` (the API normalizes to {"id": ..., "speed": "standard"}).
    model_cfg = json.dumps({"id": cfg["model"]})
    meta_json = json.dumps({k: str(v) for k, v in meta.items()})

    existing = find_agent(name)
    if existing:
        current = ant_json(["beta:agents", "retrieve", "--agent-id", existing["id"]])
        res = ant_json([
            "beta:agents", "update",
            "--agent-id", existing["id"], "--version", str(current["version"]),
            "--name", name, "--model", model_cfg, "--system", cfg["system"],
            "--metadata", meta_json,
        ])
        print(f"  agent: updated {name} -> {res['id']} v{res['version']}")
    else:
        servers = [
            {**s, "url": f"{base_url}/mcp"} if s.get("name") == "sysdesign" else s
            for s in cfg["mcp_servers"]
        ]
        body: list[str] = ["--name", name, "--model", model_cfg, "--system", cfg["system"]]
        for tool in cfg["tools"]:
            body += ["--tool", json.dumps(tool)]
        for srv in servers:
            body += ["--mcp-server", json.dumps(srv)]
        body += ["--metadata", meta_json]
        res = ant_json(["beta:agents", "create", *body])
        print(f"  agent: created {name} -> {res['id']} v{res['version']}")
    return res["id"], res["version"]


def upsert_deployment(tier: str, name: str, agent_id: str, agent_version: int,
                      base_url: str, meta: dict) -> str:
    """Create (or update) the named deployment pinning the agent, with the kickoff
    initial_event carrying this tier's base URL. Prod alone mounts the memory
    store (read_write); preview/local omit it so they never pollute prod memory."""
    dep = load_yaml("deployment.yaml")
    meta_json = json.dumps({k: str(v) for k, v in {**meta, "agent_version": str(agent_version)}.items()})

    existing = find_deployment(name)
    if existing:
        # Update only the mutable bits: re-pin `--agent` to the just-minted latest
        # version, refresh `--metadata`. Environment, vault, initial_event, and the
        # memory resource are invariant per named deployment, so they're preserved
        # by omission (which also sidesteps the update flags' JSON-typing quirk,
        # e.g. `--vault-id` wants an array on update, not a bare id).
        res = ant_json([
            "beta:deployments", "update", "--deployment-id", existing["id"],
            "--name", name, "--agent", agent_id, "--metadata", meta_json,
        ])
        print(f"  deployment: updated {name} -> {res['id']}")
        return res["id"]

    kickoff = dep["kickoff"].replace("{{BASE_URL}}", base_url).strip()
    args: list[str] = [
        "--name", name,
        "--agent", agent_id,  # id string pins latest, which is the version just minted
        "--environment-id", ENV_ID,
        "--vault-id", VAULT_ID,
        "--initial-event", json.dumps({"type": "user.message",
                                       "content": [{"type": "text", "text": kickoff}]}),
    ]
    if tier == "prod":
        args += ["--resource", json.dumps({
            "type": "memory_store",
            "memory_store_id": MEMORY_ID,
            "access": "read_write",
            "instructions": dep["memory_instructions"].strip(),
        })]
    args += ["--metadata", meta_json]
    res = ant_json(["beta:deployments", "create", *args])
    print(f"  deployment: created {name} -> {res['id']}")
    return res["id"]


# --- commands --------------------------------------------------------------------

def cmd_deploy(a: argparse.Namespace) -> None:
    base_url = resolve_base_url(a.tier, a.base_url)
    agent_name, deploy_name = names(a.tier, a.branch, a.pr)
    meta = build_metadata(a.tier, base_url, a.branch, a.pr, a.sha)
    print(f"deploy tier={a.tier} base_url={base_url}")

    ensure_mcp_credential(base_url, meta)
    agent_id, agent_version = upsert_agent(agent_name, base_url, meta)
    deployment_id = upsert_deployment(a.tier, deploy_name, agent_id, agent_version, base_url, meta)

    print(f"\nagent      {agent_name}  ({agent_id} v{agent_version})")
    print(f"deployment {deploy_name}  ({deployment_id})  [manual]")
    if a.run:
        _run(deployment_id, deploy_name)


def cmd_run(a: argparse.Namespace) -> None:
    _, deploy_name = names(a.tier, a.branch, a.pr)
    dep = find_deployment(deploy_name)
    if not dep:
        sys.exit(f"no deployment named {deploy_name}; run `deploy` first")
    _run(dep["id"], deploy_name)


def _run(deployment_id: str, deploy_name: str) -> None:
    res = ant_json(["beta:deployments", "run", "--deployment-id", deployment_id])
    sid = (res or {}).get("id") or (res or {}).get("session_id")
    print(f"\nran {deploy_name}: session {sid}")
    if sid:
        print(f"trace: {TRACE.format(sid=sid)}")


def cmd_teardown(a: argparse.Namespace) -> None:
    agent_name, deploy_name = names(a.tier, a.branch, a.pr)
    if a.tier == "prod":
        sys.exit("refusing to tear down prod; archive it by hand if you really mean to")

    dep = find_deployment(deploy_name)
    if dep:
        ant(["beta:deployments", "archive", "--deployment-id", dep["id"]])
        print(f"archived deployment {deploy_name} ({dep['id']})")
    agent = find_agent(agent_name)
    if agent:
        ant(["beta:agents", "archive", "--agent-id", agent["id"]])
        print(f"archived agent {agent_name} ({agent['id']})")

    # Preview URLs are ephemeral (per-PR Railway), so their bearer credential is
    # dead once the env is gone; delete it. Found by PR metadata, not URL, since
    # the preview URL is gone by teardown time. Local reuses a stable tunnel URL,
    # so its credential is left in place for the next local deploy.
    if a.tier == "preview":
        for c in ant_list(["beta:vaults:credentials"], ["--vault-id", VAULT_ID]):
            m = c.get("metadata") or {}
            if m.get("tier") == "preview" and m.get("pr") == str(a.pr):
                ant(["beta:vaults:credentials", "delete", "--vault-id", VAULT_ID, "--credential-id", c["id"]])
                print(f"deleted MCP credential pr{a.pr} ({c['id']})")


def cmd_list(_a: argparse.Namespace) -> None:
    agents = [a for a in ant_list(["beta:agents"]) if a.get("name", "").startswith(AGENT_BASE)]
    deploys = [d for d in ant_list(["beta:deployments"]) if d.get("name", "").startswith(DEPLOY_BASE)]
    print("AGENTS")
    for a in sorted(agents, key=lambda x: x.get("name", "")):
        m = a.get("metadata") or {}
        print(f"  {a.get('name'):40} v{a.get('version'):<3} tier={m.get('tier','?'):8} {a.get('id')}")
    print("DEPLOYMENTS")
    for d in sorted(deploys, key=lambda x: x.get("name", "")):
        m = d.get("metadata") or {}
        print(f"  {d.get('name'):40} tier={m.get('tier','?'):8} agent_v{m.get('agent_version','?'):<3} {d.get('id')}")


def main() -> None:
    p = argparse.ArgumentParser(description="Declarative deploy of the digest agent via `ant`.")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add_target(sp):
        sp.add_argument("--tier", required=True, choices=["prod", "preview", "local"])
        sp.add_argument("--branch")
        sp.add_argument("--pr", type=int)
        sp.add_argument("--base-url")
        sp.add_argument("--sha")

    d = sub.add_parser("deploy", help="create-or-update the agent + deployment for a tier")
    add_target(d)
    d.add_argument("--run", action="store_true", help="trigger a run immediately after deploy")
    d.set_defaults(func=cmd_deploy)

    r = sub.add_parser("run", help="trigger a run of an existing tier deployment")
    add_target(r)
    r.set_defaults(func=cmd_run)

    t = sub.add_parser("teardown", help="archive the agent + deployment for a tier (not prod)")
    add_target(t)
    t.set_defaults(func=cmd_teardown)

    ls = sub.add_parser("list", help="list digest agents + deployments with tier metadata")
    ls.set_defaults(func=cmd_list)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
