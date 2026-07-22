#!/usr/bin/env python3
"""Railway env-var sync for the sysdesign project. IaC-lite.

The MANIFEST below is the source of truth for which variables each service gets
and where the value comes from. Secret values live in the repo-root .env (gitignored),
never here. Reference values (${{redis...}}) are Railway-side templates resolved
at deploy, so no secret ever passes through this file.

    python3 infra/railway-env.py list          # what's actually set on Railway right now
    python3 infra/railway-env.py sync --dry    # diff manifest vs remote, change nothing
    python3 infra/railway-env.py sync          # push manifest values

Auth is the project-scoped token (RAILWAY_PROJECT_TOKEN in the repo-root .env). It can
read/write variables and trigger deploys for this one project, nothing else.
"""

import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

GQL = "https://backboard.railway.com/graphql/v2"
ENV_FILE = Path(__file__).resolve().parent.parent / ".env"

PROJECT_ID = "12dffbd4-65bd-44f7-83b7-d30238c92892"  # sysdesign
ENVIRONMENT_ID = "530d245e-d3f1-478d-b622-04e9426d7470"  # production

SERVICES = {
    "api": "96b5d402-727f-4f2a-bce0-5818e2b7973e",
    "worker": "adb815cd-c5dc-4868-80d6-2f64e82b28eb",
    "redis": "3551ca6b-210e-41ce-b49d-e1f4ca0faa9e",
    # The Module 7 chat agent. Provisioned lazily: create the Railway service, then put its id in
    # the repo-root .env as RAILWAY_AGENT_SERVICE_ID and it's injected here at runtime (see main).
    # Empty until then, and the list/sync loops skip an id-less service, so this file is valid and
    # runnable before the service exists.
    "agent": "",
    # The messaging gateway (services/chat: FastAPI + WebSocket direct messaging). A DIFFERENT
    # service from "agent": that one is the ReAct chat agent (services/agent), this one is the
    # message-delivery socket server. Named "messaging" precisely so the two don't get confused,
    # since the repo dir services/chat and the agent's public "chat" domain both want that word.
    # Same lazy-provision pattern: create the Railway service pointed at services/chat/railway.json,
    # drop its id in the repo-root .env as RAILWAY_MESSAGING_SERVICE_ID, and it's injected at runtime.
    "messaging": "",
}

# One Redis URL template, three consumers. ${{...}} is Railway's reference syntax,
# resolved against the redis service at deploy, so rotating REDIS_PASSWORD in one
# place propagates everywhere on the next deploy.
REDIS_BASE = "redis://:${{redis.REDIS_PASSWORD}}@${{redis.RAILWAY_PRIVATE_DOMAIN}}:6379"

