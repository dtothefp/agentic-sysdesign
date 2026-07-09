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
              (each message carries the chord metadata: which callback
               to fire and how many siblings to wait for)

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

## How the worker gets the message (there is no trigger)

The API never starts, calls, or reaches into the worker. Both processes were started
independently (`make api`, `make worker`) and both import the entire codebase. A file
doesn't belong to a process; what determines where a function executes is who CALLS it,
and it runs in the caller's process. `start_run` lives in `worker/tasks.py` for
organization, but it's a plain `def` (no `@celery_app.task` on it), so when `create_run`
calls it, it runs inside the API process, same call stack. Even the `chord(...)` line runs
in the API process, and all it does is push five JSON messages onto a Redis list and
return. At that point nothing has executed.

One file, split personality:

| worker/tasks.py | executes in the API | executes in the worker |
|---|---|---|
| `start_run()` | yes (called by `create_run`) | |
| the `chord(...)` enqueue inside it | yes | |
| `scrape_influencer` body | | yes |
| `finalize_run` body | | yes |

So how does the worker find out? Not by notification, and not by pub/sub. At boot the
worker opens one plain TCP socket to Redis (the same kind of long-lived connection the
API holds to Postgres; no WebSocket needed, backend processes use raw sockets directly)
and sends BRPOP, a blocking pop. It means "give me the next item from the list named
celery, and if it's empty, hold my request open until something appears."

```
WORKER: BRPOP celery ────────▶ REDIS: list is empty. I'll hold this
        (waits on the open           request open and say nothing.
         socket, zero CPU)           │
                                     │   ...seconds pass...
                                     │
API:    LPUSH celery {msg} ────────▶ │ item arrived! complete the
                                     ▼ worker's pending BRPOP with it
WORKER: ...gets its answer, runs the task, loops back to BRPOP
```

You've seen this pattern in JS as long-polling, or as `const msg = await queue.get()`, a
promise that stays pending until someone pushes. The worker was already leaning in with
its hand out, so delivery feels instant even though nobody triggered anything. And if no
worker is running, messages just pile up in the list. Try it once: stop `make worker`,
POST a run, watch it sit queued forever, then start the worker and watch it drain the
backlog. That's the decoupling made visible.

### Why the jobs don't ride pub/sub

Pub/sub has exactly the two properties a job queue can't tolerate:

```
                        PUB/SUB (radio)          LIST + BRPOP (mailbox)
who gets the message    EVERY subscriber         exactly ONE popper
nobody's listening?     message vanishes         message waits in the list
```

Play it out with 4 worker children. If jobs went over pub/sub, all 4 would receive all 5
scrape tasks and every influencer would get scraped 4 times. And a job published while
every worker was busy would evaporate. The list gives the opposite of both, each message
popped exactly once (which doubles as free load balancing) and messages wait safely until
someone's ready. Progress updates want the opposite semantics (every open browser tab
should hear them, and missing one is fine because Postgres holds the truth), so THEY ride
pub/sub. Same server, two primitives with opposite guarantees, each matched to its job.

This is also the production scaling story. Add worker boxes, all running the identical
command against the same broker URL, and the queue load-balances across them
automatically because a message can only be popped once. In ECS terms the worker is a
second service scaled on queue depth, zero code change.

Interview: "producers and consumers never reference each other's processes. The producer
pushes a message naming a task; consumers sit in a blocking pop on the broker, so
delivery is push-latency without polling, and adding consumers scales throughput with no
code change."

## The chord, for a JS developer

First the shape, then the syntax. Fan-out splits one request into N independent parallel
tasks. Fan-in is the barrier that fires exactly once when all N are done, receiving
everyone's results:

```
                    ┌─▶ scrape nick ──┐
   start_run ───────┼─▶ scrape jane ──┼──────▶ finalize_run([r1..r5])
   (1 thing)        ├─▶ scrape sam  ──┤        (1 thing again)
      FAN-OUT       ├─▶ scrape ana  ──┤   FAN-IN (waits for ALL,
   1 to N parallel  └─▶ scrape bob ──┘    gets everyone's return values)
```

`chord` is Celery's word for that whole shape, a distributed `Promise.all`. The lines in
`start_run`:

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

### The callback is data, not code

This is where the `Promise.all` analogy genuinely breaks, and it's worth slowing down on.
In JS, the process that called `Promise.all(...).then(finalize)` keeps the pending state
and the callback function in its own memory, stays alive, and its own event loop fires the
callback. The Celery chord keeps NOTHING in the caller. Functions aren't serializable, so
`finalize_run.s(run_id)` gets turned into a description (the task's registered NAME plus
its args), and a copy of that description is stamped into each of the 5 queue messages:

