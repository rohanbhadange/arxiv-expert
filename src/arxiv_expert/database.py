"""SQLite persistence for experts, papers, synchronization, and chat."""

from __future__ import annotations

import json
import os
import sqlite3
import stat
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .models import ArxivPaper, Citation, Expert, Message, Paper

SCHEMA = """
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS experts (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    category TEXT NOT NULL,
    created_at TEXT NOT NULL,
    vector_store_id TEXT NOT NULL UNIQUE,
    last_sync_at TEXT
);

CREATE TABLE IF NOT EXISTS papers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    expert_id TEXT NOT NULL REFERENCES experts(id) ON DELETE CASCADE,
    arxiv_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    title TEXT NOT NULL,
    authors_json TEXT NOT NULL,
    summary TEXT NOT NULL,
    published_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    categories_json TEXT NOT NULL,
    primary_category TEXT NOT NULL,
    abstract_url TEXT NOT NULL,
    pdf_url TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending', 'indexing', 'ready', 'failed')),
    openai_file_id TEXT,
    error TEXT,
    discovered_at TEXT NOT NULL,
    UNIQUE(expert_id, arxiv_id)
);

CREATE INDEX IF NOT EXISTS papers_expert_status_idx ON papers(expert_id, status);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    expert_id TEXT NOT NULL REFERENCES experts(id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK(role IN ('user', 'assistant')),
    content TEXT NOT NULL,
    mode TEXT NOT NULL,
    citations_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS messages_expert_idx ON messages(expert_id, id);

CREATE TABLE IF NOT EXISTS sync_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    expert_id TEXT NOT NULL REFERENCES experts(id) ON DELETE CASCADE,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    discovered INTEGER NOT NULL DEFAULT 0,
    ingested INTEGER NOT NULL DEFAULT 0,
    failed INTEGER NOT NULL DEFAULT 0,
    error TEXT
);

CREATE TABLE IF NOT EXISTS sync_locks (
    expert_id TEXT PRIMARY KEY REFERENCES experts(id) ON DELETE CASCADE,
    acquired_at TEXT NOT NULL
);
"""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


