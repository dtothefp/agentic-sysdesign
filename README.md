# agentic-sysdesign

```
                *        .           ✦                 .    ·        *
      ·                 _..._                                   .
            ✦         .::::: `.                                       ✦
                     :::::::::  \       T O   T H E   M O O N !
     *               ::::::::::  |
         .           `:::::::::  /                           ·
              ·        `::::: _.'              *        .
       ✦                 `"'          ✦
                  .                          .        ·           *
   ·        *                  ✦                            .
                                                                     ·
   scrape ──▶ fan out ──▶ rate ──▶ embed ──▶ search ──▶ chat
```

A playground and a skeleton for building agentic applications with fun tools.

[Apify](https://apify.com) · [Celery](https://docs.celeryq.dev) · [FastAPI](https://fastapi.tiangolo.com) · [pgvector](https://github.com/pgvector/pgvector) · [Supabase](https://supabase.com) · [Railway](https://railway.com) · [moon](https://moonrepo.dev) · [Anthropic Managed Agents](https://docs.claude.com/en/docs/agents-and-tools/managed-agents)

Live in production for a few dollars a month. Fork it and steal the parts you
like. Issues and PRs welcome.

## The pipeline

```
  Instagram posts
        │  Apify scrape, driven by an in-repo Claude Code skill
        ▼
  FastAPI ──── POST /runs ────▶ Celery chord fan-out
        │                       one task per creator, progress
        │                       streamed to the browser over SSE
        ▼                            │
  Postgres on Supabase               ▼
   partitioned tables         rate + embed stages
   full-text GIN index        one decoupled Celery job per new
   pgvector HNSW index        signal, LLM behind one adapter
        │
        ▼
  hybrid search (lexical + semantic, fused with RRF)
        │
        ├──▶ chat agents   ReAct loops over the REST API,
        │                  Python and TypeScript side by side
        └──▶ digest bot    scheduled Anthropic Managed Agent,
                           clusters the week's themes, writes digests
```

## The demo tour

The app grew in modules. Each finished module has a git tag (`module-1`,
`module-2`, ...) so any milestone is one `git checkout` away.

| Module | What it demos |
|---|---|
| 1. Data model | Partitioned Postgres with per-partition indexes. A materialized view. EXPLAIN drills you can run yourself |
| 2. Celery fan-out | `POST /runs` fans out a chord (one task per creator). Progress streams over Server-Sent Events. The matview refreshes on fan-in |
| 3. Deploy | Railway for api + worker + Redis + agent. Supabase Postgres with pgvector. Migrations run as a pre-deploy step so code never ships ahead of schema |
| 4. LLM rating layer | One structured-output rating job per new signal through a provider-agnostic adapter. Local dev rates through Ollama for free. Prod rents a hosted model |
| 5. Managed Agents | A scheduled Anthropic Managed Agent pulls the week's rated signals through the deployed API over MCP. Compares against last week via a Memory Store. Writes digests |
| 6. Hybrid search | Postgres full-text + pgvector HNSW run in parallel. Fused with Reciprocal Rank Fusion. No embedding key means lexical-only and it says so |
| 7. Chat agents | The same ReAct loop written twice. Python is a sync generator + thread bridge. TypeScript is a native async generator. Same wire contract so you can read the two concurrency models against each other |

Deeper write-ups live in `docs/`. One per module with the design reasoning and
ASCII diagrams.

## The stack and why it's fun

- **[moon](https://moonrepo.dev)** is the one task runner. A standalone Rust binary.
  No node. Project-scoped tasks (`moon run api:dev`, `moon run :test`). If you've
  fought Lerna or a wall of Makefiles moon is the palate cleanser.
- **[uv](https://docs.astral.sh/uv/) workspace.** One `uv.lock` at the root covers
  every Python package. `uv sync --all-packages` and you're done.
- **Railway + Supabase instead of a cloud giant.** Four Railway services and a
  Supabase Postgres run this whole thing for pocket change. Config lives as code
  (`railway.json` per service). Every PR gets its own preview environment. pgvector
  is one `CREATE EXTENSION` away.
- **Dev containers three ways.** A `.devcontainer/` with its own sibling Postgres
  (works in Cursor and VS Code and never collides with your host's 5432). A
  `.cursor/environment.json` for Cursor's cloud agents. Plain host dev with Docker
  compose. Pick whichever. The moon tasks are identical in all three.
- **Inert until keyed.** Every paid layer (rating, embeddings) switches off cleanly
  when its env var is unset. The demo runs end to end with zero API keys.
  `{"mode": "demo"}` runs generate synthetic signals so there's no scraping spend
  either.
- **Own the interface, rent the model.** The LLM adapters speak the
  OpenAI-compatible wire shape over raw urllib. The same code rates through local
  [Ollama](https://ollama.com) in the container and a hosted model in prod. Which
  model rates a run is request data not deployment config.

## Layout

```
moon.yml                 root moon project, workspace lifecycle (root:setup, root:up, ...)
.moon/                   moon workspace config, pins the moon version
apps/chat-web/           React chat UI (scaffold)
services/api/            FastAPI surface + railway.json + tests
services/worker/         Celery worker + beat + railway.json
services/agent/          chat agent, Python. ReAct loop, CLI + SSE server, deployed on Railway
services/agent-ts/       chat agent, TypeScript. Same contract, native async generators
services/managed-agents/ Anthropic Managed Agents config (agent.yaml) + agentctl.py
packages/core/           shared code, db migrations, EXPLAIN drills
packages/task-contract/  task names + send-only Celery client (api never imports worker)
infra/                   Railway env-var sync + per-PR preview environments
.claude/skills/          in-repo Claude Code skills (the scraper is a skill, not a cron)
.devcontainer/           reproducible container with its own Postgres
```

Python everywhere except `services/agent-ts/`. That one is a deliberate TypeScript
island (Node 22, [oxfmt](https://oxc.rs) + oxlint + knip + tsgo toolchain) so the
two agent runtimes can be compared honestly.

## Quick start

Needs Docker, [uv](https://docs.astral.sh/uv/), `psql`, [dbmate](https://github.com/amacneil/dbmate),
and [moon](https://moonrepo.dev) (`brew install moon dbmate`). Or open the dev
container and skip the installs.

```bash
uv sync --all-packages
moon run root:setup      # db up + migrate + seed, one shot (host)
moon run core:db-init    # same, inside the dev container (db is a sibling there)

moon run api:dev         # FastAPI at :8000, Swagger at /docs
moon run worker:dev      # Celery worker (needs Redis, compose provides it)
```

The fastest end-to-end demo needs no keys at all. It exercises the chord fan-out,
the SSE stream, and the matview refresh.

```bash
curl -X POST localhost:8000/runs -H 'content-type: application/json' \
  -d '{"mode":"demo","limit":5}'
curl -N localhost:8000/runs/<run_id>/stream    # watch the fan-out live
```

Talking to the chat agent needs `ANTHROPIC_API_KEY` in the repo-root `.env`.

```bash
uv run --package sysdesign-agent python -m agent "what creators do we track?"
cd services/agent-ts && npm ci && npm run chat -- "same question, different runtime"
```

`moon run :lint` and `moon run :test` sweep every project. Tests marked
`integration` auto-skip when Postgres is down. The suite is green even cold.

## Migrations

Plain SQL files via dbmate. No ORM. Each one has a `migrate:up` and a
`migrate:down`. They run automatically as Railway's pre-deploy step so a deploy
can't ship code ahead of its schema.

```bash
moon run core:migrate     # apply pending
moon run core:status      # what's run, what's pending
moon run core:rollback    # undo the latest
NAME=add_thing moon run core:new   # scaffold the next one
```

The raw SQL is the point. Partition pruning, `ON CONFLICT` idempotency, and index
choices stay visible instead of hiding behind an ORM. Six
`EXPLAIN (ANALYZE, BUFFERS)` drills in `packages/core/drills/` walk the query
plans one at a time.

## The one idempotency sentence

At-least-once delivery is assumed everywhere. Every write is an idempotent upsert
keyed on `(influencer_id, content_hash, captured_at)`. Reprocessing the same item
twice is a no-op. That answers most of the "what if it runs twice" questions in
the whole pipeline.

## Contributing

This is a demo repo so the bar is "does it teach something." Small focused PRs
that sharpen a module, add a drill, or port a pattern to another runtime are all
fair game. Feature branches and PRs, never straight to `main`. Each completed
module gets a tag.
