"""Request and response shapes for the API. Pydantic validates every payload at the edge,
so handlers only ever see well-formed data."""
from datetime import datetime
from typing import Any

from pydantic import BaseModel


class CompetitorIn(BaseModel):
    name: str
    domain: str | None = None


class Competitor(BaseModel):
    id: int
    name: str
    domain: str | None
    created_at: datetime


class SourceIn(BaseModel):
    competitor_id: int
    kind: str  # 'linkedin' | 'reddit' | 'changelog' | 'hackernews' | ...
    url: str


class Source(BaseModel):
    id: int
    competitor_id: int
    kind: str
    url: str
    created_at: datetime


class SignalIn(BaseModel):
    competitor_id: int
    captured_at: datetime  # the observation time; must fall inside an existing partition
    payload: dict[str, Any]
    source_id: int | None = None


class SignalInsertResult(BaseModel):
    inserted: bool  # False means the ON CONFLICT dedup already had this exact signal
    content_hash: str


class Signal(BaseModel):
    id: int
    competitor_id: int
    source_id: int | None
    captured_at: datetime
    content_hash: str
    payload: dict[str, Any]


class DailyRollup(BaseModel):
    competitor_id: int
    day: datetime
    signal_count: int
    source_count: int
