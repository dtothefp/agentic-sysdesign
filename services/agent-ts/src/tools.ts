// The agent's tool layer: thin async HTTP clients of the sysdesign REST API. Same design point as
// the Python agent (packages/agent/agent/tools.py): the agent is a CLIENT of the data API, not code
// fused into it. Every tool is one `fetch` to services/api, authenticated by the same X-API-Key the
// rest of the app uses. Point the loop at a local api (SYSDESIGN_API_URL=http://localhost:8000) or
// the deployed one (https://sysdesign.thedefrag.ai) by changing one env var; the loop never grows a
// second in-process code path.
//
// A tool is (name, description, JSON-Schema for its inputs, a callable). The Toolbox turns that list
// into the two things the loop needs: the schemas to hand the model, and a run(name, input) dispatch.
// Anything a callable returns must be JSON-serializable, because it goes straight back to the model
// as a tool_result.

import { traceable } from "./trace.js";
import type { ToolSchema } from "./types.js";

const API_URL = process.env.SYSDESIGN_API_URL ?? "http://localhost:8000";
const API_KEY = process.env.SYSDESIGN_API_KEY;

// The key rides the X-API-Key header when the deployment is keyed; unset means the api is open
// (local dev), the same inert-until-keyed contract the server enforces on its own /chat.
function authHeaders(): Record<string, string> {
  return API_KEY ? { "X-API-Key": API_KEY } : {};
}

async function apiGet(path: string, params?: Record<string, unknown>): Promise<unknown> {
  const url = new URL(path, API_URL);
  for (const [k, v] of Object.entries(params ?? {})) {
    if (v !== undefined && v !== null) url.searchParams.set(k, String(v));
  }
  const r = await fetch(url, { headers: authHeaders() });
  if (!r.ok) throw new Error(`GET ${path} -> ${r.status} ${await r.text()}`);
  return r.json();
}

