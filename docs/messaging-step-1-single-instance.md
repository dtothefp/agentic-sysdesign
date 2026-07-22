# Messaging, Step 1: one instance, an in-memory routing table, and the bug that motivates Redis

The prompt is the one that came up at both Juniper Square and Slack: design a direct-messaging
app. Two users, real-time delivery when both are online, durable delivery when one is not. The
constraint we set ourselves: reuse the monorepo's Supabase (Postgres) and, later, the Redis and
Celery the worker already runs. No frontend. Drive everything from the terminal so the moving
parts stay visible.

This doc is step 1: a single process. It works, it is honest about what it cannot do, and the
thing it cannot do is left switched on so you can watch it fail. That failure is the entire
argument for step 2 (Redis pub/sub). Read this top to bottom once, then keep it next to the code
in `services/chat/chat/main.py`.

## The four tables

The schema lives in `packages/core/db/migrations/20260721000001_messaging_schema.sql`. Four
tables, all `msg_` prefixed so they coexist with the scraper schema in the same database.

```
  msg_users                 msg_conversations
  ─────────                 ─────────────────
  id           <───┐        id          <───┐
  external_id      │        created_at      │
  display_name     │                        │
  created_at       │                        │
                   │                        │
        ┌──────────┴────────────────────────┴──────────┐
        │            msg_participants                   │
        │  (the JOIN table: who is in which thread)     │
        │  conversation_id  ─────────────────────────>  │
        │  user_id          ─────────────────────────>  │
        │  last_read_message_id                         │
        │  PRIMARY KEY (conversation_id, user_id)       │
        └───────────────────────────────────────────────┘

        ┌───────────────────────────────────────────────┐
        │            msg_messages                        │
        │  (the EVENT table: one row per message sent)   │
        │  id  (BIGSERIAL, the sequence + the cursor)    │
        │  conversation_id  ─────────────────────────>   │
        │  sender_id        ─────────────────────────>   │
        │  body                                          │
        │  client_msg_id                                 │
        │  created_at                                    │
        │  UNIQUE (sender_id, client_msg_id)  <- idem    │
        │  INDEX (conversation_id, id DESC)   <- scroll  │
        └───────────────────────────────────────────────┘
```

Two of these tables look similar and confused me at first, so here is the rule that separates
them. Both `msg_participants` and `msg_messages` point at their parents through foreign keys, so
both are "many rows to one parent." The difference is whether the same pair of ids is allowed to
appear together more than once.

- `msg_participants` is a **join table**. Its whole job is to record membership: user 1 is in
  conversation 1. That fact is true once. So the pair `(conversation_id, user_id)` is the primary
  key, and the same pair can never repeat. If you try to add user 1 to conversation 1 twice, the
  database refuses. Membership is a set, not a log.
- `msg_messages` is an **event table**. The same `(conversation_id, sender_id)` pair repeats every
  time that person sends another message, which is exactly what you want. So it gets its own
  `id`, and the pair carries no uniqueness at all.

`msg_conversations` is almost empty on purpose. A row there is just an identity, a coat-check
ticket. A one-to-one DM and a fifty-person group are the same shape: one conversation row, N
participant rows. Nothing about "this is a DM" is special-cased, which is why the fan-out code
later scales to groups for free.

### The two indexes carry the design

A message table with no indexes still works; these two are where the interview points live.

- `UNIQUE (sender_id, client_msg_id)` is the **idempotency key**. The client mints a UUID once
  per message and reuses it on every retry. The insert is `ON CONFLICT DO NOTHING RETURNING`, so
  a retry collides on this index, inserts nothing, and the API hands back the original row. This
  is where "reliable delivery" is enforced rather than merely claimed. It closed the exact gap
  Garett flagged in the Juniper Square debrief: I had asserted retries were safe without showing
  the mechanism. Scoping the key to `sender_id` (not globally) means one client cannot burn
  another client's key.
- `(conversation_id, id DESC)` is the **scroll-back cursor**. History pages on the `id`
  sequence, not on `created_at`. Timestamps collide under concurrency and drift between machines
  with unsynced clocks; a `BIGSERIAL` is monotone and unique by construction. Paging with
  `WHERE id < :cursor` also means new messages arriving mid-scroll never shift the window, which
  a numeric `OFFSET` cannot promise.

`last_read_message_id` on the participant row is a small trick: it gives you unread counts and
read receipts (count messages with `id` greater than the stored value) without a separate
receipts table.

## The single-instance architecture

```
  websocat (user 2)                                   websocat (user 1)
        │                                                    │
        │ ws://.../ws?user_id=2                              │ ws://.../ws?user_id=1
        ▼                                                    ▼
  ┌──────────────────────────── ONE process ─────────────────────────────┐
  │                                                                       │
  │   conns = { 2: <socket>, 1: <socket> }   <- the routing table         │
  │                                                                       │
  │   receive loop per socket:                                            │
  │     action "send"   -> persist_and_fanout()  -> push to conns[peer]   │
  │     action "typing" -> relay to conns[peer]  (never persisted)        │
  │                                                                       │
  └───────────────────────────────┬───────────────────────────────────────┘
                                   │  await
                                   ▼
                            ┌─────────────┐
                            │  POSTGRES   │  msg_messages (durable), msg_participants
                            └─────────────┘
```

The one idea in step 1 is the `conns` dict: `dict[int, WebSocket]`, user id to their live socket.
To deliver a message to user 2, you look up `conns[2]` and write down it. That is the entire
routing table. A user absent from the dict is offline from this process's point of view, so there
is nothing to push to; the message is already saved in Postgres and they will read it from
history when they reconnect.

Two honest simplifications, each a one-line change later:

