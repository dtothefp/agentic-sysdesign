# Messaging, Step 2: Redis pub/sub, and how one instance's send reaches another instance's socket

Step 1 ended on a working single process and a deliberately unfixed bug. Run two instances behind
a load balancer, put the sender on one and the recipient on the other, and the message persisted
but never arrived live. The recipient's socket lived in a `conns` dict inside a different process,
invisible to the sender's process. That is the split brain. This step closes it with Redis
pub/sub. Read this next to `services/chat/chat/main.py`; every function named here is in that file.

## Two different Redis, on the same server

The scraper already leaned on Redis, so it helps to separate the two things Redis does, because
they are unrelated and this step uses only one of them.

- **Storage (call them drawers).** Redis as a place to put data and take it out later: `SET`/`GET`,
  lists you `LPUSH`/`RPOP` (the Celery job queue is exactly this), hashes. The data sits in the
  drawer until something reads it or it expires. Persistent.
- **Pub/sub (a live wire).** `PUBLISH` to a channel, `SUBSCRIBE` to hear it. It stores nothing.
  Only whoever is subscribed at that instant receives the message; if no one is listening it
  evaporates. There is no drawer to read back.

The scraper used both: the Celery broker is a drawer (a list used as a queue), and the SSE
progress stream is pub/sub (the worker published progress to `run:<id>`, the API's `/stream`
endpoint subscribed). This step is the **same mechanism as that SSE stream**, a different channel.
Redis stores no chat data here at all. The durable copy of every message is the Postgres row.

## The design: conns is the last hop, the bus does the reach

The fix does not remove the `conns` dict. It keeps it, but demotes it. In step 1, `conns` was the
fan-out mechanism: the send path wrote straight into it. Now `conns` is only the **last hop**, and
fan-out goes through Redis.

```
        ┌─ SUBSCRIBE chat:fanout ─┐              ┌─ SUBSCRIBE chat:fanout ─┐
        │                         │              │                         │
   ┌────▼──────┐            ┌─────┴──────────────▼─────┐             ┌──────▼────┐
   │ instance A│            │          REDIS           │             │ instance B│
   │ conns={2} │            │     channel: chat:fanout │             │ conns={1} │
   └────┬──────┘            │   (a live wire, no store)│             └────┬──────┘
        │  1. INSERT (Postgres, the durable copy)                        │
        │  2. PUBLISH ──────────►  broadcasts to ALL subscribers ────────┤
        │                                                                │
   A's subscriber hears it:                          B's subscriber hears it:
   recipient 1 in A's conns? NO  -> drop             recipient 1 in B's conns? YES -> ws.send_text
```

