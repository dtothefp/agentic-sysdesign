# Deploy infra (Module 3)

Railway project **sysdesign** (`12dffbd4-65bd-44f7-83b7-d30238c92892`), one `production`
environment, three services. Postgres is NOT here, it lives on Supabase
(project `bmrwhbubywwaxyyynvgx`, reached via the us-east-1 session pooler).

| Service | Source | Runs |
|---|---|---|
| `api` | GitHub `dfp-side-hustle/sysdesign`, root `backend/` | uvicorn via `backend/railway.api.json`, public domain |
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
