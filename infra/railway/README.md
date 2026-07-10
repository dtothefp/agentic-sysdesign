# Railway infra (Module 4 deploy)

Terraform for the sysdesign deploy topology on [Railway](https://railway.com): a project
with three services in one `production` environment.

- **api** builds `backend/` from GitHub, runs the FastAPI surface, gets a public
  `*.up.railway.app` domain. This is the tool surface the Module 5 managed agent curls.
- **worker** builds the same `backend/`, runs the Celery worker with beat embedded
  (`--beat`), private-network only.
- **redis** runs `bitnami/redis:7.4` as the broker, result backend, and SSE pub/sub bus,
  private-network only.

Postgres is **not** here. It lives on Supabase; `database_url` points services at the
session pooler. Two config files in `backend/` (`railway.api.json`, `railway.worker.json`)
carry the per-service start commands, since the provider has no start-command field.

## Prerequisites

1. **An account/workspace token**, not a project token. Create it at
   [railway.com/account/tokens](https://railway.com/account/tokens). The project-scoped
   token that drives the Railway CLI cannot manage resources through the API this provider
   uses. Export it: `export RAILWAY_TOKEN=...`
2. **The Railway GitHub app installed** on `dfp-side-hustle/sysdesign`, so Railway can build
   the api + worker from source.
3. Terraform >= 1.6, and the values from `backend/.env` (Supabase pooler string, Apify key).

## Run (local, for now)

```bash
cd infra/railway
cp terraform.tfvars.example terraform.tfvars   # fill database_url + apify_api_key
export RAILWAY_TOKEN=...                        # account token
terraform init
terraform plan
terraform apply
```

`terraform output api_url` prints the public API URL once services are up. CI comes later;
for now this runs from the local machine with the token in the environment.

## What's deliberately parameterized

- `api_subdomain` (default `sysdesign-api`) must be globally unique on Railway. If apply
  reports a collision, change it in tfvars.
- `railway_workspace_id` is only needed if your token sees more than one workspace.
- `anthropic_api_key` stays unset until the Module 3 rating stage ships; the worker variable
  is only created when it's non-empty, so setting it later (or in the UI) won't get clobbered.

## Known considerations to verify at first apply

These can't be validated until an account token exists and apply actually runs.

- **Redis over the private network.** The bitnami image with `REDIS_PASSWORD` set enables
  auth, which is what lets a cross-service (non-loopback) connection through. If the worker
  still can't reach Redis, the fallback is Railway's one-click Redis template; reference its
  injected `REDIS_URL` instead of this image service.
- **Reference variable resolution.** Services address Redis via
  `${{redis.RAILWAY_PRIVATE_DOMAIN}}`, resolved by Railway at deploy. If a deploy log shows
  the literal token unexpanded, the service name in the reference (`redis`) doesn't match the
  actual service name; fix the reference, not the service.
- **Nixpacks + uv.** Build relies on Nixpacks detecting `backend/uv.lock` and putting `uv` on
  the runtime PATH for the `uv run ...` start commands. If detection fails, add a Dockerfile
  and point the service at it instead.
- **Config file path.** `config_path` is set to the bare filename (`railway.api.json`),
  assumed relative to the service `root_directory` (`/backend`). If Railway can't find it,
  it's resolving relative to the repo root instead; prefix the path with `backend/`.

## State

State holds the resolved `database_url`, Apify key, and the generated Redis password in
plaintext, so it's gitignored and stays local. After the first `terraform init`, un-ignore
and commit `.terraform.lock.hcl` for reproducible provider versions.