- **One socket per user.** Production is `dict[int, set[WebSocket]]` so a phone and a laptop both
  receive. Here a second login for the same user replaces the first.
- **Process-local.** This is the line that does not survive going multi-instance. It is the whole
  reason step 2 exists.

### Why a WebSocket and not SSE

A WebSocket is one connection carrying both directions. Server to client pushes a delivered
message the instant it lands. Client to server carries two kinds of upstream frame: `send` (a new
message) and `typing`. Typing is the tell. It is high frequency, fire-and-forget, and worthless
three seconds later, so it never touches Postgres, it is just relayed to the other online
participant. That constant upstream chatter is what SSE cannot carry on one connection (SSE is
server-to-client only), and it is the concrete reason the duplex socket earns its keep here.

### The one delivery path

Both send routes (a `send` frame over the socket, and a plain REST `POST` so you can curl a
message without a WebSocket client) funnel through `persist_and_fanout()`. Writing "persist, then
push to whoever is online" exactly once is the point:

1. Idempotent insert into `msg_messages` (`ON CONFLICT DO NOTHING RETURNING`); on conflict, select
   and return the original row so a retry still succeeds with the same message id.
2. `SELECT user_id FROM msg_participants WHERE conversation_id = ... AND user_id != sender`. These
   are the fan-out targets. A DM returns one, a group returns N, same query.
3. For each target with a live socket **in this process's `conns`**, push now. Targets not in
   `conns` are simply skipped: their message is already durable. Waking a genuinely offline user
   (push notification, email) is a later Celery job, not this hot path.

The service is `async` end to end (`AsyncConnectionPool`, every handler `async def`) because a
WebSocket handler owns its connection for minutes; one blocking database call would stall the
event loop and every other socket on the process with it.

### ack is not delivery

Worth nailing because it is a common interview trip-wire. When you send over the socket you get
back `{"type": "ack", ...}`. That ack means **the server persisted the message**. It does not mean
the recipient's socket received it, and it does not mean a human read it. Three different events:
server-persisted (ack), socket-delivered (a delivery receipt), human-read
(`last_read_message_id`). Step 1 only implements the first.

## The bug, demonstrated

Everything above works on one process. Here is what happens the moment there are two, which is
what any real deployment behind a load balancer looks like.

Run instance A on `:8100` and instance B on `:8101`, same code, same database. Connect the
recipient (user 1) to B, and the sender (user 2) to A. Send from user 2.

```
  user 2  ──send──>  instance A (:8100)          instance B (:8101)  <──ws── user 1
                     conns = { 2: <socket> }      conns = { 1: <socket> }
                          │
                          │ persist_and_fanout()
                          ├─ INSERT ok, row id 3          (durable in Postgres)
                          ├─ recipients = [1]             (DB says: deliver to user 1)
                          └─ conns.get(1) -> None         (user 1's socket lives in B)
                                   │
                                   └─ push skipped.  user 1 hears nothing.
```

We stepped this in the debugger and captured every piece of it:

- On instance A, `conns` was `{2: <WebSocket>}`, length 1. User 1 is simply not in this process's
  registry.
- `recipients` was `[1]`. The database correctly knows the message is for user 1. The routing
  data is right; the routing table is incomplete.
- So `conns.get(1)` returns `None`, the `if ws is not None` guard skips the push, and the sender
  still gets `ack` id 3.
- `psql` confirmed row 3 (`sender 2`, body "cross-instance, should fail") is persisted.

Persisted but not delivered live, because the recipient is online on a box this instance cannot
see. That is a split brain: two processes, two private routing tables, no shared knowledge of who
is connected where. Nothing is lost (Postgres has it, user 1 gets it on reconnect from history),
but the real-time promise is broken for exactly the case real-time matters.

Note this is a different failure from a genuinely offline recipient. If user 1 is connected
nowhere, skipping the push is correct behavior, and waking them is a Celery/push-notification
job. The split brain is specifically "online, but on another instance," and it is the one Redis
fixes.

## What step 2 adds

Redis pub/sub gives the instances a shared nervous system. On send, the receiving instance
publishes the message to a Redis channel. Every instance subscribes. The instance that happens to
hold the recipient's socket sees the publish and does the local push. No instance needs to know
which box a user is on; it just listens and delivers to its own `conns` when a relevant message
comes by. `REDIS_URL` is already in the container and the Railway environment. That is the next
branch.

## Run it yourself

Inside the dev container (`DATABASE_URL` and `REDIS_URL` are already in the environment, the
Postgres host is `db`):

```bash
# apply the schema
moon run core:migrate

# single instance: real-time works
moon run chat:dev                                    # :8100
websocat "ws://localhost:8100/ws?user_id=2"          # terminal 2: user 2 listens
curl -X POST "http://localhost:8100/conversations/1/messages?sender_id=1" \
  -H 'Content-Type: application/json' \
  -d '{"conversation_id":1,"body":"hey","client_msg_id":"abc-1"}'
# the message appears in terminal 2. Re-send the same client_msg_id: same id back, one DB row.

# two instances: the split brain
moon run chat:dev                                    # :8100 (instance A)
moon run chat:dev-b                                  # :8101 (instance B)
websocat "ws://localhost:8101/ws?user_id=1"          # user 1 (recipient) on B
curl -X POST "http://localhost:8100/conversations/1/messages?sender_id=2" \
  -H 'Content-Type: application/json' \
  -d '{"conversation_id":1,"body":"cross-instance, should fail","client_msg_id":"cross-1"}'
# sender gets a normal response, the row is in Postgres, and user 1 on B hears nothing.
```

For breakpoints, `.vscode/launch.json` has "Chat A (:8100)", "Chat B (:8101)", and the
"Chat: split-brain (A + B)" compound. No `--reload` in the debug configs so breakpoints bind.
