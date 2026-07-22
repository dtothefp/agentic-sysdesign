# Chat gateway (system design drill)

A direct-messaging service built as interview practice for the "design a messaging app"
question. Real-world, multi-component, built progressively. Not LeetCode.

## The problem being solved

Two users message each other. Delivery is **real-time** when the recipient is online and
**async** (persisted, delivered later) when they're offline. Reuse the monorepo's existing
Supabase (Postgres) and, later, Redis + Celery. No frontend; test from the terminal.

## Where we are: Phase 3a is DONE and verified

| Phase | What | Status |
|---|---|---|
| Schema | 4 tables, idempotency + scroll-back indexes | DONE (migration applied) |
| 3a | Single instance, in-memory connection registry, WebSocket + REST | DONE, 7/7 e2e checks pass |
| 3b | (optional) split out the connection registry cleanly | not started |
| 3c | Redis pub/sub so multiple instances can see each other | NEXT |
| Async | Celery job to wake offline recipients (push/email) | after 3c |
| Auth | Supabase JWT verification; `msg_users.external_id` holds the auth `sub` | punted to last |

Branch: `messaging-app-drill`. PR: dtothefp/to-the-moon#51.

## The schema (packages/core/db/migrations/20260721000001_messaging_schema.sql)

Four tables, `msg_` prefixed to coexist with the scraper schema in the same DB:

- `msg_users`: identity.
- `msg_conversations`: a thread's identity. A DM and a group are both just conversations.
- `msg_participants`: join table (users to conversations, many-to-many). PK is
  `(conversation_id, user_id)`, so each membership exists once. `last_read_message_id`
  gives unread counts with no receipts table.
- `msg_messages`: the messages. Two indexes carry the design.
  - `UNIQUE (sender_id, client_msg_id)` is the **idempotency** key. Client mints the id once
    and reuses it on retries; the duplicate INSERT conflicts and the API returns the original
    row. This is where "reliable delivery" is enforced, not just claimed.
  - `(conversation_id, id DESC)` is the **cursor scroll-back** index, keyed on the sequence
    not `created_at` (which collides under concurrency and drifts across instances).

Seeds two users (id 1 "David", id 2 "Test Contact") and one conversation (id 1) with both
in it, so the API runs on an `X-User-Id`-style query param before auth exists.

## The service (chat/main.py)

- **Separate service** from `services/api` on purpose: it scales on open-connection count,
  not CPU per job, so a redeploy of one must not drop the other's live sockets.
- **`conns: dict[int, WebSocket]`** is the routing table, user_id to live socket. This is the
  whole 3a lesson and its limitation: it's process-local, so it does not survive going
  multi-instance. That gap is what 3c fixes.
- **`/ws?user_id=N`** is one duplex socket per online user. Down: messages pushed instantly.
  Up: `send` and `typing` frames. Typing is high-frequency, ephemeral, never persisted, and
  is the concrete reason this is WebSockets and not SSE (SSE is one-way; the upstream typing
  traffic would need a whole second channel).
- **`persist_and_fanout()`** is the single delivery path both send routes funnel through:
  idempotent INSERT, look up other participants, push to whoever's online. Offline recipients
  are simply not pushed to (message is already durable); waking them is the future Celery job.
- **REST**: `POST /conversations/{id}/messages?sender_id=N` (curl-testable send) and
  `GET /conversations/{id}/messages?before=<cursor>&limit=N` (cursor pagination).
- Async end to end (`AsyncConnectionPool`) because a WS handler owns its connection for
  minutes and must never block the event loop.

## Run it (inside the dev container: DATABASE_URL/REDIS_URL already in env, host is localhost)

```bash
# single instance
moon run chat:dev                                   # :8100
websocat "ws://localhost:8100/ws?user_id=2"         # terminal 2: user 2 listens
curl -X POST "http://localhost:8100/conversations/1/messages?sender_id=1" \
  -H 'Content-Type: application/json' \
  -d '{"conversation_id":1,"body":"hey","client_msg_id":"abc-1"}'   # terminal 3: user 1 sends
# message appears in terminal 2. Re-send same client_msg_id, get same id back, one DB row.

# split-brain demo (motivates 3c)
moon run chat:dev                                   # :8100 (instance A)
moon run chat:dev-b                                 # :8101 (instance B)
websocat "ws://localhost:8101/ws?user_id=2"         # user 2 on B
curl ... :8100 ...                                  # user 1 sends to A
# nothing arrives on B. A's conns dict doesn't hold user 2's socket. That is the bug.
```

Debugging: `.vscode/launch.json` has "Chat A (:8100)", "Chat B (:8101)", and the
"Chat: split-brain (A + B)" compound (breakpoints in persist_and_fanout show A's `conns`
missing user 2). No `--reload` in the debug configs so breakpoints bind.

## Next step (3c)

Add Redis pub/sub so instances see each other. On send: A publishes the message to a channel;
every instance subscribes; the instance holding the recipient's socket pushes it. `REDIS_URL`
is already in the container env. This closes the split-brain, then the Celery path handles the
offline case.

## Interview framing

Justin (Truck Smarter technical screen) assesses "engineering judgment and structured
problem-solving": multi-component, AI tools and own IDE allowed. The reps here: decompose a
fuzzy problem, name tradeoffs out loud (WS vs SSE, idempotency, single-then-multi-instance,
when Celery earns its place), build progressively, extend on request.
