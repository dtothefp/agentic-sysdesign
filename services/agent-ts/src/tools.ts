// Thin HTTP clients of the sysdesign REST API. Read-only tools: list and search surfaces only.
// Scraping runs on a schedule (Celery); the chat agent reads what's already in the DB.

import { traceable } from "./trace.js";
import type { ToolSchema } from "./types.js";

const API_URL = process.env.SYSDESIGN_API_URL ?? "http://localhost:8000";
const API_KEY = process.env.SYSDESIGN_API_KEY;

function authHeaders(): Record<string, string> {
  return API_KEY ? { "X-API-Key": API_KEY } : {};
}

export async function apiGet(path: string, params?: Record<string, unknown>): Promise<unknown> {
  const url = new URL(path, API_URL);
  for (const [k, v] of Object.entries(params ?? {})) {
    if (v !== undefined && v !== null) url.searchParams.set(k, String(v));
  }
  const r = await fetch(url, { headers: authHeaders() });
  if (!r.ok) throw new Error(`GET ${path} -> ${r.status} ${await r.text()}`);
  return r.json();
}

export interface Tool {
  name: string;
  description: string;
  input_schema: Record<string, unknown>;
  fn: (input: Record<string, unknown>) => Promise<unknown>;
}

export const TOOLS: Record<string, Tool> = {
  list_influencers: {
    name: "list_influencers",
    description: "List the creators on Defrag's watchlist (name, instagram handle, last_scraped_at).",
    input_schema: { type: "object", properties: {} },
    fn: async () => apiGet("/influencers"),
  },
  list_ratings: {
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
    fn: async ({ min_relevance, limit }) => apiGet("/ratings", { min_relevance, limit: limit ?? 20 }),
  },
  list_recent_signals: {
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
    fn: async ({ hours, influencer_id, limit }) => {
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
  list_digests: {
    name: "list_digests",
    description: "List recent agent-written weekly digests (id, status, word_count, created_at).",
    input_schema: {
      type: "object",
      properties: { limit: { type: "integer", minimum: 1, maximum: 100 } },
    },
    fn: async ({ limit }) => apiGet("/digests", { limit: limit ?? 5 }),
  },
  get_digest: {
    name: "get_digest",
    description: "Get one digest including its markdown content by id.",
    input_schema: {
      type: "object",
      properties: { digest_id: { type: "integer" } },
      required: ["digest_id"],
    },
    fn: async ({ digest_id }) => apiGet(`/digests/${digest_id}`),
  },
  search_signals: {
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
    fn: async ({ q, limit }) => apiGet("/search", { q, limit: limit ?? 20 }),
  },
};

const TRACED: Record<string, Tool["fn"]> = Object.fromEntries(
  Object.entries(TOOLS).map(([name, tool]) => [name, traceable(name, "tool", tool.fn)]),
);

export const TOOL_SCHEMAS: ToolSchema[] = Object.values(TOOLS).map((t) => ({
  name: t.name,
  description: t.description,
  input_schema: t.input_schema,
}));

export async function runTool(name: string, input: Record<string, unknown> = {}): Promise<unknown> {
  const fn = TRACED[name];
  if (!fn) throw new Error(`unknown tool ${name}`);
  return fn(input);
}
