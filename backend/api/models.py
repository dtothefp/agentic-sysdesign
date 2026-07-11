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
    status: str  # queued | running | completed | failed
    mode: str
    model: str | None = None  # rating model recorded on the run (data plane)
    total: int
    done_count: int
    inserted: int
    # Module 4 visibility: how many of this run's inserted signals have been rated so far.
    # inserted is the rating denominator; status flips to completed at scrape-done, so during
    # the rating drain a client sees status=completed with rated_count < inserted.
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
