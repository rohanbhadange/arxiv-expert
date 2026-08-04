"""Domain models shared across ingestion, chat, persistence, and UI."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True, slots=True)
class Expert:
    id: str
    name: str
    category: str
    created_at: datetime
    vector_store_id: str
    last_sync_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class ArxivPaper:
    arxiv_id: str
    version: int
    title: str
    authors: tuple[str, ...]
    summary: str
    published_at: datetime
    updated_at: datetime
    categories: tuple[str, ...]
    primary_category: str
    abstract_url: str
    pdf_url: str


@dataclass(frozen=True, slots=True)
class Paper:
    id: int
    expert_id: str
    arxiv_id: str
    version: int
    title: str
    authors: tuple[str, ...]
    summary: str
    published_at: datetime
    updated_at: datetime
    categories: tuple[str, ...]
    primary_category: str
    abstract_url: str
    pdf_url: str
    status: str
    openai_file_id: str | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class Citation:
    kind: str
    title: str
    url: str
    marker: str


@dataclass(frozen=True, slots=True)
class ChatAnswer:
    text: str
    citations: tuple[Citation, ...] = field(default_factory=tuple)
    used_web: bool = False


@dataclass(frozen=True, slots=True)
class LibraryHit:
    file_id: str
    filename: str
    score: float
    text: str


@dataclass(frozen=True, slots=True)
class Message:
    id: int
    expert_id: str
    role: str
    content: str
    mode: str
    citations: tuple[Citation, ...]
    created_at: datetime


@dataclass(frozen=True, slots=True)
class SyncResult:
    expert_id: str
    discovered: int
    ingested: int
    failed: int
    retried: int
    skipped: bool = False
