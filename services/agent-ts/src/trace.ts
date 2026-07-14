// Optional LangSmith tracing, inert until keyed, same contract as the Python agent's _trace.py.
//
// To keep this service dependency-light these are identity wrappers by default: the seam exists so
// loop.ts and tools.ts read exactly like their Python counterparts (a traced chain turn, a traced
// tool span). When you want real traces, swap these two for the LangSmith JS SDK:
//
//   npm i langsmith
//   import { traceable } from "langsmith/traceable";
//   import { wrapAnthropic } from "langsmith/wrappers/anthropic";
//
// Its `traceable` handles async generators natively (so wrapping runAgent as a chain span works),
// and tracing only activates when LANGSMITH_TRACING=true and LANGSMITH_API_KEY are set, so unkeyed
// local dev stays a plain call either way. That's the inert-until-keyed contract the whole stack
// keeps: no key, no-op.

import type Anthropic from "@anthropic-ai/sdk";

export function traceable<A extends unknown[], R>(
  _name: string,
  _runType: "chain" | "tool" | "llm",
  fn: (...args: A) => R,
): (...args: A) => R {
  return fn;
}

export function wrapAnthropic(client: Anthropic): Anthropic {
  return client;
}
