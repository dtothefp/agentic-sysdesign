"""The chat gateway: a FastAPI + WebSocket service for direct messaging.

Separate service from the scraper API on purpose. The scraper is request/response and scales
on CPU-per-job; this is a long-lived-connection gateway that holds a socket open per online
user and scales on connection count. Different shape, different process, so a redeploy of one
never drops the other's live sockets. It shares the monorepo's Supabase project through
packages/core (the msg_* tables) and, later, the same Redis the worker already uses.

PHASE 3a (this file): ONE instance, connections tracked in an in-memory dict. That dict is the
whole point and also the whole limitation: it lives in THIS process's memory, so if you run two
instances, instance A cannot see the sockets held by instance B. That is exactly the failure
Redis pub/sub fixes in 3c. Until then: one process, and the split-brain is left visible on
purpose so the reason for Redis is felt, not just asserted.

The socket carries BOTH directions, which is why this is a WebSocket and not SSE:
  * downstream  server -> client : a delivered message, pushed the instant it lands.
  * upstream    client -> server : the client SENDS messages and TYPING pings over the same
    socket. Typing is high-frequency, fire-and-forget, ephemeral (never touches Postgres). That
    upstream chatter is the thing SSE can't carry on one connection, and the reason WS wins here.

Auth is punted (Phase 5). Identity is a `user_id` query param. In prod that becomes a verified
Supabase JWT and `msg_users.external_id` holds the auth `sub`.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager

from common.db import DATABASE_URL
from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel, Field

# Async pool, not the scraper's sync one. Every handler here is `async def` because a WebSocket
# handler owns its connection for minutes; a blocking DB call would stall the event loop and
# every OTHER socket on this process with it. So DB access is await-native end to end.
pool = AsyncConnectionPool(DATABASE_URL, min_size=1, max_size=10, open=False)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await pool.open()
    yield
    await pool.close()


app = FastAPI(title="sysdesign-chat", lifespan=lifespan)


# --- the connection registry ------------------------------------------------------
#
# user_id -> their live WebSocket. This is the routing table: to deliver to user 2, look up
# conns[2] and push down it. Absent from the dict == offline == nothing to push to (the message
# is already saved in Postgres, so they get it from history on reconnect).
#
# Two honest simplifications for the drill, both one-liners to remove later:
#   * one socket per user. Prod is `dict[int, set[WebSocket]]` so a user's phone AND laptop both
#     receive. Here a second login for the same user replaces the first.
#   * process-local. This is the dict that does NOT survive going multi-instance. See 3c.
conns: dict[int, WebSocket] = {}


# --- the shared delivery path -----------------------------------------------------
#
# Both send paths (a frame over the WS, and the REST POST) funnel through here, so "persist then
# fan out to whoever's online" is written exactly once.
async def persist_and_fanout(sender_id: int, conversation_id: int, body: str, client_msg_id: str) -> dict:
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            # The idempotent write. A retry reuses client_msg_id, conflicts on the unique index,
            # and DO NOTHING means the duplicate is silently dropped instead of stored twice.
            await cur.execute(
                """
                INSERT INTO msg_messages (conversation_id, sender_id, body, client_msg_id)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (sender_id, client_msg_id) DO NOTHING
                RETURNING id, conversation_id, sender_id, body, client_msg_id, created_at
                """,
                (conversation_id, sender_id, body, client_msg_id),
            )
            row = await cur.fetchone()
            if row is None:
                # Conflict fired: the message already exists from the first attempt. Return that
                # original row so the retry still gets a success and the SAME message id.
                await cur.execute(
                    """
                    SELECT id, conversation_id, sender_id, body, client_msg_id, created_at
                    FROM msg_messages WHERE sender_id = %s AND client_msg_id = %s
                    """,
                    (sender_id, client_msg_id),
                )
                row = await cur.fetchone()

            # Who else is in this conversation? These are the fan-out targets. Scales to groups
            # for free: a DM returns 1 recipient, a group returns N, same query.
            await cur.execute(
                "SELECT user_id FROM msg_participants WHERE conversation_id = %s AND user_id != %s",
                (conversation_id, sender_id),
            )
            recipients = [r["user_id"] for r in await cur.fetchall()]

    payload = _message_payload(row)

    # The real-time push. For each recipient with a live socket ON THIS INSTANCE, send it now.
    # A recipient not in `conns` is offline (from this process's point of view) and simply isn't
    # pushed to: their message is already durable in Postgres. Waking an offline user (push
    # notification, email) is the Celery job in a later phase, not this hot path.
    for uid in recipients:
        ws = conns.get(uid)
        if ws is not None:
            await ws.send_text(json.dumps({"type": "message", **payload}))

    return payload


def _message_payload(row: dict) -> dict:
    return {
        "id": row["id"],
        "conversation_id": row["conversation_id"],
        "sender_id": row["sender_id"],
        "body": row["body"],
        "client_msg_id": row["client_msg_id"],
        "created_at": row["created_at"].isoformat(),
    }


# --- the WebSocket endpoint -------------------------------------------------------
@app.websocket("/ws")
async def ws(websocket: WebSocket, user_id: int = Query(...)):
    await websocket.accept()
    conns[user_id] = websocket  # register: this user is now reachable for live delivery
    try:
        # The receive loop. Blocks awaiting the next frame FROM this client. Upstream traffic.
        while True:
            raw = await websocket.receive_text()
            try:
                frame = json.loads(raw)
            except json.JSONDecodeError:
                await websocket.send_text(json.dumps({"type": "error", "detail": "invalid json"}))
                continue

            action = frame.get("action")

            if action == "send":
                # A message. Persist + fan out through the shared path, then ack the SENDER so
                # their client can mark it delivered (and dedupe on the returned id).
                payload = await persist_and_fanout(
                    sender_id=user_id,
                    conversation_id=frame["conversation_id"],
                    body=frame["body"],
                    client_msg_id=frame["client_msg_id"],
                )
                await websocket.send_text(json.dumps({"type": "ack", **payload}))

            elif action == "typing":
                # High-frequency, ephemeral, upstream. Never touches Postgres (a typing state is
                # worthless in 3 seconds), just relayed to the other online participants. THIS is
                # the traffic that justifies a duplex socket over SSE: it flows client->server
                # constantly and would need a whole second channel under SSE.
                async with pool.connection() as conn:
                    async with conn.cursor() as cur:
                        await cur.execute(
                            "SELECT user_id FROM msg_participants WHERE conversation_id = %s AND user_id != %s",
                            (frame["conversation_id"], user_id),
                        )
                        others = [r[0] for r in await cur.fetchall()]
                for uid in others:
                    peer = conns.get(uid)
                    if peer is not None:
                        await peer.send_text(
                            json.dumps({"type": "typing", "conversation_id": frame["conversation_id"], "user_id": user_id})
                        )

            else:
                await websocket.send_text(json.dumps({"type": "error", "detail": f"unknown action {action!r}"}))

    except WebSocketDisconnect:
        # Client went away. Deregister so we stop trying to push to a dead socket. Guard on
        # identity: only remove our own entry, not a newer socket that replaced us for this user.
        if conns.get(user_id) is websocket:
            del conns[user_id]


# --- REST: a convenience send + history ------------------------------------------
class SendIn(BaseModel):
    conversation_id: int
    body: str
    client_msg_id: str = Field(..., description="client-minted idempotency key; retries reuse it")


@app.post("/conversations/{conversation_id}/messages")
async def send_message(conversation_id: int, sender_id: int, msg: SendIn):
    # Same delivery path as the WS `send` frame. Exists so you can trigger a send with plain curl
    # (no WebSocket client needed) and watch it arrive live on a recipient's websocat session.
    if msg.conversation_id != conversation_id:
        raise HTTPException(400, "conversation_id in path and body disagree")
    return await persist_and_fanout(sender_id, conversation_id, msg.body, msg.client_msg_id)


@app.get("/conversations/{conversation_id}/messages")
async def history(
    conversation_id: int,
    before: int | None = Query(None, description="cursor: return messages with id < this"),
    limit: int = Query(50, le=100),
):
    # Cursor pagination, riding the (conversation_id, id DESC) index. `before` is the id of the
    # oldest message the client already has; omit it for the newest page. No OFFSET, so new
    # messages arriving mid-scroll never shift the window.
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT id, conversation_id, sender_id, body, client_msg_id, created_at
                FROM msg_messages
                WHERE conversation_id = %s AND (%s::bigint IS NULL OR id < %s)
                ORDER BY id DESC
                LIMIT %s
                """,
                (conversation_id, before, before, limit),
            )
            rows = await cur.fetchall()
    return {"messages": [_message_payload(r) for r in rows]}


@app.get("/health")
async def health():
    return {"status": "ok", "online_users": list(conns.keys())}
