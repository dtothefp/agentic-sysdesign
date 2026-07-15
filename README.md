# agentic-sysdesign

```
                *        .           ✦                 .    ·        *
      ·                 _..._                                   .
            ✦         .::::: `.                                       ✦
                     :::::::::  \       m o o n   r u n s
     *               ::::::::::  |        t h e   t a s k s
         .           `:::::::::  /                           ·
              ·        `::::: _.'              *        .
       ✦                 `"'          ✦
                  .                          .        ·           *
   ·        *                  ✦                            .
                                                                     ·
   scrape ──▶ fan out ──▶ rate ──▶ embed ──▶ search ──▶ chat
```

A working demo of an agentic data pipeline, built as one real app that's live in
production. Instagram posts get scraped, fanned out through a Celery worker queue,
rated by an LLM, embedded into pgvector, served through hybrid semantic search, and
reasoned over by chat agents written twice (Python and TypeScript) plus a scheduled
[Anthropic Managed Agent](https://docs.claude.com/en/docs/agents-and-tools/managed-agents)
that writes a weekly digest. The whole thing deploys on
[Railway](https://railway.com) and [Supabase](https://supabase.com) for a few dollars
a month.

Think of it like the demos folder of a framework repo. Every piece is small enough to
read in a sitting, and because the whole thing is actually deployed and running, you're
not taking my word for any of it. Fork it and steal the parts you like, and if you
break something interesting along the way, issues and PRs are welcome.

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

Every product in the diagram gets linked once here. [Apify](https://apify.com) does
the scraping and [Celery](https://docs.celeryq.dev) runs the queue, while
[pgvector](https://github.com/pgvector/pgvector) holds the embeddings and
[FastAPI](https://fastapi.tiangolo.com) is the surface it all comes out of.

## The demo tour

The app grew in modules, and each finished module has a git tag (`module-1`,
`module-2`, ...) so any milestone is one `git checkout` away.

| Module | What it demos |
|---|---|
| 1. Data model | Partitioned Postgres with per-partition indexes, a materialized view, and EXPLAIN drills you can run yourself |
| 2. Celery fan-out | `POST /runs` fans out a chord (one task per creator) while progress streams over Server-Sent Events, and the matview refreshes on fan-in |
| 3. Deploy | Railway for api + worker + Redis + agent, with Supabase Postgres providing pgvector. Migrations run as a pre-deploy step so code never ships ahead of schema |
| 4. LLM rating layer | Each new signal gets one structured-output rating job through a provider-agnostic adapter, so local dev rates through Ollama for free and prod rents a hosted model |
| 5. Managed Agents | A scheduled Anthropic Managed Agent pulls the week's rated signals through the deployed API over MCP, compares them against last week via a Memory Store, and writes digests |
| 6. Hybrid search | Postgres full-text + pgvector HNSW run in parallel and get fused with Reciprocal Rank Fusion. With no embedding key it degrades to lexical-only and says so |
| 7. Chat agents | The same ReAct loop written twice, Python (sync generator + thread bridge) vs TypeScript (native async generator) on the same wire contract, so you can read the two concurrency models against each other |

Deeper write-ups live in `docs/` (one per module, with the design reasoning and
ASCII diagrams).

## The stack, and why it's fun

- **[moon](https://moonrepo.dev)** is the one task runner, a standalone Rust binary
  with no node required and project-scoped tasks (`moon run api:dev`, `moon run :test`).
  If you've fought Lerna or a wall of Makefiles, moon is the palate cleanser.
- **[uv](https://docs.astral.sh/uv/) workspace.** One `uv.lock` at the root covers
  every Python package, so `uv sync --all-packages` and you're done.
- **Railway + Supabase instead of a cloud giant.** Four Railway services and a
  Supabase Postgres run this whole thing for pocket change. Config lives as code
  (`railway.json` per service), every PR gets its own preview environment, and
  pgvector is one `CREATE EXTENSION` away.
- **Dev containers, three ways.** A `.devcontainer/` with its own sibling Postgres
  (works in Cursor and VS Code and never collides with your host's 5432), a
  `.cursor/environment.json` for Cursor's cloud agents, and plain host dev with
  Docker compose. Pick whichever you like, since the moon tasks are identical in
  all three.
- **Inert until keyed.** Every paid layer (rating, embeddings) switches off cleanly
  when its env var is unset, so the demo runs end to end with zero API keys.
  `{"mode": "demo"}` runs generate synthetic signals, so there's no scraping spend
  either.
- **Own the interface, rent the model.** The LLM adapters speak the
  OpenAI-compatible wire shape over raw urllib, so the same code rates through
  local [Ollama](https://ollama.com) in the container and a hosted model in prod,
  and which model rates a given run is request data rather than deployment config.

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

Python everywhere except `services/agent-ts/`, which is a deliberate TypeScript
island (Node 22, [oxfmt](https://oxc.rs) + oxlint + knip + tsgo toolchain) so the
two agent runtimes can be compared honestly.

## Quick start

It needs Docker, [uv](https://docs.astral.sh/uv/), `psql`, [dbmate](https://github.com/amacneil/dbmate),
and [moon](https://moonrepo.dev) (`brew install moon dbmate`), or you can open the
dev container and skip the installs entirely.

```bash
uv sync --all-packages
moon run root:setup      # db up + migrate + seed, one shot (host)
moon run core:db-init    # same, inside the dev container (db is a sibling there)

moon run api:dev         # FastAPI at :8000, Swagger at /docs
moon run worker:dev      # Celery worker (needs Redis, compose provides it)
```

The fastest end-to-end demo needs no keys at all, and it exercises the chord
fan-out, the SSE stream, and the matview refresh.

```bash
curl -X POST localhost:8000/runs -H 'content-type: application/json' \
  -d '{"mode":"demo","limit":5}'
curl -N localhost:8000/runs/<run_id>/stream    # watch the fan-out live
```

To talk to the chat agent you'll need `ANTHROPIC_API_KEY` in the repo-root `.env`.

```bash
uv run --package sysdesign-agent python -m agent "what creators do we track?"
cd services/agent-ts && npm ci && npm run chat -- "same question, different runtime"
```

`moon run :lint` and `moon run :test` sweep every project. Tests marked
`integration` auto-skip when Postgres is down, so the suite is green even cold.

## Migrations

Migrations are plain SQL files via dbmate, with no ORM in the way. Each one has a
`migrate:up` and a `migrate:down`, and they run automatically as Railway's
pre-deploy step so a deploy can't ship code ahead of its schema.

```bash
moon run core:migrate     # apply pending
moon run core:status      # what's run, what's pending
moon run core:rollback    # undo the latest
NAME=add_thing moon run core:new   # scaffold the next one
```

The raw SQL is the point, because partition pruning, `ON CONFLICT` idempotency, and
index choices stay visible instead of hiding behind an ORM. Six
`EXPLAIN (ANALYZE, BUFFERS)` drills in `packages/core/drills/` walk the query
plans one at a time.

## The one idempotency sentence

At-least-once delivery is assumed everywhere, so every write is an idempotent
upsert keyed on `(influencer_id, content_hash, captured_at)` and reprocessing the
same item twice is a no-op. That one sentence answers most of the "what if it runs
twice" questions in the whole pipeline.

## Contributing

This is a demo repo, so the bar is "does it teach something." Small focused PRs
that sharpen a module, add a drill, or port a pattern to another runtime are all
fair game. Work happens on feature branches with PRs rather than straight to
`main`, and each completed module gets a tag.
