// The spec for runAgent, one-to-one with the Python agent's tests/test_loop.py. A scripted `complete`
// plays canned model turns so the loop is exercised with zero network. The three cases pin the
// contract: a tool call then an answer, a tool error the model recovers from, and the max_turns guard.

import assert from "node:assert/strict";
import { test } from "node:test";
import { runAgent } from "../src/loop.js";
import { Toolbox } from "../src/tools.js";
import type { AgentEvent, Block, Complete, CompleteEvent } from "../src/types.js";

async function collect(gen: AsyncIterable<AgentEvent>): Promise<AgentEvent[]> {
  const out: AgentEvent[] = [];
  for await (const ev of gen) out.push(ev);
  return out;
}

// A fake toolbox: `echo` returns its input, `boom` throws. No HTTP, same as the Python fake.
const fakeToolbox = new Toolbox([
  {
    name: "echo",
    description: "echo the input back",
    input_schema: { type: "object", properties: {} },
    fn: async (input) => ({ echoed: input.value }),
  },
  {
    name: "boom",
    description: "always throws",
    input_schema: { type: "object", properties: {} },
    fn: async () => {
      throw new Error("kaboom");
    },
  },
]);

// Build a `complete` that plays a fixed list of turns. Each turn streams its text blocks as deltas
// (so the loop's text plumbing is exercised) then emits the final message with a stop_reason.
function scripted(turns: { content: Block[]; stop_reason: string }[]): Complete {
  let i = 0;
  return async function* (): AsyncIterable<CompleteEvent> {
    const turn = turns[i++];
    for (const b of turn.content) {
      if (b.type === "text") yield { type: "text_delta", text: b.text };
    }
    yield { type: "final", content: turn.content, stop_reason: turn.stop_reason };
  };
}

test("tool call then answer", async () => {
  const complete = scripted([
    { content: [{ type: "tool_use", id: "t1", name: "echo", input: { value: "hi" } }], stop_reason: "tool_use" },
    { content: [{ type: "text", text: "the echo said hi" }], stop_reason: "end_turn" },
  ]);
  const events = await collect(runAgent("say hi", { complete, toolbox: fakeToolbox }));
  assert.deepEqual(events.at(-1), { type: "done", stop_reason: "end_turn", turns: 2 });

  const toolResult = events.find((e) => e.type === "tool_result");
  assert.ok(toolResult && toolResult.type === "tool_result" && toolResult.ok);
});

test("tool error is recoverable", async () => {
  const complete = scripted([
    { content: [{ type: "tool_use", id: "t1", name: "boom", input: {} }], stop_reason: "tool_use" },
    { content: [{ type: "text", text: "sorry, that tool failed" }], stop_reason: "end_turn" },
  ]);
  const events = await collect(runAgent("break it", { complete, toolbox: fakeToolbox }));

  const toolResult = events.find((e) => e.type === "tool_result");
  assert.ok(toolResult && toolResult.type === "tool_result");
  assert.equal(toolResult.ok, false);
  // the loop keeps going: it still reaches a normal end_turn answer
  assert.equal(events.at(-1)?.type, "done");
});

test("max_turns guard", async () => {
  // a model that asks for a tool forever
  const complete: Complete = async function* (): AsyncIterable<CompleteEvent> {
    yield { type: "final", content: [{ type: "tool_use", id: "t", name: "echo", input: {} }], stop_reason: "tool_use" };
  };
  const events = await collect(runAgent("loop forever", { complete, toolbox: fakeToolbox, maxTurns: 3 }));
  const last = events.at(-1);
  assert.ok(last && last.type === "error");
  assert.ok(last.error.includes("max_turns=3"));
});
