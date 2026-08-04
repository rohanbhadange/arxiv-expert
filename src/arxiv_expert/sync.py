"""Incremental, retryable ingestion for all configured experts."""

from __future__ import annotations

import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Protocol

from .arxiv import ArxivClient
from .database import Database, utc_now
from .models import Expert, Paper, SyncResult


class IndexBackend(Protocol):
    def upload_pdf(self, expert: Expert, paper: Paper, pdf_path: Path) -> str: ...


class SyncService:
    OVERLAP = timedelta(hours=48)

    def __init__(self, database: Database, arxiv: ArxivClient, backend: IndexBackend):
        self.database = database
        self.arxiv = arxiv
        self.backend = backend

    def sync_all(self, *, now: datetime | None = None) -> list[SyncResult]:
        boundary = now or utc_now()
        return [self.sync_expert(expert, now=boundary) for expert in self.database.list_experts()]

    def sync_expert(self, expert: Expert, *, now: datetime | None = None) -> SyncResult:
        boundary = (now or utc_now()).astimezone(timezone.utc)
        if not self.database.acquire_sync_lock(expert.id, acquired_at=boundary):
            return SyncResult(expert.id, 0, 0, 0, 0, skipped=True)
        try:
            return self._sync_expert_locked(expert, boundary)
        finally:
            self.database.release_sync_lock(expert.id)

    def _sync_expert_locked(self, expert: Expert, boundary: datetime) -> SyncResult:
        retried = 0
        ingested = 0
        failed = 0

        for paper in self.database.list_papers(expert.id, ("failed", "pending", "indexing")):
            retried += 1
            if self._ingest(expert, paper):
                ingested += 1
            else:
                failed += 1

        if expert.last_sync_at:
            query_start = max(expert.created_at, expert.last_sync_at - self.OVERLAP)
        else:
            query_start = expert.created_at
        discovered = 0
        papers = self.arxiv.papers_between(expert.category, query_start, boundary)
        for candidate in papers:
            # This exact boundary prevents the overlap from becoming a historical seed.
            if candidate.published_at < expert.created_at:
                continue
            paper, created = self.database.add_paper(expert.id, candidate)
            if not created:
                continue
            discovered += 1
            if self._ingest(expert, paper):
                ingested += 1
            else:
                failed += 1
        self.database.set_last_sync(expert.id, boundary)
        return SyncResult(expert.id, discovered, ingested, failed, retried)

    def _ingest(self, expert: Expert, paper: Paper) -> bool:
        self.database.set_paper_status(paper.id, "indexing", error=None)
        try:
            with tempfile.TemporaryDirectory(prefix="arxiv-expert-") as directory:
                safe_id = paper.arxiv_id.replace("/", "_")
                target = Path(directory) / f"{safe_id}v{paper.version}.pdf"
                self.arxiv.download_pdf(paper.pdf_url, target)
                file_id = self.backend.upload_pdf(expert, paper, target)
            self.database.set_paper_status(paper.id, "ready", openai_file_id=file_id, error=None)
            return True
        except Exception as exc:
            self.database.set_paper_status(paper.id, "failed", error=str(exc)[:1000])
            return False
