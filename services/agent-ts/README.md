# sysdesign-agent-ts

A TypeScript rebuild of the Module 7 chat agent, running side by side with the Python one
(`packages/agent/`). Same job, same wire contract, different runtime. It's a ReAct loop over the
sysdesign REST API, exposed as a CLI and an SSE server, hostable as its own Railway service.

The point of building it twice is the concurrency contrast (below). Everything else is deliberately
one-to-one with the Python agent so you can read them against each other.

## The one interesting difference, sync generator plus thread bridge vs native async generator

The Python `run_agent` is a **sync** generator (sync Anthropic client, sync httpx tools). Iterating it
straight from an async handler would block the event loop for the whole model call, so the FastAPI
server pulls each event in a worker thread via `asyncio.to_thread(next, it, sentinel)`.

Here `runAgent` is a **native async generator**. Every `await` inside it (the model stream, each tool
`fetch`) already yields the event loop, so the Hono server just does:

```ts
for await (const ev of runAgent(body.message, { history })) {
  await stream.writeSSE({ event: ev.type, data: JSON.stringify(ev) });
}
```

No thread bridge, no sentinel, no sync/async seam. Same loop, one fewer moving part, because the
language's I/O is async from the start. That's the whole reason to have both in front of you.

## Layout

```
src/types.ts    the shared shapes (Block, CompleteEvent, Complete, Message, ToolSchema, AgentEvent)
src/tools.ts    the 8 HTTP tools (fetch clients of services/api) plus the Toolbox
src/loop.ts     runAgent (async generator), anthropicComplete (the streamed model call), SYSTEM
src/trace.ts    optional LangSmith seam, identity no-op until you wire the langsmith JS SDK
src/server.ts   Hono app: GET /health (open), POST /chat (SSE, X-API-Key gated)
src/index.ts    server entry (Railway runs this)
src/cli.ts      CLI entry (npm run chat)
test/loop.test.ts   the loop spec, one-to-one with the Python tests/test_loop.py
```

## Run it

```bash
npm install

# talk to a local api (start it first: moon run api:dev on :8000)
npm run chat -- "what creators do we track?"

# or point at prod
SYSDESIGN_API_URL=https://sysdesign.thedefrag.ai npm run chat -- "what did creators say about MCP?"

# the SSE server (defaults to :8100, same port as the Python agent's dev server)
npm run dev
curl -N -X POST localhost:8100/chat -H 'content-type: application/json' \
  -d '{"message":"start a demo run"}'

npm run typecheck   # tsc over src plus test
npm test            # node:test via tsx, no network
npm run build       # tsc into dist/, what Railway builds
```

## Env

Same variables as the Python agent, same inert-until-keyed contract.

- `ANTHROPIC_API_KEY`, required for real model calls (tests don't need it).
- `SYSDESIGN_API_URL`, the data API base (default `http://localhost:8000`).
- `SYSDESIGN_API_KEY`, dual purpose. It's sent as `X-API-Key` to the data API, and it's the gate on
  this service's own `/chat`. Unset means both are open (local dev), set means both are closed.
  `/health` is always open so Railway's healthcheck passes.
- `CHAT_MODEL`, model id, default `claude-sonnet-5`.
- `PORT`, server port. Railway sets it; local default `8100`.

## Deploy to Railway (not yet provisioned)

Config-as-code is committed (`railway.json`), build with `npm ci && npm run build`, start with
`npm run start`, healthcheck `/health`. The one-time provisioning is a deliberate step, not scripted:

1. Create a new service in the sysdesign Railway project, GitHub-connected to this repo.
2. Set its **root directory** to `services/agent-ts/` so the build runs against this `package.json`.
3. Point its config to this `railway.json`.
4. Set the env vars above (share the LangSmith, Anthropic, and API values with the Python agent,
   which is exactly the case Railway **shared variables** solve, tracked in the package TODO).
5. Optionally give it its own subdomain, the same way the Python agent got `chat.` (see
   `package-infra-services/railway/AGENTS.md`).

## Note on CI

The repo's CI is Python-only (uv plus ruff plus pytest), it doesn't build or test this service. Run
`npm run typecheck` and `npm test` locally before pushing. If this graduates from an experiment,
add a small node job to `.github/workflows/ci.yml`.
