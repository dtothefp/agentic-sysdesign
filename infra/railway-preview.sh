#!/usr/bin/env bash
# Opt-in PR preview environments for sysdesign, driven by the Railway CLI.
#
# Replaces the hand-rolled GraphQL client (railway-preview.py). The CLI wraps the
# same backboard API but tracks it for us, so an API change becomes a version bump
# of @railway/cli instead of a code change here. It also resolves services by name,
# so this script carries no service UUIDs (only the stable project id).
#
# Driven by .github/workflows/preview-env.yml: adding the "preview" label to a PR
# creates a Railway environment pr-<number> cloned from production and pointed at
# the PR branch; removing the label (or closing the PR) deletes it. Runnable
# locally too, with RAILWAY_API_TOKEN exported:
#
#     RAILWAY_API_TOKEN=<workspace-token> infra/railway-preview.sh up 42 my-branch
#     RAILWAY_API_TOKEN=<workspace-token> infra/railway-preview.sh down 42
#
# Auth is the WORKSPACE token, passed to the CLI as RAILWAY_API_TOKEN. The
# project-scoped token (RAILWAY_TOKEN) is scoped to one environment, so it can
# create an environment but cannot retarget or delete it (see infra/README.md).
# The workspace token fails `railway whoami` (a user-level query) but authorizes
# project-scoped environment lifecycle, which is all this script does.
#
# What "up" does, all idempotent:
#   1. environment new pr-<n> --duplicate production, unless it already exists.
#      Duplication copies services, variables, and config (including the shared
#      Supabase DATABASE_URL, a hazard documented in infra/README.md).
#   2. environment edit --service-config <svc> source.branch <branch> for api and
#      worker, so both git-source services track the PR branch. After this, pushes
#      to the branch auto-deploy to the preview, no Action run needed.
#   3. domain --service api, which returns the existing railway-provided domain or
#      generates one (duplication does not copy domains).
#   4. redeploy each service so the initial preview build runs on the PR branch.
#
# Migrations do NOT run in preview environments. railway.api.json scopes the
# preDeployCommand to production because previews share the production database.
set -euo pipefail

PROJECT_ID="12dffbd4-65bd-44f7-83b7-d30238c92892"  # sysdesign
PROD_ENV="production"
API_SERVICE="api"
# redis first so the broker is up while worker/api build
DEPLOY_ORDER=(redis worker api)

log() { printf '>> %s\n' "$*" >&2; }

require_token() {
  if [[ -z "${RAILWAY_API_TOKEN:-}" ]]; then
    echo "RAILWAY_API_TOKEN not set. This must be the workspace token (the CLI reads" \
         "it as RAILWAY_API_TOKEN); the project token can't manage environments." >&2
    exit 1
  fi
}

env_exists() {  # $1 = environment name
  railway environment list --json 2>/dev/null \
    | jq -e --arg n "$1" '.environments[]? | select(.name == $n)' >/dev/null 2>&1
}

cmd_up() {
  local pr="$1" branch="$2" name="pr-$1" dom
  require_token

  log "linking project $PROJECT_ID ($PROD_ENV) for environment lifecycle"
  railway link --project "$PROJECT_ID" --environment "$PROD_ENV" >/dev/null

  if env_exists "$name"; then
    log "environment $name already exists"
  else
    log "creating $name as a duplicate of $PROD_ENV"
    railway environment new "$name" --duplicate "$PROD_ENV" >/dev/null
  fi

  log "retargeting git branch -> $branch (api, worker)"
  railway environment edit \
    --environment "$name" \
    --service-config "$API_SERVICE" source.branch "$branch" \
    --service-config worker source.branch "$branch" \
    --message "preview: track $branch"

  log "linking $name for domain + deploy"
  railway link --project "$PROJECT_ID" --environment "$name" >/dev/null

  log "ensuring api has a railway-provided domain"
  dom="$(railway domain --service "$API_SERVICE" --json 2>/dev/null \
        | jq -r '.domain // .domains[0].domain // .serviceDomain // empty')"
  if [[ -z "$dom" ]]; then
    # fall back to parsing the human output if the json shape differs
    dom="$(railway domain --service "$API_SERVICE" 2>/dev/null \
          | grep -oE '[a-z0-9.-]+\.up\.railway\.app' | head -1)"
  fi

  log "deploying services on $name"
  for svc in "${DEPLOY_ORDER[@]}"; do
    if railway redeploy --service "$svc" --yes >/dev/null 2>&1; then
      log "redeploy queued: $svc"
    else
      log "redeploy $svc skipped (no prior deployment yet; the branch trigger will deploy it)"
    fi
  done

  # last line is machine-readable; the workflow greps it for the PR comment
  echo "PREVIEW_URL=https://${dom}"
}

cmd_down() {
  local name="pr-$1"
  require_token
  railway link --project "$PROJECT_ID" --environment "$PROD_ENV" >/dev/null 2>&1 || true
  if env_exists "$name"; then
    railway environment delete "$name" --yes
    log "deleted environment $name"
  else
    log "environment $name not found, nothing to delete"
  fi
}

main() {
  case "${1:-}" in
    up)
      [[ $# -eq 3 ]] || { echo "usage: $0 up <pr> <branch>" >&2; exit 2; }
      cmd_up "$2" "$3"
      ;;
    down)
      [[ $# -eq 2 ]] || { echo "usage: $0 down <pr>" >&2; exit 2; }
      cmd_down "$2"
      ;;
    *)
      echo "usage: $0 up <pr> <branch> | down <pr>" >&2
      exit 2
      ;;
  esac
}

main "$@"
