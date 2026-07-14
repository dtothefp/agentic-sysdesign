// The ReAct loop. This is the whole product; the CLI and the SSE server are just transports.
//
// Same mechanics as the Python agent (packages/agent/agent/loop.py), which is exactly what a code
// challenge pokes at:
//
//   1. Send the running transcript plus the tool schemas to the model.
//   2. If it returns a normal answer (stop_reason 'end_turn'), we're done.
//   3. If it returns one or more tool_use blocks (stop_reason 'tool_use'), run each tool, append the
//      results as a user turn, and loop. A tool that throws does NOT kill the turn: we hand the error
//      back as a tool_result with is_error, so the model can apologize or try another path.
//   4. A max_turns guard stops a model that keeps asking for tools forever (a real failure mode).
//
// The model call is abstracted behind `complete`, an async generator that yields text deltas then
// one final message. anthropicComplete streams real tokens off the Anthropic SDK; tests inject a
// scripted `complete` and never touch the network. That seam is what makes the loop unit-testable
// and keeps runAgent model-agnostic: it only ever sees plain objects.
//
// The one real difference from the Python agent is the concurrency model, and it's the interesting
// contrast. Python's run_agent is a SYNC generator (sync Anthropic + sync httpx), so its async SSE
// server has to pull each event in a worker thread (asyncio.to_thread) to keep from blocking the
// event loop. Here runAgent is a NATIVE async generator: every await (the model stream, each tool
// fetch) already yields the event loop, so server.ts just `for await`s it directly. Same loop, one
// fewer moving part, because the language's I/O is async to begin with.

import Anthropic from "@anthropic-ai/sdk";
import { defaultToolbox, Toolbox } from "./tools.js";
import { wrapAnthropic } from "./trace.js";
import type { AgentEvent, Block, Complete, CompleteEvent, Message, ToolSchema } from "./types.js";

export const DEFAULT_MODEL = process.env.CHAT_MODEL ?? "claude-sonnet-5";
export const MAX_TOKENS = 4096;

export const SYSTEM =
  "You are the sysdesign assistant, a data agent over Defrag's influencer-intelligence system. " +
  "Use the tools to answer questions about tracked creators, their scraped signals, the AI " +
  "relevance ratings, background scrape runs, and the weekly digests. For questions about what " +
  "creators have said on a TOPIC (rather than by time or rating), use search_signals. When the user asks you to " +
  "DO something, like start a scrape, call the tool and then report what happened, including any " +
  "id the caller can follow up with. Default runs to demo mode unless the user asks for live. " +
  "Keep answers short and concrete.";

// The real model call: one streamed turn off the Anthropic SDK. Yields text deltas as they arrive
// (that's the token streaming the UI shows), then normalizes the final message's content blocks into
// plain objects so the rest of the loop never touches SDK classes.
export async function* anthropicComplete(
  messages: Message[],
  toolSchemas: ToolSchema[],
  system: string,
  model: string,
): AsyncIterable<CompleteEvent> {
  // wrapAnthropic makes this a LangSmith LLM span when tracing is on, identity no-op otherwise.
  const client = wrapAnthropic(new Anthropic()); // reads ANTHROPIC_API_KEY from the environment
  const stream = client.messages.stream({
    model,
    max_tokens: MAX_TOKENS,
    system,
    tools: toolSchemas as Anthropic.Tool[],
    messages: messages as Anthropic.MessageParam[],
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
  toolbox?: Toolbox;
  system?: string;
  model?: string;
  maxTurns?: number;
  history?: Message[];
}

// Run one user turn to completion, yielding a flat stream of events the transports render:
//
//   { type:"text", text }                                   a streamed answer token
//   { type:"tool_use", id, name, input }                    the model asked to call a tool
//   { type:"tool_result", id, name, ok, result }            what the tool returned
//   { type:"done", stop_reason, turns }                     terminal, the answer is complete
//   { type:"error", error }                                 no final message, or max_turns hit
//
// Pass `history` (prior [{role, content}] messages) to continue a multi-turn chat.
export async function* runAgent(userMessage: string, opts: RunAgentOptions = {}): AsyncIterable<AgentEvent> {
  const complete = opts.complete ?? anthropicComplete;
  const toolbox = opts.toolbox ?? defaultToolbox();
  const system = opts.system ?? SYSTEM;
  const model = opts.model ?? DEFAULT_MODEL;
  const maxTurns = opts.maxTurns ?? 8;
  const schemas = toolbox.schemas;

  const messages: Message[] = [...(opts.history ?? [])];
  messages.push({ role: "user", content: userMessage });

  for (let turn = 1; turn <= maxTurns; turn++) {
    let final: { content: Block[]; stop_reason: string | null } | null = null;
    for await (const ev of complete(messages, schemas, system, model)) {
      if (ev.type === "text_delta") yield { type: "text", text: ev.text };
      else if (ev.type === "final") final = { content: ev.content, stop_reason: ev.stop_reason };
    }

    if (final === null) {
      // a well-behaved complete always yields a final; guard anyway
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
        result = await toolbox.run(block.name, block.input);
        ok = true;
      } catch (e) {
        // a failing tool must be recoverable, not fatal
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