async function apiPost(path: string, body: Record<string, unknown>): Promise<unknown> {
  const url = new URL(path, API_URL);
  const r = await fetch(url, {
    method: "POST",
    headers: { ...authHeaders(), "content-type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!r.ok) throw new Error(`POST ${path} -> ${r.status} ${await r.text()}`);
  return r.json();
}

// --- the tools -----------------------------------------------------------------
//
// Read tools (influencers, signals, ratings, digests, search) plus one action tool (trigger_run)
// and its status read (get_run). Enough surface for the agent to answer "what are we tracking /
// what's on-thesis lately / what did creators say about X" and to actually DO something ("kick off
// a scrape") and report the result. One-to-one with the Python agent's DEFAULT_TOOLS.

export interface Tool {
  name: string;
  description: string;
  input_schema: Record<string, unknown>;
  fn: (input: Record<string, unknown>) => Promise<unknown>;
}

export const DEFAULT_TOOLS: Tool[] = [
  {
    name: "list_influencers",
    description: "List the creators on Defrag's watchlist (name, instagram handle, last_scraped_at).",
    input_schema: { type: "object", properties: {} },
    fn: () => apiGet("/influencers"),
  },
  {
    name: "list_ratings",
    description:
      "List recent per-signal AI relevance ratings, newest first. Use min_relevance to see only on-thesis signals.",
    input_schema: {
      type: "object",
      properties: {
        min_relevance: { type: "number", minimum: 0, maximum: 1, description: "minimum relevance 0-1" },
        limit: { type: "integer", minimum: 1, maximum: 200, description: "max rows (default 20)" },
      },
    },
    fn: ({ min_relevance, limit }) => apiGet("/ratings", { min_relevance, limit: limit ?? 20 }),
  },
  {
    name: "list_recent_signals",
    description: "List raw scraped signals captured in the last N hours (optionally for one influencer_id).",
    input_schema: {
      type: "object",
      properties: {
        hours: { type: "integer", minimum: 1, description: "look-back window in hours (default 24)" },
        influencer_id: { type: "integer", description: "restrict to one creator" },
        limit: { type: "integer", minimum: 1, maximum: 1000, description: "max rows (default 50)" },
      },
    },
    // The api requires a time window (it's the partition key), so compute now-minus-hours to now.
    fn: ({ hours, influencer_id, limit }) => {
      const h = typeof hours === "number" ? hours : 24;
      const now = Date.now();
      return apiGet("/signals", {
        from: new Date(now - h * 3600_000).toISOString(),
        to: new Date(now).toISOString(),
        influencer_id,
        limit: limit ?? 50,
      });
    },
  },
  {
    name: "trigger_run",
    description:
      "Kick off a background scrape run. mode 'demo' (synthetic, free) or 'live' (real scrape). " +
      "Optional model ('provider/model') enables AI rating.",
    input_schema: {
      type: "object",
      properties: {
        mode: { type: "string", enum: ["demo", "live"], description: "default demo" },
        model: { type: "string", description: "e.g. 'deepseek/deepseek-chat'; omit to skip rating" },
      },
    },
    fn: ({ mode, model }) => {
      const body: Record<string, unknown> = { mode: mode ?? "demo" };
      if (model !== undefined && model !== null) body.model = model;
      return apiPost("/runs", body);
    },
  },
  {
    name: "get_run",
    description: "Get the current state of one run by id (status, done_count/total, rated_count).",
    input_schema: { type: "object", properties: { run_id: { type: "integer" } }, required: ["run_id"] },
    fn: ({ run_id }) => apiGet(`/runs/${run_id}`),
  },
  {
    name: "list_digests",
    description: "List recent agent-written weekly digests (id, status, word_count, created_at).",
    input_schema: { type: "object", properties: { limit: { type: "integer", minimum: 1, maximum: 100 } } },
    fn: ({ limit }) => apiGet("/digests", { limit: limit ?? 5 }),
  },
  {
    name: "get_digest",
    description: "Get one digest including its markdown content by id.",
    input_schema: { type: "object", properties: { digest_id: { type: "integer" } }, required: ["digest_id"] },
    fn: ({ digest_id }) => apiGet(`/digests/${digest_id}`),
  },
  {
    name: "search_signals",
    description:
      "Search signals by topic/content (hybrid full-text + semantic). Use this for 'what did " +
      "creators say about X' questions the time-windowed and rating lists can't answer.",
    input_schema: {
      type: "object",
      properties: {
        q: { type: "string", description: "free-text query; supports quoted phrases and -negation" },
        limit: { type: "integer", minimum: 1, maximum: 100, description: "max hits (default 20)" },
      },
      required: ["q"],
    },
    fn: ({ q, limit }) => apiGet("/search", { q, limit: limit ?? 20 }),
  },
];

// Indexes a list of Tools into what the loop needs: `.schemas` for the model, `.run` for dispatch.
// Kept model-agnostic (plain dict schemas) so the loop is trivially unit-testable with a fake toolbox.
export class Toolbox {
  private byName: Map<string, Tool>;

  constructor(tools: Tool[]) {
    this.byName = new Map(tools.map((t) => [t.name, t]));
  }

  get schemas(): ToolSchema[] {
    return [...this.byName.values()].map((t) => ({
      name: t.name,
      description: t.description,
      input_schema: t.input_schema,
    }));
  }

  async run(name: string, input: Record<string, unknown> | undefined): Promise<unknown> {
    const tool = this.byName.get(name);
    if (!tool) throw new Error(`unknown tool ${name}`);
    // Wrap per call so a keyed LangSmith deployment records this as a "tool" span named after the
    // tool, with its input args and HTTP result. traceable is an identity passthrough when tracing
    // is off (see trace.ts), so this is a plain call in that case.
    return traceable(tool.name, "tool", tool.fn)(input ?? {});
  }
}

// The production toolbox: every tool wired to the REST API at SYSDESIGN_API_URL.
export function defaultToolbox(): Toolbox {
  return new Toolbox(DEFAULT_TOOLS);
}
