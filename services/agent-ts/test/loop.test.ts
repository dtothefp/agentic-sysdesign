// ReAct loop spec, one-to-one with the Python tests/test_loop.py. No network, no Anthropic: a
// scripted `complete` plays the model turns and a fake `runTool` records calls, both injected via
// runAgent's options seams, so these prove the loop MECHANICS in isolation (the part a code
// challenge actually grades): tool dispatch plus result feed-back plus stop, error recovery,
// and the max_turns guard.

import assert from "node:assert/strict"
import { test } from "node:test"
import { runAgent, simpleComplete } from "../src/loop.js"
import type {
  AgentEvent,
  Block,
  Complete,
  CompleteEvent,
  RunTool,
} from "../src/types.js"

async function collect(gen: AsyncIterable<AgentEvent>): Promise<AgentEvent[]> {
  const out: AgentEvent[] = []
  for await (const ev of gen) out.push(ev)
  return out
}

// One scripted model turn: the blocks to emit and the stop_reason to end it with. Named rather than
// an inline object literal so oxfmt does not collapse it and drop the member separator.
interface ScriptedTurn {
  content: Block[]
  stop_reason: string
}

// A scripted stand-in for anthropicComplete: emits each turn's text deltas, then its final block(s).
function scripted(turns: ScriptedTurn[]): Complete {
  let i = 0
  return async function* (): AsyncIterable<CompleteEvent> {
    const turn = turns[i++]
    for (const b of turn.content) {
      if (b.type === "text") yield { type: "text_delta", text: b.text }
    }
    yield {
      type: "final",
      content: turn.content,
      stop_reason: turn.stop_reason,
    }
  }
}

// A fake runTool. `echo` records its input and echoes it back, `boom` always raises to exercise
// the loop's error-recovery path. Mirrors Python's _fake_run_tool.
function fakeRunTool(calls: Record<string, unknown>[]): RunTool {
  return async (name, input) => {
    if (name === "boom") throw new Error("kaboom")
    calls.push(input)
    return { echoed: input }
  }
}

// Sanity check: simpleComplete streams text and finishes without network or tools.
test("simpleComplete echoes the user message", async () => {
  const events = await collect(runAgent("hello", { complete: simpleComplete }))
  assert.deepEqual(events.at(-1), {
    type: "done",
    stop_reason: "end_turn",
    turns: 1,
  })
  const text = events
    .filter((e) => e.type === "text")
    .map((e) => (e.type === "text" ? e.text : ""))
  assert.ok(text.join("").includes("hello"))
})

test("tool call then answer", async () => {
  const calls: Record<string, unknown>[] = []
  const complete = scripted([
    {
      content: [
        { type: "tool_use", id: "t1", name: "echo", input: { value: "hi" } },
      ],
      stop_reason: "tool_use",
    },
    {
      content: [{ type: "text", text: "the echo said hi" }],
      stop_reason: "end_turn",
    },
  ])
  const events = await collect(
    runAgent("say hi", { complete, runTool: fakeRunTool(calls) }),
  )
  const types = events.map((e) => e.type)

  assert.ok(types.includes("tool_use") && types.includes("tool_result"))
  assert.deepEqual(calls, [{ value: "hi" }]) // the tool actually ran with the model's input
  const toolResult = events.find((e) => e.type === "tool_result")
  assert.ok(
    toolResult && toolResult.type === "tool_result" && toolResult.ok === true,
  )
  assert.deepEqual(events.at(-1), {
    type: "done",
    stop_reason: "end_turn",
    turns: 2,
  })
})

test("tool error is recoverable", async () => {
  const complete = scripted([
    {
      content: [{ type: "tool_use", id: "t1", name: "boom", input: {} }],
      stop_reason: "tool_use",
    },
    {
      content: [{ type: "text", text: "sorry, that tool failed" }],
      stop_reason: "end_turn",
    },
  ])
  const events = await collect(
    runAgent("break it", { complete, runTool: fakeRunTool([]) }),
  )

  const toolResult = events.find((e) => e.type === "tool_result")
  assert.ok(toolResult && toolResult.type === "tool_result")
  assert.equal(toolResult.ok, false)
  assert.ok(String(toolResult.result).includes("kaboom"))
  assert.equal(events.at(-1)?.type, "done") // the loop kept going and finished after the error
})

test("max_turns guard", async () => {
  // a model that asks for a tool every single turn must hit the guard, not spin forever
  const complete: Complete = async function* (): AsyncIterable<CompleteEvent> {
    yield {
      type: "final",
      content: [{ type: "tool_use", id: "t", name: "echo", input: {} }],
      stop_reason: "tool_use",
    }
  }
  const events = await collect(
    runAgent("loop forever", {
      complete,
      runTool: fakeRunTool([]),
      maxTurns: 3,
    }),
  )

  const last = events.at(-1)
  assert.ok(last && last.type === "error")
  assert.ok(last.error.includes("max_turns=3"))
  assert.equal(events.filter((e) => e.type === "tool_use").length, 3) // exactly maxTurns attempts
})
