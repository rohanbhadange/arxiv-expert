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

from .conversation_titles import derive_conversation_title
from .models import ArxivPaper, Citation, Conversation, Expert, Message, Paper

SCHEMA_VERSION = 3
NEW_CHAT_TITLE = "New chat"
LEGACY_CHAT_TITLE = "Previous chat"

SCHEMA = """

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
    origin TEXT NOT NULL DEFAULT 'topic' CHECK(origin IN ('topic', 'manual')),
    openai_file_id TEXT,
    error TEXT,
    discovered_at TEXT NOT NULL,
    UNIQUE(expert_id, arxiv_id)
);

CREATE INDEX IF NOT EXISTS papers_expert_status_idx ON papers(expert_id, status);

CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,
    expert_id TEXT NOT NULL REFERENCES experts(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS conversations_expert_updated_idx
    ON conversations(expert_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS conversation_papers (
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    paper_id INTEGER NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    added_at TEXT NOT NULL,
    PRIMARY KEY(conversation_id, paper_id)
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    expert_id TEXT NOT NULL REFERENCES experts(id) ON DELETE CASCADE,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK(role IN ('user', 'assistant')),
    content TEXT NOT NULL,
    mode TEXT NOT NULL,
    citations_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS messages_conversation_idx ON messages(conversation_id, id);

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
            connection.execute("PRAGMA journal_mode = WAL")
            tables = {
                row["name"]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
            if "experts" not in tables:
                connection.executescript(SCHEMA)
                connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            else:
                self._migrate(connection)
        if os.name != "nt":
            self.path.chmod(stat.S_IRUSR | stat.S_IWUSR)

    def _migrate(self, connection: sqlite3.Connection) -> None:
        """Upgrade the original schema without losing papers or chat history."""
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        paper_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(papers)").fetchall()
        }
        message_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(messages)").fetchall()
        }
        if (
            version >= SCHEMA_VERSION
            and {"origin"} <= paper_columns
            and "conversation_id" in message_columns
        ):
            connection.executescript(SCHEMA)
            self._ensure_expert_conversations(connection)
            return

        connection.execute("BEGIN IMMEDIATE")
        try:
            if "origin" not in paper_columns:
                connection.execute(
                    "ALTER TABLE papers ADD COLUMN origin TEXT NOT NULL DEFAULT 'topic' "
                    "CHECK(origin IN ('topic', 'manual'))"
                )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS conversations (
                    id TEXT PRIMARY KEY,
                    expert_id TEXT NOT NULL REFERENCES experts(id) ON DELETE CASCADE,
                    title TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS conversation_papers (
                    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                    paper_id INTEGER NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
                    added_at TEXT NOT NULL,
                    PRIMARY KEY(conversation_id, paper_id)
                )
                """
            )
            conversation_ids = self._ensure_expert_conversations(connection, legacy=True)
            if "conversation_id" not in message_columns:
                connection.execute(
                    """
                    CREATE TABLE messages_v2 (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        expert_id TEXT NOT NULL REFERENCES experts(id) ON DELETE CASCADE,
                        conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                        role TEXT NOT NULL CHECK(role IN ('user', 'assistant')),
                        content TEXT NOT NULL,
                        mode TEXT NOT NULL,
                        citations_json TEXT NOT NULL DEFAULT '[]',
                        created_at TEXT NOT NULL
                    )
                    """
                )
                for expert_id, conversation_id in conversation_ids.items():
                    connection.execute(
                        """
                        INSERT INTO messages_v2(
                            id, expert_id, conversation_id, role, content, mode,
                            citations_json, created_at
                        )
                        SELECT id, expert_id, ?, role, content, mode, citations_json, created_at
                        FROM messages WHERE expert_id = ? ORDER BY id
                        """,
                        (conversation_id, expert_id),
                    )
                connection.execute("DROP TABLE messages")
                connection.execute("ALTER TABLE messages_v2 RENAME TO messages")
            connection.execute("DROP INDEX IF EXISTS messages_expert_idx")
            connection.execute(
                "CREATE INDEX IF NOT EXISTS messages_conversation_idx "
                "ON messages(conversation_id, id)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS conversations_expert_updated_idx "
                "ON conversations(expert_id, updated_at DESC)"
            )
            self._replace_legacy_conversation_titles(connection)
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise

    @staticmethod
    def _ensure_expert_conversations(
        connection: sqlite3.Connection, *, legacy: bool = False
    ) -> dict[str, str]:
        result: dict[str, str] = {}
        experts = connection.execute("SELECT id, created_at FROM experts").fetchall()
        for expert in experts:
            existing = connection.execute(
                "SELECT id FROM conversations WHERE expert_id = ? ORDER BY created_at LIMIT 1",
                (expert["id"],),
            ).fetchone()
            if existing:
                result[expert["id"]] = existing["id"]
                continue
            last_message = (
                connection.execute(
                    "SELECT MAX(created_at) AS created_at FROM messages WHERE expert_id = ?",
                    (expert["id"],),
                ).fetchone()["created_at"]
                if legacy
                else None
            )
            has_messages = bool(last_message)
            conversation_id = str(uuid.uuid4())
            created_at = expert["created_at"]
            connection.execute(
                "INSERT INTO conversations(id, expert_id, title, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    conversation_id,
                    expert["id"],
                    LEGACY_CHAT_TITLE if has_messages else NEW_CHAT_TITLE,
                    created_at,
                    last_message or created_at,
                ),
            )
            result[expert["id"]] = conversation_id
        return result

    @staticmethod
    def _replace_legacy_conversation_titles(connection: sqlite3.Connection) -> None:
        """Give migrated chat history a useful title without discarding it."""
        legacy_conversations = connection.execute(
            "SELECT id FROM conversations WHERE title = ?", (LEGACY_CHAT_TITLE,)
        ).fetchall()
        for conversation in legacy_conversations:
            first_message = connection.execute(
                "SELECT content FROM messages WHERE conversation_id = ? "
                "ORDER BY CASE role WHEN 'user' THEN 0 ELSE 1 END, id LIMIT 1",
                (conversation["id"],),
            ).fetchone()
            title = (
                derive_conversation_title(first_message["content"])
                if first_message
                else NEW_CHAT_TITLE
            )
            connection.execute(
                "UPDATE conversations SET title = ? WHERE id = ?",
                (title, conversation["id"]),
            )

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
            now = _iso(expert.created_at)
            connection.execute(
                "INSERT INTO conversations(id, expert_id, title, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (str(uuid.uuid4()), expert.id, NEW_CHAT_TITLE, now, now),
            )
        return expert

    def create_conversation(self, expert_id: str, title: str = NEW_CHAT_TITLE) -> Conversation:
        clean_title = title.strip() or NEW_CHAT_TITLE
        now = utc_now()
        conversation = Conversation(str(uuid.uuid4()), expert_id, clean_title, now, now)
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO conversations(id, expert_id, title, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    conversation.id,
                    expert_id,
                    conversation.title,
                    _iso(now),
                    _iso(now),
                ),
            )
        return conversation

    def list_conversations(self, expert_id: str) -> list[Conversation]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM conversations WHERE expert_id = ? "
                "ORDER BY updated_at DESC, created_at DESC",
                (expert_id,),
            ).fetchall()
        return [self._conversation(row) for row in rows]

    def get_conversation(self, conversation_id: str) -> Conversation | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM conversations WHERE id = ?", (conversation_id,)
            ).fetchone()
        return self._conversation(row) if row else None

    def rename_conversation(self, conversation_id: str, title: str) -> None:
        clean_title = " ".join(title.split())
        if not clean_title:
            raise ValueError("Conversation title cannot be empty.")
        with self.connect() as connection:
            connection.execute(
                "UPDATE conversations SET title = ?, updated_at = ? WHERE id = ?",
                (clean_title[:80], _iso(utc_now()), conversation_id),
            )

    def title_new_conversation(self, conversation_id: str, title: str) -> None:
        clean_title = " ".join(title.split())[:80]
        if not clean_title:
            return
        with self.connect() as connection:
            connection.execute(
                "UPDATE conversations SET title = ?, updated_at = ? WHERE id = ? AND title = ?",
                (clean_title, _iso(utc_now()), conversation_id, NEW_CHAT_TITLE),
            )

    def delete_conversation(self, conversation_id: str) -> None:
        with self.connect() as connection:
            connection.execute("DELETE FROM conversations WHERE id = ?", (conversation_id,))

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

    def add_paper(
        self, expert_id: str, paper: ArxivPaper, *, origin: str = "topic"
    ) -> tuple[Paper, bool]:
        if origin not in {"topic", "manual"}:
            raise ValueError("Paper origin must be 'topic' or 'manual'.")
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO papers(
                    expert_id, arxiv_id, version, title, authors_json, summary,
                    published_at, updated_at, categories_json, primary_category,
                    abstract_url, pdf_url, status, origin, discovered_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
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
                    origin,
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

    def search_papers(self, expert_id: str, query: str, *, limit: int = 5) -> list[Paper]:
        clean = query.strip().strip('"').strip("'")
        if not clean:
            return []
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM papers
                WHERE expert_id = ? AND (
                    lower(arxiv_id) = lower(?) OR lower(title) = lower(?) OR lower(title) LIKE lower(?)
                )
                ORDER BY
                    CASE WHEN lower(arxiv_id) = lower(?) THEN 0
                         WHEN lower(title) = lower(?) THEN 1 ELSE 2 END,
                    published_at DESC
                LIMIT ?
                """,
                (expert_id, clean, clean, f"%{clean}%", clean, clean, limit),
            ).fetchall()
        return [self._paper(row) for row in rows]

    def list_conversation_papers(self, conversation_id: str) -> list[Paper]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT papers.* FROM conversation_papers
                JOIN papers ON papers.id = conversation_papers.paper_id
                WHERE conversation_papers.conversation_id = ?
                ORDER BY conversation_papers.added_at, papers.id
                """,
                (conversation_id,),
            ).fetchall()
        return [self._paper(row) for row in rows]

    def focus_paper(self, conversation_id: str, paper_id: int, *, limit: int = 8) -> bool:
        with self.connect() as connection:
            conversation = connection.execute(
                "SELECT expert_id FROM conversations WHERE id = ?", (conversation_id,)
            ).fetchone()
            paper = connection.execute(
                "SELECT expert_id FROM papers WHERE id = ?", (paper_id,)
            ).fetchone()
            if not conversation or not paper or conversation["expert_id"] != paper["expert_id"]:
                raise ValueError("The paper and conversation must belong to the same expert.")
            existing = connection.execute(
                "SELECT 1 FROM conversation_papers WHERE conversation_id = ? AND paper_id = ?",
                (conversation_id, paper_id),
            ).fetchone()
            if existing:
                return False
            count = connection.execute(
                "SELECT COUNT(*) FROM conversation_papers WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()[0]
            if count >= limit:
                raise ValueError(f"A chat can focus on at most {limit} papers.")
            now = _iso(utc_now())
            connection.execute(
                "INSERT INTO conversation_papers(conversation_id, paper_id, added_at) "
                "VALUES (?, ?, ?)",
                (conversation_id, paper_id, now),
            )
            connection.execute(
                "UPDATE conversations SET updated_at = ? WHERE id = ?", (now, conversation_id)
            )
        return True

    def unfocus_paper(self, conversation_id: str, paper_id: int) -> bool:
        with self.connect() as connection:
            cursor = connection.execute(
                "DELETE FROM conversation_papers WHERE conversation_id = ? AND paper_id = ?",
                (conversation_id, paper_id),
            )
            if cursor.rowcount:
                connection.execute(
                    "UPDATE conversations SET updated_at = ? WHERE id = ?",
                    (_iso(utc_now()), conversation_id),
                )
        return cursor.rowcount == 1

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
        conversation_id: str,
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
            owner = connection.execute(
                "SELECT expert_id FROM conversations WHERE id = ?", (conversation_id,)
            ).fetchone()
            if not owner or owner["expert_id"] != expert_id:
                raise ValueError("The conversation does not belong to this expert.")
            cursor = connection.execute(
                "INSERT INTO messages(expert_id, conversation_id, role, content, mode, "
                "citations_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    expert_id,
                    conversation_id,
                    role,
                    content,
                    mode,
                    citations_json,
                    _iso(created_at),
                ),
            )
            message_id = int(cursor.lastrowid)
            connection.execute(
                "UPDATE conversations SET updated_at = ? WHERE id = ?",
                (_iso(created_at), conversation_id),
            )
        return Message(
            message_id,
            expert_id,
            conversation_id,
            role,
            content,
            mode,
            tuple(citations),
            created_at,
        )

    def list_messages(self, conversation_id: str, limit: int = 30) -> list[Message]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM (
                    SELECT * FROM messages WHERE conversation_id = ? ORDER BY id DESC LIMIT ?
                ) ORDER BY id
                """,
                (conversation_id, limit),
            ).fetchall()
        return [self._message(row) for row in rows]

    def clear_messages(self, conversation_id: str) -> None:
        with self.connect() as connection:
            connection.execute("DELETE FROM messages WHERE conversation_id = ?", (conversation_id,))

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
    def _conversation(row: sqlite3.Row) -> Conversation:
        return Conversation(
            id=row["id"],
            expert_id=row["expert_id"],
            title=row["title"],
            created_at=_dt(row["created_at"]),
            updated_at=_dt(row["updated_at"]),
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
            origin=row["origin"],
            openai_file_id=row["openai_file_id"],
            error=row["error"],
        )

    @staticmethod
    def _message(row: sqlite3.Row) -> Message:
        citations = tuple(Citation(**item) for item in json.loads(row["citations_json"]))
        return Message(
            id=row["id"],
            expert_id=row["expert_id"],
            conversation_id=row["conversation_id"],
            role=row["role"],
            content=row["content"],
            mode=row["mode"],
            citations=citations,
            created_at=_dt(row["created_at"]),
        )
