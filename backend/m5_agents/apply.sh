#!/usr/bin/env bash
# Control plane for the Module 5 digest agent, Terraform-lite. The YAML files
# beside this script are the source of truth; this script pushes them to the
# API once and records the returned IDs in resources.json (committed, IDs are
# not secrets). There is no state engine: no plan, no diff, no drift
# detection. The optimistic lock on `update --version` is all the safety you
# get, which is plenty for two resources.
#
# Re-running is a no-op once resources.json exists; it prints the update
# commands instead. Creating a fresh agent per run is THE canonical Managed
# Agents anti-pattern (you'd orphan a pile of versioned configs), hence the
# guard.
set -euo pipefail
cd "$(dirname "$0")"
set -a; source ../.env; set +a

if [[ -f resources.json ]]; then
  VAULT_ID=$(python3 -c 'import json;print(json.load(open("resources.json"))["vault_id"])')
  echo "resources.json exists; nothing to create. To push YAML edits:"
  echo '  ant beta:agents update --agent-id $(id) --version $(current) < digest.agent.yaml'
  echo '  ant beta:environments update --environment-id $(id) < sandbox.env.yaml'
  echo
  echo "Module 5 MCP migration: the agent now reaches get_rated_signals over the co-mounted"
  echo "MCP server, authed by static_bearer vault credentials keyed to each tier's /mcp URL"
  echo "(one per URL; static_bearer can't wildcard, so preview PR URLs get added ad hoc)."
  echo "Add them to the existing vault ($VAULT_ID) once:"
  cat <<CMD
  ant beta:vaults:credentials create --vault-id $VAULT_ID <<'YAML'
display_name: sysdesign MCP bearer (prod)
auth:
  type: static_bearer
  mcp_server_url: https://sysdesign.thedefrag.ai/mcp
  token: \$SYSDESIGN_API_KEY
YAML
  ant beta:vaults:credentials create --vault-id $VAULT_ID <<'YAML'
display_name: sysdesign MCP bearer (local tunnel)
auth:
  type: static_bearer
  mcp_server_url: https://sysdesign-local.thedefrag.ai/mcp
  token: \$SYSDESIGN_API_KEY
YAML
CMD
  echo
  echo "(Expand \$SYSDESIGN_API_KEY from backend/.env first; the heredocs above quote it literally.)"
  exit 0
fi

ENV_ID=$(ant beta:environments create < sandbox.env.yaml --transform id -r)
echo "environment: $ENV_ID"

AGENT_ID=$(ant beta:agents create < digest.agent.yaml --transform id -r)
AGENT_VERSION=$(ant beta:agents retrieve --agent-id "$AGENT_ID" --transform version -r)
echo "agent: $AGENT_ID (version $AGENT_VERSION)"

# The description is written FOR the model: it's how future sessions decide
# what belongs in this store. Not a docstring for humans.
MEM_ID=$(ant beta:memory-stores create \
  --name sysdesign-digest-memory \
  --description "Weekly competitor-intel digests, one dated file per week. Read the most recent digest before writing a new one, for week-over-week comparison." \
  --transform id -r)
echo "memory store: $MEM_ID"

# The vault holds the API key the agent uses against the deployed API. Two
# credential shapes, one secret, because the agent hits the API two ways:
#   - environment_variable (X-API-Key header): for the plain curl the agent runs
#     from the sandbox (openapi.json, POST /digests, PUT the finished digest).
#     The sandbox only ever sees an opaque placeholder in $SYSDESIGN_API_KEY;
#     Anthropic substitutes the real value at egress, header-only, toward the
#     allowed hosts only.
#   - static_bearer (Authorization: Bearer): for the get_rated_signals MCP tool.
#     MCP credentials are keyed by mcp_server_url, and it's the ONLY way to
#     authenticate an MCP server; one credential per tier URL (static_bearer
#     can't wildcard, so preview PR URLs are added ad hoc). The /mcp path checks
#     the same SYSDESIGN_API_KEY, so both shapes carry the same secret.
# Secret comes from backend/.env, same place railway-env.py reads it, so the API
# and the vault can't drift apart silently.
VAULT_ID=$(ant beta:vaults create --display-name "sysdesign digest vault" --transform id -r)
CRED_ID=$(ant beta:vaults:credentials create --vault-id "$VAULT_ID" --transform id -r <<YAML
display_name: sysdesign API key (X-API-Key)
auth:
  type: environment_variable
  secret_name: SYSDESIGN_API_KEY
  secret_value: $SYSDESIGN_API_KEY
  networking:
    type: limited
    allowed_hosts:
      - sysdesign.thedefrag.ai
      - "*.up.railway.app"
      - sysdesign-local.thedefrag.ai  # Cloudflare tunnel to a laptop dev server (infra/README.md)
  injection_location:
    header: true
YAML
)
echo "vault: $VAULT_ID (X-API-Key credential $CRED_ID)"

# MCP bearer, one per tier URL. Prod and the local tunnel at bootstrap; preview
# PR URLs get added by hand when a preview needs the agent (static_bearer keys
# on the exact URL, no wildcards).
for MCP_URL in https://sysdesign.thedefrag.ai/mcp https://sysdesign-local.thedefrag.ai/mcp; do
  ant beta:vaults:credentials create --vault-id "$VAULT_ID" --transform id -r <<YAML
display_name: sysdesign MCP bearer ($MCP_URL)
auth:
  type: static_bearer
  mcp_server_url: $MCP_URL
  token: $SYSDESIGN_API_KEY
YAML
  echo "  mcp bearer: $MCP_URL"
done

cat > resources.json <<JSON
{
  "environment_id": "$ENV_ID",
  "agent_id": "$AGENT_ID",
  "agent_version": $AGENT_VERSION,
  "memory_store_id": "$MEM_ID",
  "vault_id": "$VAULT_ID",
  "vault_credential_id": "$CRED_ID"
}
JSON
echo "wrote resources.json"