```
one message sitting in the Redis list:
{
  "task": "worker.tasks.scrape_influencer",
  "args": [3, {"handle": "nick", ...}, ...],
  "chord": {                                  ← the callback, as DATA
    "task": "worker.tasks.finalize_run",
    "args": [3],
    "chord_size": 5
  }
}
```

The worker turns that name back into a function by looking it up in its task registry,
built at boot when it imported `worker/tasks.py` (that's what `include=["worker.tasks"]`
in celery_app.py is for). Ship the name, look up the code on the other side; that's the
whole trick for moving "a function call" between processes.

Once those messages are pushed, the API is done, completely. You could kill the API one
second after POST and the run would still finish. So who fires the callback? The LAST
worker to finish, running library code. Every worker, as part of finishing any task,
runs a little epilogue Celery bolts on:

```
worker child finishes scrape_influencer(...)
  │
  ├─ store my return value            ──▶ Redis /1  (celery-task-meta-<uuid>)
  ├─ this message had a "chord" field, so:
  │     append my result to the chord scoreboard
  │                                   ──▶ Redis /1  (celery-taskset-meta-<group_id>.s,
  │                                        a sorted set of the results so far)
  │
  └─ did MY append bring the scoreboard to 5?
        no  → done, go BRPOP the next task
        yes → I'm the closer. The 5 results are already sitting in the
              scoreboard; build the finalize_run message, LPUSH it  ──▶ Redis /0
              then delete the scoreboard key.
              (finalize_run then runs like any other task,
               in whichever worker pops it)
```

A tidy detail: the scoreboard and the collected results are the same structure. Each
task appends its own return value, so "the counter" is just the set's size, and the
moment it reaches 5 the callback's input list is already assembled. The callback's
task_id was stamped into every header message at enqueue time, which is part of why it
fires exactly once no matter which worker closes.

Four workers answer "no" and walk away; the fifth enqueues the callback. Nobody watches,
nobody subscribes. The check happens inline at the moment each task completes, and the
counter lives in Redis so the check is race-safe across processes. The API never learns
the run finished; the only reason the browser does is that finalize_run writes
`completed` to Postgres and publishes on the megaphone.

Interview: "a Celery chord has no coordinator process. The callback travels as data
inside each header message, completion is a counter in the result backend, and whichever
worker finishes last enqueues the callback. The caller can die immediately and the chord
completes anyway. The coordination is decentralized, so no single crash loses the run."

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

### Those `: ping` lines in the stream

Watch a run with `curl -N` and between events you'll see lines like `: ping - 2026-07-09
22:02:09`. In the SSE protocol a line starting with a colon is a comment, and clients
ignore it. sse-starlette sends one every 15 seconds as a keepalive, so the connection
never looks dead to proxies, load balancers, or idle timeouts along the way. Any
long-lived HTTP connection needs a heartbeat; this one comes free with the library.

They also make a nice decoupling proof. Freeze the worker at a debugger breakpoint for two
minutes and the pings keep arriving on schedule, no deltas (the publisher is frozen) but a
live connection (the relayer doesn't notice or care). Two processes, genuinely
independent, demonstrated in your own terminal.

## Who writes to Postgres

Both processes, deliberately. There's no rule that only one process may touch the
database; handling many concurrent clients is most of what Postgres does. The rule we DO
enforce is narrower, both processes write signals through the literal same function
(`common.signals.insert_signal`), so exactly one code path defines what a valid write is.

```
API writes (fast bookkeeping)         WORKER writes (the heavy lifting)
─────────────────────────────         ────────────────────────────────
INSERT runs row, status queued        UPDATE runs to running
(inside start_run, so POST /runs      INSERT raw_signals (the actual data,
 can return the run_id instantly)       via the same insert_signal the API uses)
                                      UPDATE runs done_count++, inserted++
regular CRUD endpoints                UPDATE runs to completed/failed
(POST /signals, /influencers)         REFRESH the rollup matview
```

The split follows one principle. Anything that must happen before the HTTP response
returns (the tiny "a run now exists" row) is the API's job; everything slow belongs to
workers.

## Redis is one big dictionary (its states through a run)

Redis is essentially one big dictionary living in RAM, where every value has a type
(string, list, hash, ...). The 16 numbered drawers are just 16 separate dictionaries.
Pub/sub is the one feature that's NOT in the dictionary, a side channel bolted onto the
same server where nothing is stored. And Redis never invents a key on its own; every key
below exists because celery library code running in one of OUR processes sent a write
command over its socket.

Four moments of a demo run with id 3:

```
T0: idle, before POST /runs
  db 0: { }                                    ← empty dict
  db 1: { }
  pub/sub: no channels, no subscribers

