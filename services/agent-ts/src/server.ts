// SSE transport: a tiny Hono app that streams the agent loop to a browser. A separate process from
// services/api on purpose, because the agent is a CLIENT of the data API, not part of it: it gets
// its own /chat surface on its own port (8100) and dials the api over HTTP like any other client.
//
// The contrast with the Python server.py is the whole point of this parallel build. There, run_agent
// is a SYNC generator, so the async handler pulls each event in a worker thread via
// asyncio.to_thread to avoid blocking the event loop. Here runAgent is a native async generator, so
// this handler just `for await`s it and writes each event straight to the SSE stream. No thread
// bridge, no sync/async seam. Same wire format as the api's run/digest streams: one SSE event per
// loop event, `event:` is the loop event type, `data:` is the JSON event.

import { serve } from "@hono/node-server";
import { timingSafeEqual } from "node:crypto";
import { Hono } from "hono";
import { streamSSE } from "hono/streaming";
import { runAgent } from "./loop.js";
import type { Message } from "./types.js";

const app = new Hono();

// Same inert-until-keyed gate as services/api and the Python agent: one shared key on the X-API-Key
// header. /health stays open so Railway's headerless healthcheck passes; an unset key means fully
// open (local dev). Constant-time compare so the check can't leak the key byte by byte. Only the
// /chat route carries the gate, so /health is never blocked.
app.use("/chat", async (c, next) => {
  const expected = process.env.SYSDESIGN_API_KEY;
  if (!expected) return next();
  const got = Buffer.from(c.req.header("X-API-Key") ?? "");
  const want = Buffer.from(expected);
  if (got.length !== want.length || !timingSafeEqual(got, want)) {
    return c.json({ detail: "missing or invalid X-API-Key" }, 401);
  }
  return next();
});

app.get("/health", (c) => c.json({ status: "ok" }));

app.post("/chat", (c) => {
  return streamSSE(c, async (stream) => {
    const body = await c.req.json<{ message: string; history?: Message[] }>();
    for await (const ev of runAgent(body.message, { history: body.history })) {
      await stream.writeSSE({ event: ev.type, data: JSON.stringify(ev) });
    }
  });
});

export default app;

export function startServer(port = Number(process.env.PORT ?? 8100)): void {
  serve({ fetch: app.fetch, port, hostname: "0.0.0.0" });
  console.log(`sysdesign chat agent (ts) listening on :${port}`);
}