Every instance holds one long-lived subscription to the single channel `chat:fanout`. On send, the
receiving instance persists the row, then publishes one envelope. Every instance (including the
sender's own) hears it and delivers to whichever recipients sit in **its** `conns`. No instance
needs a global map of who is connected where. Same-instance delivery rides the identical path
(publish, loopback, subscribe, push), so there is exactly one delivery path, not a local fast path
plus a remote slow one.

## Why the envelope and conns must be separate

The published **envelope** is the message data, a plain dict serialized to JSON and sent over the
wire:

```python
{"kind": "message", "recipients": [1], "payload": {"id": 42, "body": "...", ...}}
```

`conns` is a different thing entirely: this instance's table of `user_id -> live WebSocket`.

They have to be separate because **a WebSocket object cannot be JSON-serialized**. You cannot put a
live socket onto the Redis wire. So the envelope carries only the recipient's **id** (a number),
and each instance resolves that id to a socket using its **own local `conns`**. Instance A's conns
and instance B's conns are different dictionaries holding different sockets. The id in the envelope
is universal; the socket lookup is local. That split is precisely what lets a message reach a
recipient on another box: A's lookup for user 1 misses (returns `None`, skip), B's lookup for user
1 hits (deliver). Envelope is the letter; conns is this building's mailbox directory.

## The code, in the order a message flows

**Startup: the subscriber is a background task started in `lifespan`.** `lifespan` is FastAPI's
once-at-boot / once-at-shutdown hook (everything before its `yield` runs at startup, everything
after at shutdown, the app serves in between). It opens the DB pool, connects Redis, subscribes to
the channel, and spawns the one forever-running consumer:

```python
subscriber = asyncio.create_task(_subscriber(pubsub))
```

There is nowhere else to start a process-wide background loop that outlives any single request.

**Send: `_publish` puts the envelope on the wire.** Called from `persist_and_fanout` (after the row
is committed) and from the typing handler. It only broadcasts; it delivers nothing, not even to a
recipient on the same instance.

```python
async def _publish(envelope):
    if redis_client is not None:
        await redis_client.publish(CHANNEL, json.dumps(envelope))
```

**Receive: `_subscriber` runs one item per incoming message.**

```python
async def _subscriber(pubsub):
    async for msg in pubsub.listen():        # parked between messages, wakes on each
        if msg["type"] != "message":         # skip Redis control frames (the subscribe confirm)
            continue
        text = msg["data"].decode() if isinstance(msg["data"], bytes) else msg["data"]
        await _deliver_local(json.loads(text))
```

`pubsub.listen()` is an async generator: the loop body runs once per item, and between items the
coroutine is awaiting (parked), so the event loop is free to run the WebSocket handlers. It is not
a busy loop. It also yields Redis's own bookkeeping frames (the `subscribe` confirmation), which is
why the `if msg["type"] != "message"` guard is there.

**Deliver: `_deliver_local` is where the message goes down the socket.**

```python
async def _deliver_local(envelope):
    for uid in envelope["recipients"]:       # uid from the ENVELOPE
        ws = conns.get(uid)                  # look uid up in CONNS (this instance's table)
        if ws is None:
            continue                         # not here; another instance will handle them
        if kind == "message":
            await ws.send_text(...)          # <- THIS line is the delivery
```

That `ws.send_text` is the moment bytes leave the server for the recipient's browser. The loop is
where envelope and conns meet: the envelope says who the message is for, conns says whether that
person is on this instance and on which socket.

**conns is written elsewhere.** The `/ws` endpoint populates it (`conns[user_id] = websocket` on
connect, `del` on disconnect). It is a module-level dict shared between the request world (the
socket handlers) and the background world (the subscriber). That works with no lock because asyncio
is single-threaded: only one coroutine touches it at a time.

**Do not confuse delivery with the ack.** The `/ws` handler also does
`websocket.send_text({"type": "ack", ...})`, but that goes back to the **sender** ("I saved it").
Delivery to the other person is only ever the `_deliver_local` line. Ack goes backward to who sent
it; the message goes forward to who receives it.

## The full trace, one breath

```
socket connects        -> conns[user_id] = websocket           (/ws handler)
user sends             -> persist_and_fanout: INSERT, then _publish
_publish               -> redis.publish("chat:fanout", envelope)
Redis                  -> broadcasts to every subscribed instance
_subscriber (each one) -> async for wakes, filters control frames
_deliver_local         -> for each recipient id, conns.get(id)
ws.send_text           -> DELIVERED, on whichever instance holds that socket
```

## Verify it (dev container)

```bash
uv sync --all-packages          # the new redis dep into the venv, once after the container restart
redis-cli -h redis ping         # PONG
moon run core:migrate           # idempotent; ensures msg_* tables + seed

moon run chat:dev               # instance A on :8100
moon run chat:dev-b             # instance B on :8101

websocat "ws://localhost:8101/ws?user_id=1"     # recipient on B
curl -X POST "http://localhost:8100/conversations/1/messages?sender_id=2" \
  -H 'Content-Type: application/json' \
  -d '{"conversation_id":1,"body":"crosses now","client_msg_id":"cross-3"}'   # sender on A
```

The recipient on B receives the message even though B's `conns` never held the sender. In step 1
that terminal stayed silent. `redis-cli -h redis SUBSCRIBE chat:fanout` in a spare terminal shows
the raw envelope crossing. Use a fresh `client_msg_id` each send; reusing one hits the idempotency
key and returns the original row instead of a new message.

## Is this the normal way, or a hack? Normal, with a named ceiling

One global channel, every instance subscribes, filter by local conns, is the textbook first answer
and it runs in production. What makes it a senior answer is naming its ceiling out loud: every
instance receives every message even when it holds none of the recipients. Fine with a handful of
instances, wasteful at thousands. The progression:

```
v1  ONE channel, subscribe everywhere, filter locally     <- this build
v2  PER-USER / PER-CONVERSATION channels                  the server subscribes user:{id}
      the server (not the client) subscribes on connect,  on connect, unsubscribes on
      unsubscribes on disconnect; publisher targets the   disconnect, so a node only hears
      recipient's channel                                 traffic for its own users
v3  PRESENCE / ROUTING layer                              a registry maps user -> which node;
      route straight to the node holding the recipient    stop broadcasting, route instead
      instead of broadcasting at all
```

Two correctnesses worth stating:

- **The client never subscribes to Redis.** Redis is backend infrastructure; a browser has a
  WebSocket to a server, and the server subscribes on behalf of the users it holds. The natural
  "subscribe to a specific chat" intuition is real, it just lives at the server layer.
- **Plain pub/sub is at-most-once and stores nothing.** If an instance is momentarily disconnected
  from Redis when a message publishes, it misses it. That is safe here only because Postgres is the
  durable copy, so a missed live push just means the user reads it from history on reconnect. With
  no durable store you would need a real log (Redis Streams or Kafka), which is a drawer: messages
  persist and can be replayed.

## At bigger scale (named, not built)

Stop broadcasting, start routing, and make the bus durable and partitioned:

- **Presence registry** mapping `user -> which gateway holds them`, so a send routes to the exact
  instance instead of every instance hearing everything.
- **A partitioned durable log** (Kafka or Redis Streams) partitioned by `conversation_id`, for
  durability and guaranteed per-conversation ordering (one conversation always maps to one
  partition, so its order is a straight line).
- **Fan-out by conversation size:** fan-out on write for DMs and small groups; fan-out on read (or
  a hybrid nudge) for huge rooms, where pushing synchronously to tens of thousands of sockets is
  the wrong move.
- **Shard the message store** by `conversation_id`, and keep offline delivery (a recipient online
  nowhere) as a separate async pipeline feeding a push-notification service, never the hot path.

Everything from this build survives that jump: the gateway holding sockets, the per-conversation id
sequence for ordering, the idempotency key, persist-before-deliver, Postgres as the durable truth.
Scaling swaps the broadcast bus for a routed, partitioned, durable one and adds a presence layer.

## What is next

- **Async / Mode A.** The bus reaches recipients online somewhere. A recipient online nowhere is a
  different problem: the message is already durable, but nothing wakes them. That is a Celery job
  (push notification / email) on the broker the worker already runs, not a chat hot-path concern.
- **A real create-conversation endpoint** (find-or-create a DM), so conversations are not only the
  seeded one.
- **Auth.** Supabase JWT verification; `msg_users.external_id` holds the auth `sub`.