# Manifest entry values are either ("env", KEY) = read KEY from the repo-root .env,
# or ("literal", VALUE) = use VALUE as-is (safe for non-secrets and references).
MANIFEST = {
    "api": {
        "DATABASE_URL": ("env", "DATABASE_URL_SUPABASE"),
        "REDIS_URL": ("literal", REDIS_BASE),
        "CELERY_BROKER_URL": ("literal", REDIS_BASE + "/0"),
        "CELERY_RESULT_BACKEND": ("literal", REDIS_BASE + "/1"),
        # Module 4: the API validates a run's model at the door (resolve_model checks the
        # provider key exists), so the keys live on BOTH services. RATING_MODEL doesn't:
        # it's the worker's default, the API never reads it.
        "GROQ_API_KEY": ("env", "GROQ_API_KEY"),
        "ANTHROPIC_API_KEY": ("env", "ANTHROPIC_API_KEY"),
        # Module 5: turns on X-API-Key enforcement (api/main.py require_api_key). The same
        # value lives in the Managed Agents vault, substituted into the digest agent's
        # requests at egress, so the sandbox never sees it. api-only: the worker never
        # calls the API (it writes the database directly).
        "SYSDESIGN_API_KEY": ("env", "SYSDESIGN_API_KEY"),
        # Module 5: where the digest agent is told to reach the API. start_digest (which
        # runs IN this api process, called by POST /digests) resolves the agent's base_url
        # as: POST-body override -> this var -> code default sysdesign.thedefrag.ai. The
        # ${{...}} template re-resolves PER ENVIRONMENT: prod gets prod's domain, and any
        # PR-preview env forked from prod gets ITS OWN preview domain, both covered by the
        # vault's allowed_hosts (sysdesign.thedefrag.ai + *.up.railway.app). Net effect: a
        # bodyless POST /digests on any deploy targets that same deploy, so base_url in the
        # request body is only ever needed for the localhost tunnel (a process that can't
        # know its own public URL). api-only, same reason as SYSDESIGN_API_KEY above.
        "SYSDESIGN_PUBLIC_URL": ("literal", "https://${{RAILWAY_PUBLIC_DOMAIN}}"),
        # Module 6 semantic search, query side. The api embeds the SEARCH QUERY at request time
        # (common.search.embed_query -> embed_text) so GET /search can run the vector half of the
        # hybrid RRF. Same OpenAI 1536-dim model the worker writes doc vectors with, so query and
        # document embeddings share a space. Unset EMBEDDING_MODEL degrades /search to lexical-only
        # ("semantic": false). The key lives on both services because both embed (worker writes,
        # api queries); it's the same OPENAI_API_KEY from the app .env.
        "EMBEDDING_MODEL": ("literal", "openai/text-embedding-3-small"),
        "OPENAI_API_KEY": ("env", "OPENAI_API_KEY"),
        # LangSmith tracing for the api (the embed_text span on the query path, plus any api-side
        # traced work). Same inert-until-keyed contract and same prod project as worker/agent, so
        # every service's traces land together under sysdesign-prod. Prod's own key, Railway-only.
        "LANGSMITH_TRACING": ("literal", "true"),
        "LANGSMITH_ENDPOINT": ("literal", "https://api.smith.langchain.com"),
        "LANGSMITH_PROJECT": ("literal", "sysdesign-prod"),
        "LANGSMITH_API_KEY": ("env_optional", "LANGSMITH_API_KEY_PROD"),
    },
    "worker": {
        "DATABASE_URL": ("env", "DATABASE_URL_SUPABASE"),
        "REDIS_URL": ("literal", REDIS_BASE),
        "CELERY_BROKER_URL": ("literal", REDIS_BASE + "/0"),
        "CELERY_RESULT_BACKEND": ("literal", REDIS_BASE + "/1"),
        "APIFY_API_KEY": ("env", "APIFY_API_KEY"),
        # Module 6 semantic search, write side. The worker embeds each new signal's caption in the
        # embed_signal stage (and the sweep_unembedded beat backstop) and writes the vector to
        # signal_embeddings, which is what the api's query half searches against. Same 1536-dim
        # OpenAI model + key as the api. Unset EMBEDDING_MODEL makes the embedding stage inert
        # (search stays lexical-only). One-time turn-on over an existing corpus is the separate
        # worker.backfill script; this var just keeps future signals embedded.
        "EMBEDDING_MODEL": ("literal", "openai/text-embedding-3-small"),
        "OPENAI_API_KEY": ("env", "OPENAI_API_KEY"),
        # Module 4 rating stage, LIVE as of 2026-07-10. Default is Groq's free tier
        # (rate-limited, plenty for this pipeline); Anthropic is the per-run quality
        # override, POST /runs {"model": "anthropic/claude-haiku-4-5"}. Keys come from
        # the repo-root .env (GROQ reused from the parent root .env, ANTHROPIC from
        # package-defrag). Unset RATING_MODEL here and re-sync to make rating inert again.
        "RATING_MODEL": ("literal", "groq/llama-3.1-8b-instant"),
        "GROQ_API_KEY": ("env", "GROQ_API_KEY"),
        "ANTHROPIC_API_KEY": ("env", "ANTHROPIC_API_KEY"),
        # LangSmith tracing for the rating call. Worker-only: rate_caption runs here, never on
        # the API. Inert unless LANGSMITH_TRACING=true AND the key resolve, so leaving the key
        # out of .env just makes tracing a no-op (same inert-until-keyed contract as rating).
        "LANGSMITH_TRACING": ("literal", "true"),
        "LANGSMITH_ENDPOINT": ("literal", "https://api.smith.langchain.com"),
        # Deployed traces land in a separate project from local (the repo-root .env uses
        # sysdesign-local), so the LangSmith sidebar cleanly splits prod from dev runs.
        "LANGSMITH_PROJECT": ("literal", "sysdesign-prod"),
        # Prod gets its OWN LangSmith key (the local worker uses the local LANGSMITH_API_KEY), so
        # either environment's key can be revoked without touching the other. This key lives ONLY
        # in Railway, never in the repo-root .env, since it has no local use. env_optional means: push it
        # from .env when it happens to be present (the rotation path), else leave Railway's be.
        "LANGSMITH_API_KEY": ("env_optional", "LANGSMITH_API_KEY_PROD"),
    },
    "agent": {
        # The chat agent is a CLIENT of the data API, not fused into it: no DATABASE_URL, no
        # Redis, no Apify. Its whole job is the LLM ReAct loop plus HTTP calls to the api, so its
        # blast radius is just those two things. That separation is the point of the two planes.
        #
        # Where its tools dial the data API. The public api domain (proven: the digest agent uses
        # the same host). The api enforces X-API-Key, so SYSDESIGN_API_KEY below both authenticates
        # the agent AS a client here AND gates the agent's own /chat surface (server.py). One key,
        # two directions.
        "SYSDESIGN_API_URL": ("literal", "https://sysdesign.thedefrag.ai"),
        "SYSDESIGN_API_KEY": ("env", "SYSDESIGN_API_KEY"),
        # The agent runs the model loop itself (loop.py), so it needs the Anthropic key directly,
        # unlike the api (which only validates a run's model at the door).
        "ANTHROPIC_API_KEY": ("env", "ANTHROPIC_API_KEY"),
        # LangSmith tracing for the agent loop (agent_turn chain + LLM + tool spans). Same
        # inert-until-keyed contract and same prod project as the worker, so agent and rating
        # traces land side by side under sysdesign-prod. Prod's own key, Railway-only.
        "LANGSMITH_TRACING": ("literal", "true"),
        "LANGSMITH_ENDPOINT": ("literal", "https://api.smith.langchain.com"),
        "LANGSMITH_PROJECT": ("literal", "sysdesign-prod"),
        "LANGSMITH_API_KEY": ("env_optional", "LANGSMITH_API_KEY_PROD"),
    },
    "messaging": {
        # Small blast radius on purpose: the gateway persists to Postgres and (step 2) fans out
        # over Redis pub/sub, nothing else. No Apify, no LLM keys, no API client key (auth is
        # punted; identity is a user_id query param until Supabase JWT lands).
        #
        # Same Supabase Postgres as api + worker. The msg_* tables coexist with the scraper schema,
        # and the service's own railway.json preDeployCommand runs migrate.py so the schema is
        # applied on deploy.
        "DATABASE_URL": ("env", "DATABASE_URL_SUPABASE"),
        # For step 2: publish each message to a Redis channel, every instance subscribes, and the
        # instance holding the recipient's socket does the local push. Same shared redis + rotating
        # ${{...}} password reference as api/worker, so multi-instance chat and the Celery broker
        # ride one Redis. Unused by step 1 (single instance) but set now so the step-2 deploy is
        # just code, no env change.
        "REDIS_URL": ("literal", REDIS_BASE),
    },
    # redis's own REDIS_PASSWORD is deliberately NOT managed here. It was generated
    # once at provision time; rotating it is a deliberate act, not a sync side effect.
}


