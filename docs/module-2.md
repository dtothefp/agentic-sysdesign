# Module 2: Celery fan-out, Redis, and SSE, from first principles

Module 1 was one process talking to one database. Module 2 adds the thing almost every
system-design interview eventually asks for, background work. A scrape of 5 influencers
shouldn't happen inside an HTTP request (it's slow, it can fail halfway, the browser would
sit on a spinner for minutes). So we split it into pieces that run in parallel, in a
separate process, while the browser watches live progress over a stream.

This doc walks the whole thing assuming none of it is familiar. Read it top to bottom once,
then keep it open next to the code.

## The four players

```
┌─────────────┐     ┌─────────────┐     ┌─────────────┐     ┌─────────────┐
│   BROWSER   │     │  API        │     │   REDIS     │     │  POSTGRES   │
│  (or curl)  │     │  (FastAPI)  │     │             │     │             │
│             │     │  make api   │     │ the queue + │     │ the durable │
│ asks for    │     │             │     │ the megaphone│    │ truth       │
│ things,     │     │ front door: │     │             │     │             │
│ watches the │     │ fast answers│     │ nothing that│     │ runs,       │
│ stream      │     │ only, never │     │ matters is  │     │ raw_signals,│
│             │     │ does slow   │     │ ONLY here   │     │ the rollup  │
└─────────────┘     │ work itself │     └─────────────┘     └─────────────┘
                    └─────────────┘     ┌─────────────┐
                                        │ CELERY      │
                                        │ WORKER      │
                                        │ make worker │
                                        │             │
                                        │ the muscle: │
                                        │ pulls jobs, │
                                        │ does slow   │
                                        │ work        │
                                        └─────────────┘
```

The API and the worker are two separate OS processes running the same codebase. The API
imports `worker/tasks.py` only to *enqueue* work; the worker is the one that *executes* it.
They never call each other directly. Everything between them goes through Redis or Postgres.

Anchor to your world: the API is a stateless container behind a load balancer, the worker is
a separate ECS service scaled independently, Redis is the queue between them. This local
setup is that architecture in miniature.

## Redis wears two hats

This is the single most confusing part, so it gets its own section. Redis is doing two
unrelated jobs in this app, and one of them we never touch.

A Redis server contains 16 numbered mini-databases, like one filing cabinet with 16 drawers.
The number after the slash in a URL picks the drawer (`redis://redis:6379/0` is drawer 0).
Same key can exist in two drawers without colliding. That's all the number means.

```
                    ONE REDIS SERVER
   ┌─────────────────────────────────────────────────┐
   │                                                 │
   │  HAT 1: Celery's private plumbing               │
   │  ┌───────────────────────────────────────────┐  │
   │  │ drawer /0  the BROKER (task queue)        │  │   Celery-only.
   │  │   ["scrape nick", "scrape jane", ...]     │  │   We configure the URLs
   │  │                                           │  │   and never read or write
   │  │ drawer /1  the RESULT BACKEND             │  │   these keys ourselves.
   │  │   {task return values, chord counter}     │  │
   │  └───────────────────────────────────────────┘  │
   │                                                 │
   │  HAT 2: our progress megaphone                  │
   │  ┌───────────────────────────────────────────┐  │   Our code.
   │  │ pub/sub channel "run:3"                   │  │   Worker PUBLISHes,
   │  │ (channels ignore drawers entirely,        │  │   the API's SSE endpoint
   │  │  a PUBLISH reaches every subscriber        │  │   SUBSCRIBEs.
   │  │  on the server)                           │  │
   │  └───────────────────────────────────────────┘  │
   └─────────────────────────────────────────────────┘
```

**The broker (drawer /0)** is a queue. The API pushes task messages on, workers pull them
off. A message survives until someone consumes it.

**The result backend (drawer /1)** is where a finished task's return value goes. Two
different processes can't read each other's memory, so when `scrape_influencer` returns a
dict inside the worker, Celery serializes it and stores it here so anyone (including
Celery's own chord machinery) can fetch it. It's also the scoreboard the chord uses to
count "4 of 5 done".

**Pub/sub** is neither of those. It's a megaphone. `PUBLISH run:3 {...}` delivers the
message to whoever is subscribed at that exact instant and then it's gone forever. Nothing
is stored, late subscribers hear nothing. That sounds like a flaw, and it would be if the
progress messages were the only record. They aren't, which is the whole design (see the SSE
section).

## Life of a run, step by step

You POST `/runs` and get back `{"run_id": 3, "total": 5}`. Here's everything that happens.

### Step 1: the trigger (API process, milliseconds)

```
POST /runs {"mode":"demo","limit":5}
   │
   ▼
create_run() calls start_run()          [worker/tasks.py]
   │
   ├── INSERT INTO runs (status='queued', total=5)  ──▶  POSTGRES
   │      the job now EXISTS durably. id=3 is the run_id.
   │
   └── chord( [5 task signatures] )( finalize_run )
          │
          └── 5 messages pushed onto the broker  ──▶  REDIS /0
              + chord registered in the result backend  ──▶  REDIS /1

   ◀── returns {"run_id": 3, "total": 5, "mode": "demo"}   (HTTP done)
```