class Database:
    def __init__(self, path: Path | str):
        self.path = Path(path)

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.executescript(SCHEMA)
        if os.name != "nt":
            self.path.chmod(stat.S_IRUSR | stat.S_IWUSR)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def create_expert(
        self,
        name: str,
        category: str,
        vector_store_id: str,
        *,
        created_at: datetime | None = None,
    ) -> Expert:
        expert = Expert(
            id=str(uuid.uuid4()),
            name=name.strip(),
            category=category,
            created_at=created_at or utc_now(),
            vector_store_id=vector_store_id,
        )
        if not expert.name:
            raise ValueError("Expert name cannot be empty.")
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO experts(id, name, category, created_at, vector_store_id) VALUES (?, ?, ?, ?, ?)",
                (
                    expert.id,
                    expert.name,
                    expert.category,
                    _iso(expert.created_at),
                    expert.vector_store_id,
                ),
            )
        return expert

    def list_experts(self) -> list[Expert]:
        with self.connect() as connection:
            rows = connection.execute("SELECT * FROM experts ORDER BY created_at, name").fetchall()
        return [self._expert(row) for row in rows]

    def get_expert(self, expert_id: str) -> Expert | None:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM experts WHERE id = ?", (expert_id,)).fetchone()
        return self._expert(row) if row else None

    def delete_expert(self, expert_id: str) -> None:
        with self.connect() as connection:
            connection.execute("DELETE FROM experts WHERE id = ?", (expert_id,))

    def set_last_sync(self, expert_id: str, completed_at: datetime) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE experts SET last_sync_at = ? WHERE id = ?", (_iso(completed_at), expert_id)
            )

    def acquire_sync_lock(
        self,
        expert_id: str,
        *,
        acquired_at: datetime | None = None,
        stale_after: timedelta = timedelta(hours=4),
    ) -> bool:
        now = acquired_at or utc_now()
        cutoff = now - stale_after
        with self.connect() as connection:
            connection.execute("DELETE FROM sync_locks WHERE acquired_at < ?", (_iso(cutoff),))
            cursor = connection.execute(
                "INSERT OR IGNORE INTO sync_locks(expert_id, acquired_at) VALUES (?, ?)",
                (expert_id, _iso(now)),
            )
        return cursor.rowcount == 1

    def release_sync_lock(self, expert_id: str) -> None:
        with self.connect() as connection:
            connection.execute("DELETE FROM sync_locks WHERE expert_id = ?", (expert_id,))

    def add_paper(self, expert_id: str, paper: ArxivPaper) -> tuple[Paper, bool]:
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO papers(
                    expert_id, arxiv_id, version, title, authors_json, summary,
                    published_at, updated_at, categories_json, primary_category,
                    abstract_url, pdf_url, status, discovered_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
                """,
                (
                    expert_id,
                    paper.arxiv_id,
                    paper.version,
                    paper.title,
                    json.dumps(paper.authors),
                    paper.summary,
                    _iso(paper.published_at),
                    _iso(paper.updated_at),
                    json.dumps(paper.categories),
                    paper.primary_category,
                    paper.abstract_url,
                    paper.pdf_url,
                    _iso(utc_now()),
                ),
            )
            created = cursor.rowcount == 1
            row = connection.execute(
                "SELECT * FROM papers WHERE expert_id = ? AND arxiv_id = ?",
                (expert_id, paper.arxiv_id),
            ).fetchone()
        return self._paper(row), created

    def get_paper(self, paper_id: int) -> Paper | None:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM papers WHERE id = ?", (paper_id,)).fetchone()
        return self._paper(row) if row else None

    def find_paper(self, expert_id: str, arxiv_id: str) -> Paper | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM papers WHERE expert_id = ? AND arxiv_id = ?", (expert_id, arxiv_id)
            ).fetchone()
        return self._paper(row) if row else None

    def list_papers(self, expert_id: str, statuses: Sequence[str] | None = None) -> list[Paper]:
        parameters: list[object] = [expert_id]
        clause = ""
        if statuses:
            clause = f" AND status IN ({','.join('?' for _ in statuses)})"
            parameters.extend(statuses)
        with self.connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM papers WHERE expert_id = ?{clause} ORDER BY published_at DESC, id DESC",
                parameters,
            ).fetchall()
        return [self._paper(row) for row in rows]

    def set_paper_status(
        self,
        paper_id: int,
        status: str,
        *,
        openai_file_id: str | None = None,
        error: str | None = None,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE papers SET status = ?, openai_file_id = COALESCE(?, openai_file_id), error = ? WHERE id = ?",
                (status, openai_file_id, error, paper_id),
            )

    def paper_counts(self, expert_id: str) -> dict[str, int]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT status, COUNT(*) AS count FROM papers WHERE expert_id = ? GROUP BY status",
                (expert_id,),
            ).fetchall()
        counts = {"pending": 0, "indexing": 0, "ready": 0, "failed": 0}
        counts.update({row["status"]: row["count"] for row in rows})
        return counts

    def add_message(
        self,
        expert_id: str,
        role: str,
        content: str,
        mode: str,
        citations: Sequence[Citation] = (),
    ) -> Message:
        created_at = utc_now()
        citations_json = json.dumps(
            [
                {"kind": item.kind, "title": item.title, "url": item.url, "marker": item.marker}
                for item in citations
            ]
        )
        with self.connect() as connection:
            cursor = connection.execute(
                "INSERT INTO messages(expert_id, role, content, mode, citations_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (expert_id, role, content, mode, citations_json, _iso(created_at)),
            )
            message_id = int(cursor.lastrowid)
        return Message(message_id, expert_id, role, content, mode, tuple(citations), created_at)

    def list_messages(self, expert_id: str, limit: int = 30) -> list[Message]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM (
                    SELECT * FROM messages WHERE expert_id = ? ORDER BY id DESC LIMIT ?
                ) ORDER BY id
                """,
                (expert_id, limit),
            ).fetchall()
        return [self._message(row) for row in rows]

    def clear_messages(self, expert_id: str) -> None:
        with self.connect() as connection:
            connection.execute("DELETE FROM messages WHERE expert_id = ?", (expert_id,))

    @staticmethod
    def _expert(row: sqlite3.Row) -> Expert:
        return Expert(
            id=row["id"],
            name=row["name"],
            category=row["category"],
            created_at=_dt(row["created_at"]),
            vector_store_id=row["vector_store_id"],
            last_sync_at=_dt(row["last_sync_at"]),
        )

    @staticmethod
    def _paper(row: sqlite3.Row) -> Paper:
        return Paper(
            id=row["id"],
            expert_id=row["expert_id"],
            arxiv_id=row["arxiv_id"],
            version=row["version"],
            title=row["title"],
            authors=tuple(json.loads(row["authors_json"])),
            summary=row["summary"],
            published_at=_dt(row["published_at"]),
            updated_at=_dt(row["updated_at"]),
            categories=tuple(json.loads(row["categories_json"])),
            primary_category=row["primary_category"],
            abstract_url=row["abstract_url"],
            pdf_url=row["pdf_url"],
            status=row["status"],
            openai_file_id=row["openai_file_id"],
            error=row["error"],
        )

    @staticmethod
    def _message(row: sqlite3.Row) -> Message:
        citations = tuple(Citation(**item) for item in json.loads(row["citations_json"]))
        return Message(
            id=row["id"],
            expert_id=row["expert_id"],
            role=row["role"],
            content=row["content"],
            mode=row["mode"],
            citations=citations,
            created_at=_dt(row["created_at"]),
        )
