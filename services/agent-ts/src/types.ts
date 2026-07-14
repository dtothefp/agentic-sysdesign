// Shared shapes for the loop. Kept model-agnostic on purpose: the loop only ever sees these plain
// objects, never the Anthropic SDK's classes. That's what makes it unit-testable with a scripted
// `complete` (see test/loop.test.ts) and swappable to another provider without touching loop.ts.

// A content block as the loop understands it. Either model text or a tool request.
export type Block =
  | { type: "text"; text: string }
  | { type: "tool_use"; id: string; name: string; input: Record<string, unknown> };

// What `complete` yields: token deltas as they stream, then exactly one final message. Mirrors the
// Python agent's {"type":"text_delta"} events followed by one {"type":"final"}.
export type CompleteEvent =
  | { type: "text_delta"; text: string }
  | { type: "final"; content: Block[]; stop_reason: string | null };

// The model-call seam. Same role as Python's Complete alias: messages + schemas + system + model,
// returns an async iterator of CompleteEvents. anthropicComplete is the real one; tests inject a
// fake that never touches the network.
export type Complete = (
  messages: Message[],
  toolSchemas: ToolSchema[],
  system: string,
  model: string,
) => AsyncIterable<CompleteEvent>;

// An Anthropic-shaped message. content is a string on the user's first turn, or a list of blocks
// (assistant turns, and user tool_result turns).
export interface Message {
  role: "user" | "assistant";
  content: string | unknown[];
}

export interface ToolSchema {
  name: string;
  description: string;
  input_schema: Record<string, unknown>;
}

// The tool-dispatch seam. Same role as the Complete seam: runAgent calls it to execute one tool by
// name. The real one (tools.ts runTool) hits the REST API; tests inject a fake that records calls
// and never touches the network. Python's tests monkeypatch loop.run_tool for the same reason; ESM
// imports can't be monkeypatched cleanly, so we thread it through as an option instead.
export type RunTool = (name: string, input: Record<string, unknown>) => Promise<unknown>;

// The flat event stream runAgent yields, one per loop event. Same wire shape as the Python agent
// (loop.py) so a single chat-web UI can read either backend over the same SSE contract.
export type AgentEvent =
  | { type: "text"; text: string }
  | { type: "tool_use"; id: string; name: string; input: Record<string, unknown> }
  | { type: "tool_result"; id: string; name: string; ok: boolean; result: unknown }
  | { type: "done"; stop_reason: string | null; turns: number }
  | { type: "error"; error: string };