Nothing has scraped. The HTTP request is already over. The work is sitting in a queue.

### Step 2: the fan-out (worker process)

The worker (`make worker`) is subscribed to the broker and pulls tasks, up to 4 at once
(`--concurrency 4`). Each `scrape_influencer(run_id, inf, ...)` does the same dance:

```
pull task off queue
   │
   ├── UPDATE runs SET status='running', started_at=now()   ──▶ POSTGRES
   │     (only the FIRST task actually changes anything,
   │      the SQL is written so the race doesn't matter)
   │
   ├── do the actual work: insert this influencer's signals ──▶ POSTGRES
   │     via common.signals.insert_signal, the SAME idempotent
   │     ON CONFLICT upsert the API's POST /signals uses
   │
   ├── UPDATE runs SET done_count = done_count + 1           ──▶ POSTGRES
   │
   ├── PUBLISH run:3 {"type":"progress","done":2,"total":5}  ──▶ REDIS (megaphone)
   │
   └── return {"handle":"nick","inserted":5}                 ──▶ REDIS /1 (result backend)
```

Note the order. Postgres first, megaphone second. The durable record is already correct
before anyone hears the announcement.

### Step 3: the fan-in (worker process, exactly once)

Celery watches the chord's counter in the result backend. The moment the 5th task returns,
it collects all 5 return values into a list and calls the callback:

```
finalize_run(results=[{...},{...},{...},{...},{...}], run_id=3)
   │
   ├── REFRESH MATERIALIZED VIEW CONCURRENTLY daily_signal_rollup  ──▶ POSTGRES
   │     the dashboard's precomputed counts now include this run
   │
   ├── UPDATE runs SET status='completed', finished_at=now()       ──▶ POSTGRES
   │
   └── PUBLISH run:3 {"type":"done","status":"completed"}          ──▶ REDIS (megaphone)
```

The refresh runs here, once per run, instead of inside each task (which would run it 5
times) or on every dashboard read (which would defeat the point of a matview).

## The chord, for a JS developer

`chord` is Celery's distributed `Promise.all`. The three lines in `start_run`:

```python
header = [scrape_influencer.s(run_id, inf, run_ts, mode, limit) for inf in payload_infs]
chord(header)(finalize_run.s(run_id))
```

translate almost mechanically to

```js
const header = influencers.map(inf => scrapeInfluencer.bind(null, runId, inf, runTs, mode, limit));
Promise.all(header.map(fn => fn())).then(results => finalizeRun(results, runId));
```

Two bits of Celery syntax to decode:

- **`.s(args)`** builds a "signature", a task bundled with its arguments but not yet
  running. It's `fn.bind(null, args)`. Needed because the function will execute later in a
  different process, so "which function + which args" has to be serialized into a queue
  message rather than called.
- **`chord(header)(callback)`** is two calls. `chord(header)` builds the coordination
  object; invoking it with `(callback)` launches everything.

What makes it more than `Promise.all`: in JS, one process holds the "4 of 5 resolved" state
in memory. Here the 5 tasks run in different worker processes at unpredictable times, so no
single process can hold that counter. Celery keeps it in the result backend (Redis /1);
each finishing task increments it, and whoever brings it to 5 triggers the callback.

## SSE and why a page refresh loses nothing

Server-Sent Events is the simple half of WebSockets. The browser opens a plain GET and the
server just never stops writing. Each `data: {...}\n\n` chunk pops out as an event on an
`EventSource` object in the browser. One direction only (server to client), auto-reconnect
built in, plain HTTP.

The trap in any live-progress design is state that lives only in the stream. Pub/sub
messages evaporate on delivery, so a client that connects late (or refreshes, killing its
connection) has missed messages forever. The fix is the snapshot-then-deltas pattern in
`stream_run`:

```
GET /runs/3/stream
   │
   1. SUBSCRIBE to run:3 FIRST           nothing published after this instant
   │                                     can be missed
   2. SELECT * FROM runs WHERE id=3      the durable truth, however late we are
   │
   ├──▶ send event: snapshot {"done_count":2,"total":5,...}
   │
   3. already finished? send done, close.
   │
   4. forward each megaphone message as it arrives
   │
   ├──▶ event: progress {"done":3,...}
   ├──▶ event: progress {"done":4,...}
   └──▶ event: done     {"status":"completed"}      close.
```

Subscribe-then-snapshot, in that order, closes the race. If we snapshotted first and
subscribed second, a delta published in the gap between the two would be lost. The other
way around, the worst case is hearing a delta that the snapshot already includes, which is
harmless (the numbers just repeat).

A refresh now costs nothing. The new connection re-reads the snapshot (Postgres never
forgot) and rides the deltas from there. There's no per-user registration anywhere; the
run_id in the URL is the entire subscription.

## Python syntax survival kit (for the JS developer)

