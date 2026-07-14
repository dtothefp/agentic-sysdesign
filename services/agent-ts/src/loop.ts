// The ReAct loop. This is the whole product; the CLI and the SSE server are just transports.
//
// The one real difference from the Python agent is concurrency: runAgent is a NATIVE async
// generator. Every await (model stream, tool fetch) yields the event loop, so server.ts just
// `for await`s it directly. No asyncio.to_thread bridge.

import Anthropic from "@anthropic-ai/sdk";
import { runTool, TOOL_SCHEMAS } from "./tools.js";
import { wrapAnthropic } from "./trace.js";
import type { AgentEvent, Block, Complete, CompleteEvent, Message, RunTool, ToolSchema } from "./types.js";

export const DEFAULT_MODEL = process.env.CHAT_MODEL ?? "claude-sonnet-5";
export const MAX_TOKENS = 4096;

export const SYSTEM =
  "You are the sysdesign assistant, a read-only data agent over Defrag's influencer-intelligence " +
  "system. Use the tools to answer questions about tracked creators, their scraped signals, AI " +
  "relevance ratings, and weekly digests. For questions about what creators have said on a TOPIC " +
  "(rather than by time or rating), use search_signals. You cannot start scrapes or trigger " +
  "background jobs — scraping is scheduled separately. Keep answers short and concrete.";

// Toy `complete` for learning async generators. No Anthropic client, no network.
//
// `complete` means "one model turn": stream some tokens, then hand back one final message.
// runAgent will call it once per loop iteration once you build the ReAct loop.
//
// Try in a REPL:
//   for await (const ev of simpleComplete([{ role: "user", content: "hello" }], [], SYSTEM, "fake")) console.log(ev)
export async function* simpleComplete(
  messages: Message[],
  _toolSchemas: ToolSchema[],
  _system: string,
  _model: string,
): AsyncIterable<CompleteEvent> {
  const last = messages.at(-1)?.content ?? "";
  const reply = typeof last === "string" ? `You said: ${last}` : "Thanks, I saw your tool results.";

  for (const word of reply.split(/\s+/)) {
    yield { type: "text_delta", text: word + " " };
  }
  yield {
    type: "final",
    content: [{ type: "text", text: reply }],
    stop_reason: "end_turn",
  };
}

// One streamed model turn via the Anthropic SDK. Omits tools until toolSchemas is non-empty.
export async function* anthropicComplete(
  messages: Message[],
  toolSchemas: ToolSchema[],
  system: string,
  model: string,
): AsyncIterable<CompleteEvent> {
  const client = wrapAnthropic(new Anthropic()); // reads ANTHROPIC_API_KEY from the environment
  const stream = client.messages.stream({
    model,
    max_tokens: MAX_TOKENS,
    system,
    messages: messages as Anthropic.MessageParam[],
    ...(toolSchemas.length > 0 ? { tools: toolSchemas as Anthropic.Tool[] } : {}),
  });

  for await (const event of stream) {
    if (event.type === "content_block_delta" && event.delta.type === "text_delta") {
      yield { type: "text_delta", text: event.delta.text };
    }
  }
  const final = await stream.finalMessage();

  const content: Block[] = [];
  for (const block of final.content) {
    if (block.type === "text") {
      content.push({ type: "text", text: block.text });
    } else if (block.type === "tool_use") {
      content.push({ type: "tool_use", id: block.id, name: block.name, input: block.input as Record<string, unknown> });
    }
  }
  yield { type: "final", content, stop_reason: final.stop_reason };
}

export interface RunAgentOptions {
  complete?: Complete;
  runTool?: RunTool;
  system?: string;
  model?: string;
  maxTurns?: number;
  history?: Message[];
}

export async function* runAgent(userMessage: string, opts: RunAgentOptions = {}): AsyncIterable<AgentEvent> {
  const complete = opts.complete ?? anthropicComplete;
  const dispatch = opts.runTool ?? runTool;
  const system = opts.system ?? SYSTEM;
  const model = opts.model ?? DEFAULT_MODEL;
  const maxTurns = opts.maxTurns ?? 8;
  const schemas = TOOL_SCHEMAS;

  const messages: Message[] = [...(opts.history ?? [])];
  messages.push({ role: "user", content: userMessage });

  for (let turn = 1; turn <= maxTurns; turn++) {
    let final: { content: Block[]; stop_reason: string | null } | null = null;
    for await (const ev of complete(messages, schemas, system, model)) {
      if (ev.type === "text_delta") {
        yield { type: "text", text: ev.text };
      } else if (ev.type === "final") {
        final = { content: ev.content, stop_reason: ev.stop_reason };
      }
    }

    if (final === null) {
      yield { type: "error", error: "model produced no final message" };
      return;
    }

    messages.push({ role: "assistant", content: final.content });

    if (final.stop_reason !== "tool_use") {
      yield { type: "done", stop_reason: final.stop_reason, turns: turn };
      return;
    }

    const toolResults: unknown[] = [];
    for (const block of final.content) {
      if (block.type !== "tool_use") continue;
      yield { type: "tool_use", id: block.id, name: block.name, input: block.input };
      let result: unknown;
      let ok: boolean;
      try {
        result = await dispatch(block.name, block.input);
        ok = true;
      } catch (e) {
        const err = e as Error;
        result = `${err.name}: ${err.message}`;
        ok = false;
      }
      yield { type: "tool_result", id: block.id, name: block.name, ok, result };
      toolResults.push({
        type: "tool_result",
        tool_use_id: block.id,
        content: JSON.stringify(result),
        is_error: !ok,
      });
    }
    messages.push({ role: "user", content: toolResults });
  }

  yield { type: "error", error: `hit max_turns=${maxTurns} without a final answer` };
}