T1: instant after POST /runs (API pushed, workers haven't popped yet)
  db 0: {
    "celery": [ msg(scrape nick), msg(scrape jane), msg(scrape sam),
                msg(scrape ana), msg(scrape bob) ]      ← a LIST value
  }
  db 1: { }
  pub/sub: channel run:3 has 1 subscriber (your browser's SSE stream)

T2: mid-run (4 popped and executing, 1 still queued, 2 finished)
  db 0: {
    "celery": [ msg(scrape bob) ]                       ← last one still waiting
  }
  db 1: {
    "celery-taskset-meta-<group_id>.s":                 ← the chord scoreboard, a sorted
        [ {result of nick}, {result of jane} ],           set. Created by the FIRST
                                                          finishing task, appended to by
                                                          each one after. Size = the count.
                                                          Deleted once the callback fires.
    "celery-task-meta-<uuid1>": {result of nick},       ← each finished task's return
    "celery-task-meta-<uuid2>": {result of jane}          value, serialized
  }
  pub/sub: PUBLISH run:3 {"done_count": 2, ...}  ──▶ subscribers hear it, then it's gone
  (meanwhile the Postgres runs row reads status=running, done_count=2)

T3: after finalize_run
  db 0: { "celery": [] }                                ← drained
  db 1: {
    "celery-task-meta-<uuid1..5>": {...}                ← all 5 results plus finalize's,
  }                                                       auto-expire in 4h (result_expires)
  pub/sub: one last PUBLISH run:3 {"status": "completed"}, then silence.
           The browser closes the stream, subscriber count drops to 0.
  (the Postgres runs row reads completed, done_count=5. That's the durable record.)
```

Who reads those keys? The taskset scoreboard is how the workers collectively notice the
barrier is down. Whichever worker child finishes the 5th task sees its own append bring
the set to 5, takes the five results already accumulated there, and enqueues
`finalize_run(results, run_id)` as a new message on the job list, where a worker picks it
up like any other task. So the fan-in hands off worker to worker, through Redis. The API
never reads any of this; it fired and forgot at POST time, and it learns outcomes the
same way the browser does, from Postgres and the megaphone. The task-meta keys then sit
as leftovers for 4 hours in case something asks "what did task <uuid> return" (nothing in
our app does), and Redis evicts them.

Notice what's true at T3 plus a few hours. Redis is back to roughly empty. Everything in
it was scaffolding (queue entries, scoreboards, broadcasts), and the only permanent
artifacts of the run live in Postgres. That's the design in one sentence.

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

### Why it's called a "rollup"

Industry term from the OLAP/analytics world, not something this project invented. When you
aggregate fine-grained rows up a hierarchy (events into hours, hours into days, days into
months), each summary level "rolls up" the level below it. SQL even has it as a keyword,
`GROUP BY ROLLUP (...)`, which computes subtotals at every level in one query. Data teams
say "the daily rollup" the way frontend teams say "the bundle". So `daily_signal_rollup`
reads as "the table where per-signal rows get summarized per day", and here 264 raw
signals roll up into a handful of `(influencer, day, count)` rows.

### Watch the cache be wrong (the staleness experiment)

Run this in `psql` while paused in the debugger at `finalize_run`, just BEFORE stepping
over the `refresh_rollup(conn)` line, then again just AFTER:

```sql
SELECT * FROM daily_signal_rollup ORDER BY day DESC LIMIT 10;
SELECT count(*) FROM raw_signals;
```

Observed live on a real run (run 4, the 4th demo run of the day, 5 signals per influencer
per run):

```
                 raw_signals (the truth)        daily_signal_rollup (the cache)
                 ────────────────────────       ────────────────────────────────
before refresh   264 rows. run 4's 25 new       "15 per influencer today"
                 signals ALREADY in there        computed after run 3, FROZEN

     ...step over refresh_rollup(conn, concurrently=True)...

after refresh    264 rows, unchanged.           "20 per influencer today"
                 the raw table never lied        recomputed, agrees again
```

`count(*)` is identical both times because the raw table can't go stale, rows are rows.
But the matview said 15 while the truth was 20. The gap between those two queries IS
staleness, and the one line between them is what closes it. In this lab a GROUP BY over
264 rows is free; in production `raw_signals` is millions of rows and the dashboard reads
daily counts on every page load, so you flip the expensive work to write time (once per
run, a moment you control) and every read becomes an index lookup.

### Two names, two refresh paths

`common/signals.py` has `refresh_rollup`, a plain function that executes
`REFRESH MATERIALIZED VIEW CONCURRENTLY daily_signal_rollup`. `worker/tasks.py` also has
`refresh_rollup_task`, a Celery task that just calls that function. They exist for two
different callers.

```
who pushes onto the queue          what they push
─────────────────────────          ──────────────
API (POST /runs)                   5x scrape_influencer (+ chord callback as data)
the LAST worker to finish          finalize_run
celery-beat (a clock process)      refresh_rollup_task, every 5 min
```

`finalize_run` calls the plain FUNCTION directly, in-process. It's already inside a worker
with a database connection open, so queueing a task from inside a task just to run one SQL
statement would be a pointless round trip through Redis. The TASK wrapper exists solely so
celery-beat, which can only speak "enqueue a task by name on a schedule", has something to
schedule (`beat_schedule` in `worker/celery_app.py`). Beat is a separate little process
(`make worker-beat`), cron for Celery. If you set a breakpoint in `refresh_rollup_task`
and never hit it, that's why. Nobody enqueues it unless beat is running. The backstop
exists because runs aren't the only door into `raw_signals` (the scrape-signals skill
POSTs to `/signals` directly, and those inserts never pass through `finalize_run`), so
beat guarantees the dashboard is never more than ~5 minutes stale no matter which door
the data came in. And since any process that imports the celery app can be a producer,
a "refresh now" button in the API would just be `refresh_rollup_task.delay()`.

### Why CONCURRENTLY, and why autocommit

A plain `REFRESH` takes an exclusive lock while rebuilding, so every dashboard read blocks
until it finishes. `CONCURRENTLY` builds the new copy off to the side and swaps it in, and
readers never wait. Postgres only allows that if the matview has a UNIQUE index (ours is
on `(influencer_id, day)`, built in Module 1 for exactly this moment; the unique key is
how Postgres diffs old copy against new during the swap). `CONCURRENTLY` also refuses to
run inside a transaction block, which is why `finalize_run` opens its connection with
`autocommit=True`.

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

## Watermarks (how live mode stays incremental)

A watermark is a saved position marker, "I've processed everything up to HERE, next time
start from here." Same pattern as a Kafka consumer offset, a pagination cursor, or an
`If-Modified-Since` header. Each influencer row carries `last_scraped_at`; NULL means
first scrape (fetch just the newest post), otherwise the run asks Apify for
`onlyPostsNewerThan: watermark` and advances the watermark afterward. Three details
matter.

**It's set to the run's START time, not the newest post seen.** `run_ts` is stamped in
`start_run` before anything is fetched. If it were set to the scrape's end time, a post
published while the scrape was running could land behind the watermark and be missed
forever. Start-time watermarks mean the next run re-covers the minutes the scrape took,
deliberately overlapping windows.

**The overlap is safe only because of the upsert.** Re-fetching a post you already have is
a free no-op through `ON CONFLICT DO NOTHING`. That's the two-layer shape nearly every
incremental pipeline converges on:

```
watermark  = EFFICIENCY   don't re-FETCH old stuff   (saves Apify money)
upsert     = CORRECTNESS  re-fetching is harmless    (covers the watermark's edges)
```

Neither layer alone works. Watermark without upsert, any overlap creates duplicates.
Upsert without watermark, every run re-scrapes (and re-pays for) the full history.

**Failed tasks don't advance it.** The watermark UPDATE sits after the scrape call, so an
exception skips it and the failed window stays open. The next successful run picks up
everything the failed one missed, retry semantics for free, no repair step.

### A real 400, and why error bodies matter

The first live run ever attempted failed all 5 tasks with `HTTPError: HTTP Error 400: Bad
Request` and nothing else, because the original `_apify_run` discarded the HTTP response
body. Reading that body gave the actual reason in one line, Apify validates
`onlyPostsNewerThan` against a regex that accepts ISO timestamps ending in `Z` only, and
Python's `isoformat()` spells UTC as `+00:00`. The scrape-signals skill never hit this
because it reads the watermark from the API's JSON, where pydantic spells UTC as `Z`.
Same database column, two spellings, one strict regex.

Two fixes shipped. The watermark is normalized to `%Y-%m-%dT%H:%M:%SZ`, and `_apify_run`
now raises with the response body included, so the run's `error` column stores the real
reason. The lesson is bigger than the bug, error messages are stored observability, and
run 5's row (five useless copies of "400 Bad Request") is a monument to what happens when
you throw the good part away. The failure cost nothing, input-validation rejections never
start an actor run.

## Validating a run (and what failure looks like)

After a `done` event, every durable artifact the run was supposed to change can be
checked in psql:

```sql
-- 1. the run's own record: status, counts, timestamps, error
SELECT id, status, done_count, total, inserted, error, finished_at
FROM runs WHERE id = 6;

-- 2. the data landed (live rows carry source=instagram in the payload)
SELECT i.instagram_handle, s.captured_at, left(s.payload->>'caption', 70)
FROM raw_signals s JOIN influencers i ON i.id = s.influencer_id
WHERE s.payload->>'source' = 'instagram' ORDER BY s.id DESC LIMIT 10;

-- 3. the rollup is fresh: zero rows = the cache agrees with the truth everywhere
SELECT influencer_id, date_trunc('day', captured_at) AS day, count(*)
FROM raw_signals GROUP BY 1, 2
EXCEPT
SELECT influencer_id, day, signal_count FROM daily_signal_rollup;

-- 4. watermarks advanced to this run's run_ts
SELECT instagram_handle, last_scraped_at FROM influencers ORDER BY id;
```

Then the best validation of all, POST another live run immediately. Every task should
report `inserted: 0` (nothing newer than a watermark set minutes ago). A pipeline you can
run twice and get a no-op is the idempotency contract proven end to end.

**Failure shapes.** A task that scraped fine but found nothing new is NOT a failure; it
looks like `inserted: 0, error: null` in the stream and like nothing at all in the
database. A task that broke carries its message in the progress event, and `finalize_run`
writes the joined per-handle messages into `runs.error`. Status is `failed` only when ALL
tasks errored; partial failure stays `completed` with the failing handles listed in
`error`. Note `done_count` still reaches total on a fully failed run, errored tasks count,
that's the swallow-and-continue design that lets the chord fire and the run close instead
of hanging at `running` forever.

**What's deliberately NOT stored.** `raw_signals` has no `run_id` column, so "which run
inserted this row" isn't recorded, a signal is a fact about the world (rourke posted X at
time T), not about the scraping process, and dedup means two runs can both "insert" the
same post. Reconstructing a run's rows means inferring from insert order (`ORDER BY id
DESC`) or the watermark window. Relatedly, `captured_at` is the post's PUBLISH time (event
time), not when the pipeline stored it (ingestion time), so live rows land on the day they
were posted, filed into whichever partition covers that date. If run-level provenance or
ingestion time ever mattered (auditing, retry dashboards), that's a schema decision, an
`ingested_at` column or a `run_items` table. The database remembers exactly what you
design it to remember.

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

Expect each influencer to "catch" several times. Play means "run until the NEXT
breakpoint", so with breakpoints at the task top, the done_count UPDATE, and `_publish`,
one task stops three times before `inf` changes. Nothing is running twice; the stops are
geography. The tells are the yellow highlighted line (different line each stop, same
influencer) and the Call Stack panel (`_publish` on top means you're inside the megaphone
call, `scrape_influencer` on top means you're in the task body). For one stop per
influencer, uncheck all but one breakpoint in the Breakpoints panel.

## Interview soundbites

- "The POST returns a job id immediately; the work happens in workers pulled off a Redis
  queue. Never make an HTTP request wait on a scrape."
- "Fan-out is a Celery chord, one task per influencer; the chord's callback is my fan-in
  barrier, it refreshes the read model exactly once when all tasks land."
- "Progress is snapshot-then-deltas. Durable state in Postgres, live deltas over Redis
  pub/sub, SSE to the browser. A refresh re-reads the snapshot, so nothing is lost."
- "Every write is the same idempotent ON CONFLICT upsert, so at-least-once delivery and
  retries are no-ops, not duplicates."
- "Redis plays three roles, a list as the task queue (competing consumers, so it
  load-balances), keys as the chord scoreboard, and pub/sub for live broadcast. Queue
  messages wait for exactly one popper; pub/sub reaches every current subscriber and
  stores nothing."
- "Redis is disposable here by design. Lose it and I lose in-flight queue messages and the
  live animation, never the data. Postgres is the system of record."
- "Dashboard aggregates come from a materialized view refreshed at write time. The fan-in
  callback refreshes it the moment a run completes, a periodic beat task is the staleness
  backstop, and REFRESH CONCURRENTLY over a unique index means readers never block during
  the rebuild."
- "Each source keeps a watermark, the run START time of the last successful scrape. Only
  advanced on success, so failures self-heal on the next run, and set to the start rather
  than the end so nothing published mid-scrape falls in a gap. Any overlap dedupes through
  the idempotent upsert."
- "The raw table keys on event time, not ingestion time. If we needed run-level provenance
  we'd add an ingested_at or run_id column, but dedup means a row can belong to two runs,
  so 'which run inserted this' is genuinely ambiguous by design."
