# Chat gateway (system design drill)

A direct-messaging service built as interview practice for the "design a messaging app"
question. Real-world, multi-component, built progressively. Not LeetCode.

## The problem being solved

Two users message each other. Delivery is **real-time** when the recipient is online and
**async** (persisted, delivered later) when they're offline. Reuse the monorepo's existing
Supabase (Postgres) and, later, Redis + Celery. No frontend; test from the terminal.

## Where we are: step 2 (Redis pub/sub) is built

| Phase | What | Status |
|---|---|---|
| Schema | 4 tables, idempotency + scroll-back indexes | DONE (migration applied) |
| Step 1 | Single instance, in-memory connection registry, WebSocket + REST | DONE, 7/7 e2e checks pass |
| Step 2 | Redis pub/sub so multiple instances deliver across the split brain | DONE (verify cross-instance below) |
| Async | Celery job to wake offline recipients (push/email), the Mode A case | NEXT |
| Auth | Supabase JWT verification; `msg_users.external_id` holds the auth `sub` | punted to last |

Step 1 landed in dtothefp/to-the-moon#51; the Railway service definition in #52. Step 2 is on
branch `chat-redis-pubsub`.

Deep dives:
[docs/messaging-step-1-single-instance.md](../../docs/messaging-step-1-single-instance.md)
(schema, single-instance design, the split-brain demo) and
[docs/messaging-step-2-redis-pubsub.md](../../docs/messaging-step-2-redis-pubsub.md)
(pub/sub vs drawers, the envelope vs conns split, the delivery trace, the scaling spectrum).

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
- **`conns: dict[int, WebSocket]`** is the routing table, user_id to live socket. Step 1's whole
  lesson was its limitation: process-local, so it does not survive going multi-instance. Step 2
  keeps it as the LAST HOP only; fan-out now goes through Redis (below).
- **Redis pub/sub bus (step 2)** is the fix for that limitation. `persist_and_fanout()` publishes
  each message to one channel (`chat:fanout`); every instance runs one background subscriber
  (`_subscriber`) that delivers to whichever recipient sockets it holds. So a message reaches its
  recipient regardless of which instance their socket is on. Same-instance delivery rides the same
  path (publish, loopback, subscribe, push), so there is exactly one delivery path. Typing rides
  the bus too, so it also crosses instances.
- **`/ws?user_id=N`** is one duplex socket per online user. Down: messages pushed instantly.
  Up: `send` and `typing` frames. Typing is high-frequency, ephemeral, never persisted, and
  is the concrete reason this is WebSockets and not SSE (SSE is one-way; the upstream typing
  traffic would need a whole second channel).
- **`persist_and_fanout()`** is the single delivery path both send routes funnel through:
  idempotent INSERT, look up other participants, then publish to the bus. Offline recipients are
  simply not delivered to by any instance's subscriber (message is already durable); waking them is
  the future Celery job.
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

# cross-instance delivery (step 2: the split brain, now FIXED)
moon run chat:dev                                   # :8100 (instance A)
moon run chat:dev-b                                 # :8101 (instance B)
websocat "ws://localhost:8101/ws?user_id=1"         # user 1 (recipient) on B
curl -X POST "http://localhost:8100/conversations/1/messages?sender_id=2" \
  -H 'Content-Type: application/json' \
  -d '{"conversation_id":1,"body":"crosses now","client_msg_id":"cross-2"}'   # user 2 sends to A
# the message NOW arrives on B. A published to Redis, B's subscriber delivered to its own socket.
# Before step 2, nothing arrived (A's conns never held user 1). That is the fix, demonstrated.
```

Redis is the sibling compose service (`redis:6379`); `REDIS_URL` is already in the container env
and in the debug configs. `redis-cli -h redis SUBSCRIBE chat:fanout` in a terminal shows the raw
envelopes flowing while you send.

Debugging: `.vscode/launch.json` has "Chat A (:8100)", "Chat B (:8101)", and the
"Chat: split-brain (A + B)" compound. Good breakpoints for step 2: `_publish` (A publishes),
`_subscriber` / `_deliver_local` (B receives and pushes to its `conns`). No `--reload` in the
debug configs so breakpoints bind.

## Next step (async / Mode A)

The bus fixes delivery to recipients who are online SOMEWHERE. The remaining case is a recipient
online NOWHERE (Mode A): the message is already durable, but nothing wakes them. That is a Celery
job (push notification / email), the same broker the worker already uses, not a chat hot-path
concern. After that: a real "create conversation" endpoint (find-or-create DM) and Supabase JWT
auth.

## Interview framing

Justin (Truck Smarter technical screen) assesses "engineering judgment and structured
problem-solving": multi-component, AI tools and own IDE allowed. The reps here: decompose a
fuzzy problem, name tradeoffs out loud (WS vs SSE, idempotency, single-then-multi-instance,
when Celery earns its place), build progressively, extend on request.
