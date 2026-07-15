"""Module 5: the digest agent's tool surface, as a remote MCP server.

This replaces the custom tool `get_rated_signals` that the Celery worker used to answer
host-side while babysitting the session (worker/tasks.py, now retired). The migration is the
Module 5 lesson made concrete: a custom tool is bound to whatever process holds the session's
event stream, so it can't survive an unattended deployment. An MCP tool has no such tether. The
agent dials this server directly through Anthropic's MCP proxy, so nothing has to sit on the
stream waiting to answer.

Co-mounted on the FastAPI app at `/mcp` (see api/main.py), so it shares the app's process,
its connection target, and its lifespan. Two consequences of the co-mount:

  1. The server queries whatever DATABASE_URL its own process was booted with, so it needs no
     database configuration of its own per environment. The local API already points at the
     local (or tunnelled) DB, a preview API at the preview DB, prod at prod, so each tier's
     /mcp reads that tier's data for free. (The agent still has to be told which tier's /mcp
     URL to dial, that part isn't free, it's baked into the agent config per environment.)
  2. The `get_rated_signals` SQL is written once, in common/digests.py, and imported by both
     this server and the (now retired) worker path, so the query is never duplicated across
     two definitions that could drift.

Transport is Streamable HTTP (what the Managed Agents MCP proxy speaks). DNS-rebinding
protection is off because the Host varies across tiers (localhost, the Cloudflare tunnel
hostname, per-PR *.up.railway.app, the prod domain) and we enforce our own bearer auth on the
`/mcp` path instead (api/main.py, the same inert-until-keyed SYSDESIGN_API_KEY contract as the
REST routes). The vault injects that bearer at egress via a static_bearer credential keyed to
this server's URL (services/managed-agents/vault/mcp-bearer.yaml); the sandbox never sees the token.
"""

from common.clusters import get_signal_clusters as _clusters
from common.digests import get_rated_signals as _query
from common.search import search_signals as _search
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

# Host check off; we authenticate the /mcp path ourselves (api/main.py). See module docstring.
_SECURITY = TransportSecuritySettings(enable_dns_rebinding_protection=False)

# stateless_http: each tool call is a self-contained request/response, no server-side session
# to keep alive between calls, which is all this read-only tool needs and the simplest thing to
# co-mount. streamable_http_path="/" so that mounting the app at "/mcp" yields the endpoint /mcp.
mcp = FastMCP(
    "sysdesign",
    stateless_http=True,
    streamable_http_path="/",
    transport_security=_SECURITY,
)


@mcp.tool()
def get_rated_signals(days: int = 7, min_relevance: float = 0.5) -> list[dict]:
    """Rated Instagram posts joined to their source signals: creator handle, post URL, caption
    excerpt, captured_at, plus the rating (relevance, confidence, topics, summary). This join is
    not available from the REST API (GET /ratings returns ratings with no handle or URL). Returns
    up to 100 rows, highest relevance first.

    days: look-back window in days (default 7).
    min_relevance: minimum relevance score 0-1 (default 0.5).
    """
    # No dsn override: reads this process's DATABASE_URL, so the tier's own database is queried.
    return _query(days=days, min_relevance=min_relevance)


@mcp.tool()
def search_signals(query: str, limit: int = 20) -> dict:
    """Hybrid search over all tracked signal captions (not just rated ones): Postgres full-text
    (lexical) fused with pgvector semantic similarity via Reciprocal Rank Fusion. Use this to find
    posts about a TOPIC or CONCEPT ("autonomous agents", "RAG pipelines", a product name) rather
    than to list recently-rated posts (that's get_rated_signals). Complements get_rated_signals:
    search finds candidates by content, get_rated_signals reads the AI relevance layer.

    Returns {"query", "semantic", "hits"}. Each hit has content_hash, handle, url, caption excerpt,
    captured_at, a fused `score` (higher = better, only comparable within this result set), and
    `sources` naming which halves found it (["lexical","semantic"] means both agreed, the strongest
    signal). When it has been rated, the hit also carries relevance/summary/topics. `semantic` is
    false when no embedding model is configured, in which case results are lexical-only.

    query: free text; supports quoted "exact phrases", OR, and -negation (websearch syntax).
    limit: max hits to return (default 20).
    """
    # No dsn override: reads this process's DATABASE_URL, so the tier's own database is queried,
    # exactly like get_rated_signals. Shares common.search.search_signals with GET /search so the
    # tool and the REST endpoint can't drift.
    return _search(query=query, limit=limit)


@mcp.tool()
def get_signal_clusters(days: int = 7, min_relevance: float = 0.5, max_themes: int = 15) -> dict:
    """The week's rated posts pre-grouped into emergent themes by embedding similarity. Call this
    FIRST when writing the digest: it does the mechanical grouping for you, so you reason over
    ~15 themes instead of hundreds of raw posts. Themes are not predefined, they emerge from this
    week's data (a week full of voice-agent posts yields a big voice-agents theme on its own),
    ordered biggest and strongest first.

    Returns {"clustered", "rated_in_window", "embedded", "theme_count", "themes"}. Each theme has
    theme_size (member count, the momentum signal), avg_relevance, topics (union across members),
    a representative post (handle, url, caption, the strongest member), and members (handle, url,
    content_hash for every post in the theme, so you can drill in without another call). Name each
    theme and judge its momentum from theme_size + avg_relevance + last week's memory; for the 2-3
    posts to highlight, use a theme's representative or its members.

    When "clustered" is false (no embeddings back the window, embedded == 0), fall back to
    get_rated_signals and group the flat list yourself as before.

    days: look-back window (default 7). min_relevance: floor 0-1 (default 0.5). max_themes: cap on
    themes returned (default 15).
    """
    # No dsn override: reads this process's DATABASE_URL, the tier's own database, like the other
    # tools. Shares common.clusters.get_signal_clusters with GET /signal-clusters, no drift.
    return _clusters(days=days, min_relevance=min_relevance, max_themes=max_themes)
