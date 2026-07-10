# Deploy infra (Module 4)

Railway project **sysdesign** (`12dffbd4-65bd-44f7-83b7-d30238c92892`), one `production`
environment, three services. Postgres is NOT here, it lives on Supabase
(project `bmrwhbubywwaxyyynvgx`, reached via the us-east-1 session pooler).

| Service | Source | Runs |
|---|---|---|
| `api` | GitHub `dfp-side-hustle/sysdesign`, root `backend/` | uvicorn via `backend/railway.api.json`, public domain |
| `worker` | same repo + root | Celery worker with beat embedded, via `backend/railway.worker.json`, private |
| `redis` | image `bitnami/redis:7.4` + volume | broker, result backend, SSE pub/sub, private |

## Where each piece of config lives (the IaC-lite contract)

We deliberately skipped Terraform. Railway has no official provider and the community one
is niche; the platform's own config-as-code story is a per-service `railway.json` plus
GitHub-connected auto-deploys. So the codified surface is

- **Build + run config** in `backend/railway.api.json` and `backend/railway.worker.json`
  (start command, healthcheck, restart policy). Reviewed in PRs like any code. Railway
  reads them because each service's settings point at the file (repo-root path, set once).
- **Env vars** in [railway-env.py](railway-env.py). The manifest in that script says which
  variables each service gets and where values come from (`backend/.env`, gitignored).
  `python3 infra/railway-env.py list` shows what's live on Railway with secrets redacted;
  `sync --dry` diffs manifest vs remote and flags drift (vars on Railway the manifest
  doesn't know about); `sync` pushes. No more mystery vars.
- **One-time provisioning** (project, services, volume, domains) was done over Railway's
  GraphQL API with the project-scoped token and is documented here rather than replayed by
  a tool. It changes rarely; when it changes, update this file in the same PR.

Redis auth is one `REDIS_PASSWORD` on the redis service (generated at provision time, not
managed by sync). api + worker consume it through Railway reference templates
(`${{redis.REDIS_PASSWORD}}` / `${{redis.RAILWAY_PRIVATE_DOMAIN}}`), so a rotation is
edit-one-place then redeploy.

## Deploys

Push to `main` deploys api + worker (Railway GitHub integration, PR environments off).
Redis redeploys only when its image/config changes. The project-scoped token in
`backend/.env` (`RAILWAY_PROJECT_TOKEN`) drives `railway` CLI status/logs/redeploys and
the GraphQL calls in `railway-env.py`. It cannot touch other projects.

Connecting a service to GitHub is the one thing the token cannot do (it's a user-identity
+ GitHub-app grant), so that step happens once in the dashboard.
