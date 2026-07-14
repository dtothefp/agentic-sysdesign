#!/usr/bin/env python3
"""Opt-in PR preview environments for the sysdesign Railway project.

Driven by .github/workflows/preview-env.yml: adding the "preview" label to a PR
creates a Railway environment named pr-<number> cloned from production, pointed
at the PR branch; removing the label (or closing the PR) deletes it. Runnable
locally too:

    python3 infra/railway-preview.py up 42 my-branch   # create/refresh env pr-42
    python3 infra/railway-preview.py down 42           # delete env pr-42

Auth is the WORKSPACE token (env var RAILWAY_WORKSPACE_TOKEN, falls back to the
same key in the repo-root .env; sent as "Authorization: Bearer"). The project-scoped
token that drives railway-env.py CANNOT be used here: it's scoped to the
production environment, so it can create a new environment but then can't read,
retarget, or delete it (verified empirically 2026-07-10). In CI the token comes
from the RAILWAY_WORKSPACE_TOKEN repo secret.

What "up" does, in order, all idempotent:
  1. environmentCreate(sourceEnvironmentId=production, skipInitialDeploys=true)
     unless pr-<n> already exists. Cloning copies service instances and env vars
     (including the ${{redis...}} reference templates and the shared Supabase
     DATABASE_URL, see the preview-env section of infra/README.md for why that's
     a hazard worth knowing about).
  2. deploymentTriggerUpdate on every cloned trigger (api + worker) so they
     track the PR branch instead of main. After this, pushes to the PR branch
     auto-deploy to the preview env, no Action run needed.
  3. serviceDomainCreate for the api if it doesn't have one (clones don't copy
     domains). Railway assigns something like api-pr-42.up.railway.app.
  4. serviceInstanceDeployV2 for redis, worker, api, the initial deploys that
     step 1 skipped.

Migrations do NOT run in preview environments. services/api/railway.json scopes
preDeployCommand to the production environment because previews share the
production Supabase database.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

GQL = "https://backboard.railway.com/graphql/v2"
ENV_FILE = Path(__file__).resolve().parent.parent / ".env"

PROJECT_ID = "12dffbd4-65bd-44f7-83b7-d30238c92892"  # sysdesign
PROD_ENVIRONMENT_ID = "530d245e-d3f1-478d-b622-04e9426d7470"  # production

SERVICES = {
    "api": "96b5d402-727f-4f2a-bce0-5818e2b7973e",
    "worker": "adb815cd-c5dc-4868-80d6-2f64e82b28eb",
    "redis": "3551ca6b-210e-41ce-b49d-e1f4ca0faa9e",
}
# redis first so the broker is coming up while api/worker build
DEPLOY_ORDER = ["redis", "worker", "api"]


def load_token() -> str:
    if os.environ.get("RAILWAY_WORKSPACE_TOKEN"):
        return os.environ["RAILWAY_WORKSPACE_TOKEN"]
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            if line.startswith("RAILWAY_WORKSPACE_TOKEN="):
                return line.split("=", 1)[1].strip()
    sys.exit(
        "RAILWAY_WORKSPACE_TOKEN not set (env var or the repo-root .env). This must be "
        "a workspace token, not the project token; see infra/README.md."
    )


def gql(token: str, query: str, variables: dict | None = None) -> dict:
    body = json.dumps({"query": query, "variables": variables or {}}).encode()
    # UA matters. Railway's edge 403s the default Python urllib agent.
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "User-Agent": "curl/8.7.1",
    }
    for attempt in range(4):
        try:
            req = urllib.request.Request(GQL, body, headers)
            resp = json.load(urllib.request.urlopen(req))
            if resp.get("errors"):
                raise RuntimeError(resp["errors"][0]["message"])
            return resp["data"]
        except urllib.error.HTTPError:
            if attempt == 3:
                raise
            time.sleep(2 * (attempt + 1))  # transient 5xx, back off and retry
    raise AssertionError("unreachable")


def find_environment(token: str, name: str) -> str | None:
    q = """query($projectId: String!) {
      environments(projectId: $projectId) { edges { node { id name } } }
    }"""
    edges = gql(token, q, {"projectId": PROJECT_ID})["environments"]["edges"]
    for edge in edges:
        if edge["node"]["name"] == name:
            return edge["node"]["id"]
    return None


def environment_detail(token: str, env_id: str) -> dict:
    q = """query($id: String!) {
      environment(id: $id) {
        deploymentTriggers { edges { node { id serviceId branch } } }
        serviceInstances { edges { node {
          serviceId serviceName domains { serviceDomains { domain } }
        } } }
      }
    }"""
    return gql(token, q, {"id": env_id})["environment"]


def cmd_up(token: str, pr: str, branch: str) -> None:
    name = f"pr-{pr}"
    env_id = find_environment(token, name)
    if env_id:
        print(f"environment {name} already exists ({env_id})")
    else:
        m = """mutation($input: EnvironmentCreateInput!) {
          environmentCreate(input: $input) { id }
        }"""
        env_id = gql(
            token,
            m,
            {
                "input": {
                    "projectId": PROJECT_ID,
                    "name": name,
                    "sourceEnvironmentId": PROD_ENVIRONMENT_ID,
                    "skipInitialDeploys": True,
                }
            },
        )["environmentCreate"]["id"]
        print(f"created environment {name} ({env_id})")

    detail = environment_detail(token, env_id)

    # point the cloned repo triggers (api + worker) at the PR branch
    m = """mutation($id: String!, $input: DeploymentTriggerUpdateInput!) {
      deploymentTriggerUpdate(id: $id, input: $input) { id branch }
    }"""
    for edge in detail["deploymentTriggers"]["edges"]:
        trig = edge["node"]
        if trig["branch"] != branch:
            gql(token, m, {"id": trig["id"], "input": {"branch": branch}})
            print(f"trigger {trig['serviceId'][:8]}: {trig['branch']} -> {branch}")
        else:
            print(f"trigger {trig['serviceId'][:8]}: already on {branch}")

    # the api needs a Railway-provided domain (domains aren't cloned)
    domain = None
    for edge in detail["serviceInstances"]["edges"]:
        inst = edge["node"]
        if inst["serviceId"] == SERVICES["api"] and inst["domains"]["serviceDomains"]:
            domain = inst["domains"]["serviceDomains"][0]["domain"]
    if not domain:
        m = """mutation($input: ServiceDomainCreateInput!) {
          serviceDomainCreate(input: $input) { domain }
        }"""
        domain = gql(
            token,
            m,
            {
                "input": {
                    "environmentId": env_id,
                    "serviceId": SERVICES["api"],
                }
            },
        )["serviceDomainCreate"]["domain"]
        print(f"created api domain {domain}")

    for svc in DEPLOY_ORDER:
        m = """mutation($environmentId: String!, $serviceId: String!) {
          serviceInstanceDeployV2(environmentId: $environmentId, serviceId: $serviceId)
        }"""
        gql(token, m, {"environmentId": env_id, "serviceId": SERVICES[svc]})
        print(f"deploy queued: {svc}")
        time.sleep(0.5)

    # last line is machine-readable, the workflow greps it for the PR comment
    print(f"PREVIEW_URL=https://{domain}")


def cmd_down(token: str, pr: str) -> None:
    name = f"pr-{pr}"
    env_id = find_environment(token, name)
    if not env_id:
        print(f"environment {name} not found, nothing to delete")
        return
    gql(token, """mutation($id: String!) { environmentDelete(id: $id) }""", {"id": env_id})
    print(f"deleted environment {name} ({env_id})")


def main() -> None:
    args = sys.argv[1:]
    if len(args) == 3 and args[0] == "up":
        cmd_up(load_token(), args[1], args[2])
    elif len(args) == 2 and args[0] == "down":
        cmd_down(load_token(), args[1])
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
