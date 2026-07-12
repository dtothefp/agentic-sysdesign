# Deploy infra (Module 3)

Railway project **sysdesign** (`12dffbd4-65bd-44f7-83b7-d30238c92892`), one `production`
environment, three services. Postgres is NOT here, it lives on Supabase
(project `bmrwhbubywwaxyyynvgx`, reached via the us-east-1 session pooler).

| Service | Source | Runs |
|---|---|---|
| `api` | GitHub `dtothefp/agentic-sysdesign`, root `backend/` | uvicorn via `backend/railway.api.json`, public domain |
| `worker` | same repo + root | Celery worker with beat embedded, via `backend/railway.worker.json`, private |
| `redis` | image `redis:7-alpine` + volume | broker, result backend, SSE pub/sub, private |

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

### Gotcha: redis start command must run through a shell

The redis start command is

```
sh -c 'redis-server --requirepass "$REDIS_PASSWORD" --appendonly yes --dir /data'
```

The `sh -c` wrap is load-bearing. Railway exec's an image service's start command
directly, with no shell, so a bare `redis-server --requirepass $REDIS_PASSWORD` passes the
literal seven characters `$REDIS_PASSWORD` as the password (no expansion). Redis then
enforces that literal string while api + worker send the real 48-char rendered value, and
every connection fails with `WRONGPASS invalid username-password pair`. Wrapping in
`sh -c '...'` gives you the shell that expands `$REDIS_PASSWORD`. (The `bitnami/redis`
image sidesteps this by reading `REDIS_PASSWORD` from the env natively; plain
`redis:7-alpine` does not, so the flag plus a shell is required.)

### Gotcha: a custom domain needs the ownership TXT, not just the CNAME

Railway asks for two DNS records per custom domain, a `CNAME` to the assigned
`*.up.railway.app` target and a `TXT _railway-verify.<subdomain>` carrying a
`railway-verify=...` token. The CNAME alone routes traffic, but the cert sits in
`CERTIFICATE_STATUS_TYPE_VALIDATING_OWNERSHIP` until the TXT resolves; the edge keeps
serving the default `*.up.railway.app` wildcard cert in the meantime, so HTTPS to the
custom host fails SAN validation. Both records live in Cloudflare, DNS-only (proxied off),
so Railway's own Let's Encrypt challenge can reach the origin. Pull the exact token with
the `verificationDnsHost` / `verificationToken` fields on the custom domain's `status`.

## Migrations

The api runs migrations before it takes traffic, via the `preDeployCommand` in
`railway.api.json`:

```
uv run python db/migrate.py
```

The command is scoped to the production environment (nested under
`environments.production` in the config file, Railway's per-environment override
syntax) so PR preview environments never migrate. Previews share the production
Supabase database, and a PR branch carrying a new migration must not rewrite the
shared schema just by deploying a preview. See the preview section below for the
flip side of that hazard.

Railway runs `preDeployCommand` once, inside the freshly built image, before the new
version goes live. So a deploy can't serve code that expects a column the database doesn't
have yet, the schema is caught up first, or the deploy fails and the old version keeps
serving. It reads `DATABASE_URL` (the api's, the Supabase session pooler on 5432, which
supports the DDL a migration needs).

[db/migrate.py](../backend/db/migrate.py) is a ~50-line applier over dbmate's own file
format. dbmate is a Go binary and this image is a uv/Python build, so rather than wrangle
the binary into the image it reads the same `db/migrations/*.sql` files and writes the same
`schema_migrations` table dbmate uses, with psycopg (already a dependency). dbmate stays the
local tool (`make migrate`, `make new`), this is the prod applier, and the two are
interchangeable, `dbmate status` reads the table the runner wrote and reports every
migration applied. It's idempotent (already-applied versions skip), each migration commits
atomically, and it honors a `transaction:false` marker for the `CREATE INDEX CONCURRENTLY`
case.

