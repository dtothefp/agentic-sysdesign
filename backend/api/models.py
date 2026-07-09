"""Request and response shapes for the API. Pydantic validates every payload at the edge,
so handlers only ever see well-formed data."""
from datetime import datetime
from typing import Any

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
