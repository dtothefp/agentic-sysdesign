"""Module 5: the digest agent's tool surface, as a remote MCP server.

This replaces the custom tool `get_rated_signals` that the Celery worker used to answer
host-side while babysitting the session (worker/tasks.py, now retired). The migration is the
Module 5 lesson made concrete: a custom tool is bound to whatever process holds the session's
event stream, so it can't survive an unattended deployment. An MCP tool has no such tether. The
agent dials this server directly through Anthropic's MCP proxy, so nothing has to sit on the
stream waiting to answer.

Co-mounted on the FastAPI app at `/mcp` (see api/main.py), so it shares the app's process,
its connection target, and its lifespan. That co-mount is deliberate: the tool delegates to
the same `common.digests.get_rated_signals` the worker's custom tool called, reading whatever
DATABASE_URL this process was started with. So the environment routing is automatic, the local
API reads the local (or tunnelled) DB, a preview API reads the preview DB, prod reads prod, with
zero per-environment MCP config. The schema for the join lives in exactly one place, still
common/digests.py, so "two things know the schema" never happens.

Transport is Streamable HTTP (what the Managed Agents MCP proxy speaks). DNS-rebinding
protection is off because the Host varies across tiers (localhost, the Cloudflare tunnel
hostname, per-PR *.up.railway.app, the prod domain) and we enforce our own bearer auth on the
`/mcp` path instead (api/main.py, the same inert-until-keyed SYSDESIGN_API_KEY contract as the
REST routes). The vault injects that bearer at egress via a static_bearer credential keyed to
this server's URL (m5_agents/apply.sh); the sandbox never sees the token.
"""
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from common.digests import get_rated_signals as _query

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
