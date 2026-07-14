"""Request and response shapes for the API. Pydantic validates every payload at the edge,
so handlers only ever see well-formed data."""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel


class InfluencerIn(BaseModel):
    name: str
    instagram_handle: str


class Influencer(BaseModel):
    id: int
    name: str
    instagram_handle: str
    last_scraped_at: datetime | None
    created_at: datetime


class InfluencerWatermark(BaseModel):
    # the scraper advances this after a run so the next run only pulls newer posts
    last_scraped_at: datetime


class SourceIn(BaseModel):
    influencer_id: int
    kind: str  # 'instagram' | 'tiktok' | 'youtube' | ...
    url: str


class Source(BaseModel):
    id: int
    influencer_id: int
    kind: str
    url: str
    created_at: datetime


class SignalIn(BaseModel):
    influencer_id: int
    captured_at: datetime  # the post's own publish time; must fall inside an existing partition
    payload: dict[str, Any]
    source_id: int | None = None


class SignalInsertResult(BaseModel):
    inserted: bool  # False means the ON CONFLICT dedup already had this exact signal
    content_hash: str


class Signal(BaseModel):
    id: int
    influencer_id: int
    source_id: int | None
    captured_at: datetime
    content_hash: str
    payload: dict[str, Any]


class DailyRollup(BaseModel):
    influencer_id: int
    day: datetime
    signal_count: int
    source_count: int


# --- Module 2: fan-out runs ----------------------------------------------------


class RunTrigger(BaseModel):
    # live = real Apify scrape; demo = synthetic signals, no Apify spend (watch the SSE bar)
    mode: Literal["live", "demo"] = "live"
    limit: int = 5  # posts (live) or synthetic signals (demo) per influencer
    # Module 4: which model rates this run's new signals, as "provider/model" (e.g.
    # "ollama/qwen3:4b", "deepseek/deepseek-chat"). Model selection is data, not deploy
    # config; None falls back to the worker's RATING_MODEL default (unset = no rating).
    model: str | None = None


class RunCreated(BaseModel):
    run_id: int
    total: int  # how many influencers this run fanned out to
    mode: str
    model: str | None = None


class Run(BaseModel):
    id: int
    # queued -> running -> (rating) -> completed | failed. `rating` is the phase after the
    # scrape chord fans in but before every signal has been rated; a run with no rating work
    # skips it and completes at fan-in. See stream_run for the matching SSE events.
    status: str
    mode: str
    model: str | None = None  # rating model recorded on the run (data plane)
    total: int
    done_count: int
    inserted: int
    # Module 4 visibility: how many of this run's inserted signals have been rated so far.
    # inserted is the rating denominator; the run sits in `rating` with rated_count < inserted
    # until the last rating lands, then flips to `completed`.
    rated_count: int = 0
    error: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


# --- Module 4: AI ratings -------------------------------------------------------


class Rating(BaseModel):
    content_hash: str  # joins to raw_signals.content_hash (dedup key = the model's INPUT)
    model: str
    relevance: float
    confidence: float
    topics: list[str]
    summary: str
    rated_at: datetime


# --- Module 5: agent-written digests ---------------------------------------------
#
# No trigger model here anymore. A digest run is started by triggering a Managed Agents
# deployment (packages/agents/deployment.yaml), not by POSTing a trigger body. The agent creates its
# own row (POST /digests, no body) and delivers into it (PUT /digests/{id}/content,
# DigestContent), so the only digest input the API still takes is the delivered markdown.


class DigestContent(BaseModel):
    content_md: str


# --- Module 6: hybrid search ------------------------------------------------------


class SearchHit(BaseModel):
    content_hash: str
    handle: str | None
    url: str | None
    caption: str | None  # excerpt (first 200 chars)
    captured_at: datetime
    # RRF fused score; higher is better. Not comparable to a ts_rank or a cosine distance, it's
    # a sum of 1/(k+rank) contributions, so it's only meaningful RELATIVE to the other hits.
    score: float
    # which retrieval halves surfaced this hit: ["lexical"], ["semantic"], or both. Both means
    # the two methods agreed, the strongest signal RRF can produce.
    sources: list[str]
    # the Module 4 rating, when this content has been rated (LEFT JOIN, so null until then).
    relevance: float | None = None
    summary: str | None = None
    topics: list[str] | None = None


class SearchResponse(BaseModel):
    query: str
    # False when EMBEDDING_MODEL is unset or embedding the query failed: results are lexical-only.
    # Surfaced so a caller knows the semantic half didn't run rather than silently getting half a
    # search. This is the inert-until-keyed contract made visible in the response.
    semantic: bool
    hits: list[SearchHit]


class ClusterMember(BaseModel):
    # one post inside a theme; enough to drill in (fetch the full row) without a second call.
    handle: str | None
    url: str | None
    content_hash: str
    relevance: float


class ClusterRepresentative(ClusterMember):
    # the theme's strongest post (highest relevance), carries a caption excerpt so the agent can
    # name the theme without pulling every member.
    caption: str | None = None


class SignalTheme(BaseModel):
    # an emergent theme: a group of the week's rated posts that sit close in embedding space.
    # theme_size (member count) is the momentum signal the agent judges on.
    theme_size: int
    avg_relevance: float
    topics: list[str]
    representative: ClusterRepresentative
    members: list[ClusterMember]


class SignalClustersResponse(BaseModel):
    days: int
    min_relevance: float
    # how many rated posts matched the window; `embedded` is how many of those had an embedding to
    # cluster on. A big gap means EMBEDDING_MODEL is off or backfill is incomplete.
    rated_in_window: int
    embedded: int
    # False when embedded == 0 (no embeddings back the window): the caller falls back to the flat
    # get_rated_signals list. Same inert-until-keyed contract as search's `semantic` flag.
    clustered: bool
    theme_count: int
    themes: list[SignalTheme]


class Digest(BaseModel):
    id: int
    # queued -> running -> completed | failed. `completed` is flipped by the AGENT's
    # PUT /digests/{id}/content, not by the worker; the worker only flips `failed` when
    # the session ends without that delivery.
    status: str
    session_id: str | None  # joins the row to the Anthropic Console trace
    content_md: str | None
    word_count: int | None
    error: str | None
    created_at: datetime
    completed_at: datetime | None