Local dev is unchanged, keep using `make migrate`. A `transaction:false` migration must be
a single statement (Postgres won't run `CONCURRENTLY` inside an implicit transaction block).

## Deploys

Push to `main` deploys api + worker (Railway GitHub integration, PR environments off).
The api's deploy runs the migration step above first. Redis redeploys only when its
image/config changes. The project-scoped token in `backend/.env`
(`RAILWAY_PROJECT_TOKEN`) drives `railway` CLI status/logs/redeploys and the GraphQL calls
in `railway-env.py`. It cannot touch other projects.

Connecting a service to GitHub is the one thing the token cannot do (it's a user-identity
+ GitHub-app grant), so that step happens once in the dashboard.

## PR preview environments (opt-in, label-gated)

Not every PR gets an environment. Add the **`preview`** label to a PR and
[.github/workflows/preview-env.yml](../.github/workflows/preview-env.yml) creates a Railway
environment named `pr-<number>` cloned from production, with api + worker pointed at the PR
branch and a fresh Railway-provided domain on the api (commented on the PR). Remove the
label, or close/merge the PR, and the environment is deleted. Further pushes to the branch
auto-deploy with no Action run, because the cloned deployment triggers track the branch.

Railway's native "PR environments" feature can't do this. It's a project-level toggle that
fires on every PR, with no per-PR or label-based gating (verified against the docs
2026-07-10, only enterprise "Focused PR environments" narrows anything, and by changed
services, not by PR). So the workflow drives the GraphQL API directly through
[railway-preview.py](railway-preview.py), which documents the exact mutation sequence
(environmentCreate with `sourceEnvironmentId` + `skipInitialDeploys`, deploymentTriggerUpdate
to the PR branch, serviceDomainCreate for the api, serviceInstanceDeployV2 per service,
environmentDelete on teardown).

### Token: this needs the WORKSPACE token, not the project token

The project-scoped token (`RAILWAY_PROJECT_TOKEN`) is scoped to the production environment.
Empirically (2026-07-10) it CAN run `environmentCreate`, but it can't read, retarget, or
delete the environment it just created, which is worse than useless (it strands an
environment only a broader token can remove). Environment management runs on a workspace
token instead, stored as

- the `RAILWAY_WORKSPACE_TOKEN` repo secret (what the Action uses), and
- `RAILWAY_WORKSPACE_TOKEN` in `backend/.env` for local runs of the script.

Rotating it means updating both. `railway-env.py` (env-var sync) stays on the
narrower project token; only preview create/teardown needs the wide one. Fork PRs never
receive the secret (GitHub's rule), so previews only work for branches pushed to this repo,
which is the intended scope.

### The shared-database hazard (read this before trusting a preview)

Preview environments clone production's env vars, including `DATABASE_URL`. There's no
preview database. That means

- **Migrations don't run in previews** (production-scoped `preDeployCommand`, above). A PR
  that adds a migration deploys preview code against the OLD shared schema, so endpoints
  touching the new column/table will 500 in the preview. That's the accepted trade-off;
  apply the migration locally to test it, the preview verifies everything else.
- **Preview writes are production writes.** A `demo` or `live` run started against a
  preview api inserts real rows into the shared Supabase database. The worker's beat
  backstops also run (the matview refresh is idempotent and harmless, the unrated sweep
  stays inert while `RATING_MODEL` is unset in the cloned vars).
- Redis IS per-preview (each environment gets its own instance), so queues and SSE
  pub/sub don't cross between preview and prod.

If previews ever need real isolation, the move is a Supabase branch database per PR wired
into the same workflow, punted for now (see `packages/package-supabase/` consolidation).

### One-time setup already done (dashboard-free, for the record)

The `preview` GitHub label and the `RAILWAY_WORKSPACE_TOKEN` repo secret were created with
`gh` (2026-07-10). No Railway dashboard steps were needed. The cloned services reuse the
existing GitHub connection, so the "token can't connect GitHub repos" constraint never
bites; connecting the repo happened once for production and clones inherit it.

## Local testing with the Managed Agent (Cloudflare tunnel)

The Module 5 agent's sandbox runs in Anthropic's cloud, so it can't reach `localhost`. To
test the digest loop against a laptop dev server, a named Cloudflare tunnel publishes the
local API at a stable hostname the vault credential already allows.

- Hostname: `https://sysdesign-local.thedefrag.ai` -> `localhost:8000` (tunnel
  `sysdesign-local`, id `dd6113aa-...`, config in `~/.cloudflared/config.yml` on David's
  machine, created 2026-07-11 via `cloudflared tunnel login` + `create` + `route dns`).
- The vault credential's `allowed_hosts` includes this hostname, so the agent's
  `X-API-Key` is substituted toward it exactly like prod. Keep `m5_agents/apply.sh` and
  the live credential in sync when hosts change.
- Run it with `cloudflared tunnel run sysdesign-local`. Nothing listens until the local
  API is up; the tunnel itself only ever holds an OUTBOUND connection to Cloudflare's
  edge, which is why it works behind NAT with no router config.
- Full local loop: local api + redis + worker running (dev container), digests migration
  applied locally, then `POST /digests {"base_url": "https://sysdesign-local.thedefrag.ai"}`
  against the LOCAL api. The worker babysits the session, the sandbox curls back in
  through the tunnel, and the digest row completes in the local database.
- The tunnel exposes the local API to the whole internet, not just Anthropic. Data routes
  all require `X-API-Key`, which is the thing making that acceptable. Never tunnel an
  unauthenticated service.