The worker code is deliberately synchronous and the API streaming code is async. Knowing
which is which, and why, is half of reading this codebase.

| Python | JS equivalent | Notes |
|---|---|---|
| `async def f():` | `async function f()` | same |
| `await x` | `await x` | pause me until x finishes, value flows IN |
| `yield x` | `yield x` | hand x OUT to my consumer, sleep until they want more |
| `async def` with `yield` | `async function*` | async generator. Python has no `*` marker, the `yield` in the body is the only clue |
| `async for m in it:` | `for await (const m of it)` | consume an async iterable |
| `task.s(a, b)` | `fn.bind(null, a, b)` | Celery signature, "function + args, call later" |
| `with psycopg.connect() as conn:` | roughly `try/finally` with `conn.close()` | context manager, auto-cleanup |
| `conn.execute(...)` (no await) | synchronous, blocking call | fine in the worker (nothing else to serve), forbidden on the API's event loop |

Why the split. The API process serves many requests on one event loop, so anything slow
must be `await`ed (or shoved to a thread, see `asyncio.to_thread(_read_run, ...)`). The
worker process serves exactly one task at a time per slot, so plain blocking code is
correct there, and simpler. `EventSourceResponse(gen())` is FastAPI's version of returning
a `Response` wrapping a `ReadableStream` in a Next.js route handler, with the async
generator as the stream source.

## Matview vs partitions, one more time

Both live on `raw_signals`' read path but they're different tools.

```
raw_signals (partitioned)                daily_signal_rollup (materialized view)
┌─────────┬─────────┬─────────┐
│ 2026-05 │ 2026-06 │ 2026-07 │          influencer  day     signal_count
│ drawer  │ drawer  │ drawer  │  GROUP   nick        07-09   25
│ 1000s   │ 1000s   │ 1000s   │───BY───▶ jane        07-09   25
│ of rows │ of rows │ of rows │  (saved)             (5 rows, not 1000s)
└─────────┴─────────┴─────────┘
```

Partitions are how the raw rows are FILED. Every row exists exactly once; a time-windowed
query just opens fewer drawers (partition pruning). Partitions can't go stale because they
ARE the table.

The matview is a PRECOMPUTED ANSWER, a separate small table storing the result of the
GROUP BY. Cheap to read, but it's a snapshot, stale the moment new signals land. That's
why `finalize_run` refreshes it after every run, with the Celery-beat task
(`refresh_rollup_task`, every 5 minutes) as a backstop for signals that arrive outside a
run.

## Running it

Three dev-container terminals, all from `backend/`:

```bash
make api       # terminal A: the front door, :8000
make worker    # terminal B: the muscle
make worker-beat   # terminal C, optional: the 5-minute refresh backstop
```

Then trigger and watch:

```bash
curl -sX POST localhost:8000/runs -H 'Content-Type: application/json' -d '{"mode":"demo","limit":5}'
curl -N localhost:8000/runs/<run_id>/stream
```

`demo` mode inserts synthetic signals with a 0.4s delay each (watch the bar move, zero
Apify spend). `live` mode does the real Apify scrape off each influencer's watermark.

## Debugging it (stepping through the flow)

`.vscode/launch.json` has three entries for Cursor's Run and Debug panel, made for exactly
this kind of "walk the code to understand it" session. Start the compound **Debug API +
Worker**, set breakpoints, then curl a run.

Two configs differ from the Make targets on purpose:

- **API runs without `--reload`.** The uvicorn reloader spawns a child process; your
  breakpoints bind to the parent and never fire. Restart the debug session after edits.
- **Worker runs with `--pool solo`.** Celery's default pool forks child processes the
  debugger can't follow. Solo runs every task in the worker's main thread, one at a time,
  so breakpoints inside tasks actually hit.

A good first walk, in breakpoint order:

1. `api/main.py` `create_run`, see the request arrive.
2. `worker/tasks.py` `start_run`, step over the INSERT and the chord launch.
3. `worker/tasks.py` `scrape_influencer`, watch one influencer's inserts and the PUBLISH.
4. `worker/tasks.py` `finalize_run`, the barrier, inspect the `results` list Celery hands it.
5. `api/main.py` `gen()` inside `stream_run` (attach a `curl -N` first), watch the
   snapshot yield, then each delta arrive.

## Interview soundbites

- "The POST returns a job id immediately; the work happens in workers pulled off a Redis
  queue. Never make an HTTP request wait on a scrape."
- "Fan-out is a Celery chord, one task per influencer; the chord's callback is my fan-in
  barrier, it refreshes the read model exactly once when all tasks land."
- "Progress is snapshot-then-deltas. Durable state in Postgres, live deltas over Redis
  pub/sub, SSE to the browser. A refresh re-reads the snapshot, so nothing is lost."
- "Every write is the same idempotent ON CONFLICT upsert, so at-least-once delivery and
  retries are no-ops, not duplicates."
- "Redis is disposable here by design. Lose it and I lose in-flight queue messages and the
  live animation, never the data. Postgres is the system of record."