def load_dotenv() -> dict:
    env = {}
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k] = v
    return env


def gql(token: str, query: str, variables: dict | None = None) -> dict:
    body = json.dumps({"query": query, "variables": variables or {}}).encode()
    # UA matters. Railway's edge 403s the default Python urllib agent.
    headers = {
        "Project-Access-Token": token,
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


def remote_vars(token: str, service_id: str) -> dict:
    q = """query($projectId: String!, $environmentId: String!, $serviceId: String!) {
      variables(projectId: $projectId, environmentId: $environmentId, serviceId: $serviceId, unrendered: true)
    }"""
    return gql(
        token,
        q,
        {
            "projectId": PROJECT_ID,
            "environmentId": ENVIRONMENT_ID,
            "serviceId": service_id,
        },
    )["variables"]


# Sentinel for a manifest entry we deliberately leave untouched this run (a prod-only secret
# that lives in Railway and isn't in the repo-root .env).
_SKIP = object()


def resolve(entry: tuple, dotenv: dict):
    kind, val = entry
    if kind == "literal":
        return val
    if kind == "env_optional":
        # Prod-only secret managed in Railway, not in the repo-root .env. Push it only when it IS in
        # .env (the rotation path: drop the new key in, sync, remove); otherwise leave Railway's
        # value alone instead of erroring on the blank.
        return dotenv.get(val, _SKIP)
    if val not in dotenv:
        sys.exit(f"the repo-root .env is missing {val}, refusing to sync a blank")
    return dotenv[val]


def redact(name: str, value: str) -> str:
    if "${{" in value:  # references are templates, not secrets
        return value
    if any(s in name for s in ("KEY", "PASSWORD", "SECRET", "URL")):
        return value[:14] + "..." if len(value) > 14 else "***"
    return value


def cmd_list(token: str) -> None:
    for svc, svc_id in SERVICES.items():
        if not svc_id:
            print(f"\n[{svc}] skipped (no service id; set RAILWAY_{svc.upper()}_SERVICE_ID in .env)")
            continue
        print(f"\n[{svc}]")
        for name, value in sorted(remote_vars(token, svc_id).items()):
            print(f"  {name} = {redact(name, value)}")


def cmd_sync(token: str, dry: bool) -> None:
    dotenv = load_dotenv()
    upsert = """mutation($input: VariableUpsertInput!) { variableUpsert(input: $input) }"""
    for svc, wanted in MANIFEST.items():
        svc_id = SERVICES[svc]
        if not svc_id:
            print(f"  {svc}: skipped (no service id; set RAILWAY_{svc.upper()}_SERVICE_ID in .env once the service exists)")
            continue
        current = remote_vars(token, svc_id)
        for name, entry in wanted.items():
            value = resolve(entry, dotenv)
            if value is _SKIP:
                where = "already in Railway" if name in current else "MISSING from Railway too"
                print(f"  {svc}.{name}: skip ({where}, not in .env)")
                continue
            if current.get(name) == value:
                print(f"  {svc}.{name}: unchanged")
                continue
            verb = "would set" if dry else "set"
            print(f"  {svc}.{name}: {verb} -> {redact(name, value)}")
            if not dry:
                gql(
                    token,
                    upsert,
                    {
                        "input": {
                            "projectId": PROJECT_ID,
                            "environmentId": ENVIRONMENT_ID,
                            "serviceId": svc_id,
                            "name": name,
                            "value": value,
                        }
                    },
                )
                time.sleep(0.5)
        # flag drift the manifest doesn't know about (the Vercel mystery-var problem)
        railway_injected = {"RAILWAY_", "REDIS_PASSWORD"}
        for name in sorted(set(current) - set(wanted)):
            if not any(name.startswith(p) for p in railway_injected):
                print(f"  {svc}.{name}: on Railway but NOT in manifest (drift)")


def main() -> None:
    args = sys.argv[1:]
    if not args or args[0] not in ("list", "sync"):
        sys.exit(__doc__)
    env = load_dotenv()
    token = env.get("RAILWAY_PROJECT_TOKEN") or sys.exit("RAILWAY_PROJECT_TOKEN missing from the repo-root .env")
    # Lazily-provisioned services read their id from .env, so this file stays valid pre-provision.
    if env.get("RAILWAY_AGENT_SERVICE_ID"):
        SERVICES["agent"] = env["RAILWAY_AGENT_SERVICE_ID"]
    if env.get("RAILWAY_MESSAGING_SERVICE_ID"):
        SERVICES["messaging"] = env["RAILWAY_MESSAGING_SERVICE_ID"]
    if args[0] == "list":
        cmd_list(token)
    else:
        cmd_sync(token, dry="--dry" in args)


if __name__ == "__main__":
    main()
