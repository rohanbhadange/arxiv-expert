import os
import sqlite3
import stat
from datetime import timedelta

import pytest

from arxiv_expert.database import Database
from arxiv_expert.models import ArxivPaper, Citation


def sample_paper(created_at, *, arxiv_id="2608.00001", version=1):
    return ArxivPaper(
        arxiv_id=arxiv_id,
        version=version,
        title="Learning to Move",
        authors=("Ada Robot", "Rein Force"),
        summary="A grounded robotics result.",
        published_at=created_at + timedelta(minutes=5),
        updated_at=created_at + timedelta(minutes=5),
        categories=("cs.RO", "cs.LG"),
        primary_category="cs.RO",
        abstract_url=f"https://arxiv.org/abs/{arxiv_id}v{version}",
        pdf_url=f"https://arxiv.org/pdf/{arxiv_id}v{version}",
    )


def test_expert_paper_revision_dedup_and_chat_persist(db, created_at):
    expert = db.create_expert("Robotics", "cs.RO", "vs_123", created_at=created_at)
    paper, created = db.add_paper(expert.id, sample_paper(created_at))
    assert created is True
    revision, created = db.add_paper(expert.id, sample_paper(created_at, version=2))
    assert created is False
    assert revision.version == 1

    db.set_paper_status(paper.id, "ready", openai_file_id="file_123")
    conversation = db.list_conversations(expert.id)[0]
    citation = Citation("library", paper.title, paper.abstract_url, "L1")
    db.add_message(expert.id, conversation.id, "user", "What changed?", "Analyze")
    db.add_message(
        expert.id,
        conversation.id,
        "assistant",
        "It improved. [L1]",
        "Analyze",
        (citation,),
    )
    messages = db.list_messages(conversation.id)
    assert [item.role for item in messages] == ["user", "assistant"]
    assert messages[-1].citations == (citation,)
    assert db.paper_counts(expert.id)["ready"] == 1
    if os.name != "nt":
        assert stat.S_IMODE(db.path.stat().st_mode) == 0o600


def test_conversations_are_isolated_persistent_and_keep_global_papers(db, created_at):
    expert = db.create_expert("Robotics", "cs.RO", "vs_conversations", created_at=created_at)
    first = db.list_conversations(expert.id)[0]
    second = db.create_conversation(expert.id, "Second line of inquiry")
    paper, _ = db.add_paper(expert.id, sample_paper(created_at), origin="manual")

    assert db.focus_paper(first.id, paper.id) is True
    assert db.focus_paper(first.id, paper.id) is False
    db.add_message(expert.id, first.id, "user", "First question", "Analyze")
    db.add_message(expert.id, second.id, "user", "Second question", "Analyze")

    assert [item.content for item in db.list_messages(first.id)] == ["First question"]
    assert [item.content for item in db.list_messages(second.id)] == ["Second question"]
    assert db.list_conversation_papers(first.id)[0].origin == "manual"
    assert db.list_conversation_papers(second.id) == []

    db.delete_conversation(first.id)
    assert db.get_paper(paper.id) is not None
    assert db.get_conversation(second.id).title == "Second line of inquiry"


def test_focus_is_limited_to_eight_papers(db, created_at):
    expert = db.create_expert("Robotics", "cs.RO", "vs_focus_limit", created_at=created_at)
    conversation = db.list_conversations(expert.id)[0]
    for index in range(8):
        paper, _ = db.add_paper(
            expert.id,
            sample_paper(created_at, arxiv_id=f"2608.{index + 1:05d}"),
        )
        db.focus_paper(conversation.id, paper.id)
    extra, _ = db.add_paper(expert.id, sample_paper(created_at, arxiv_id="2608.99999"))
    with pytest.raises(ValueError, match="at most 8"):
        db.focus_paper(conversation.id, extra.id)


LEGACY_SCHEMA = """
CREATE TABLE experts (
 id TEXT PRIMARY KEY, name TEXT UNIQUE, category TEXT, created_at TEXT,
 vector_store_id TEXT UNIQUE, last_sync_at TEXT
);
CREATE TABLE papers (
 id INTEGER PRIMARY KEY AUTOINCREMENT, expert_id TEXT, arxiv_id TEXT, version INTEGER,
 title TEXT, authors_json TEXT, summary TEXT, published_at TEXT, updated_at TEXT,
 categories_json TEXT, primary_category TEXT, abstract_url TEXT, pdf_url TEXT,
 status TEXT, openai_file_id TEXT, error TEXT, discovered_at TEXT,
 UNIQUE(expert_id, arxiv_id)
);
CREATE TABLE messages (
 id INTEGER PRIMARY KEY AUTOINCREMENT, expert_id TEXT, role TEXT, content TEXT,
 mode TEXT, citations_json TEXT DEFAULT '[]', created_at TEXT
);
CREATE TABLE sync_runs (
 id INTEGER PRIMARY KEY AUTOINCREMENT, expert_id TEXT, started_at TEXT,
 completed_at TEXT, discovered INTEGER DEFAULT 0, ingested INTEGER DEFAULT 0,
 failed INTEGER DEFAULT 0, error TEXT
);
CREATE TABLE sync_locks (expert_id TEXT PRIMARY KEY, acquired_at TEXT);
"""


def test_legacy_migration_preserves_39_papers_messages_and_citations(tmp_path, created_at):
    path = tmp_path / "legacy.sqlite3"
    timestamp = created_at.isoformat()
    citation = Citation("library", "Legacy paper", "https://arxiv.org/abs/2608.00001", "L1")
    with sqlite3.connect(path) as connection:
        connection.executescript(LEGACY_SCHEMA)
        connection.execute(
            "INSERT INTO experts VALUES (?, ?, ?, ?, ?, ?)",
            ("expert-1", "Robotics", "cs.RO", timestamp, "vs_existing", timestamp),
        )
        for index in range(39):
            arxiv_id = f"2608.{index + 1:05d}"
            connection.execute(
                """
                INSERT INTO papers(
                    expert_id, arxiv_id, version, title, authors_json, summary,
                    published_at, updated_at, categories_json, primary_category,
                    abstract_url, pdf_url, status, openai_file_id, error, discovered_at
                ) VALUES (?, ?, 1, ?, '["Ada"]', 'Summary', ?, ?, '["cs.RO"]',
                          'cs.RO', ?, ?, 'ready', ?, NULL, ?)
                """,
                (
                    "expert-1",
                    arxiv_id,
                    f"Paper {index + 1}",
                    timestamp,
                    timestamp,
                    f"https://arxiv.org/abs/{arxiv_id}",
                    f"https://arxiv.org/pdf/{arxiv_id}",
                    f"file-{index + 1}",
                    timestamp,
                ),
            )
        connection.execute(
            "INSERT INTO messages(expert_id, role, content, mode, citations_json, created_at) "
            "VALUES (?, 'user', 'How should robots learn from demonstrations?', 'Analyze', '[]', ?)",
            ("expert-1", timestamp),
        )
        connection.execute(
            "INSERT INTO messages(expert_id, role, content, mode, citations_json, created_at) "
            "VALUES (?, 'assistant', 'Legacy answer', 'Analyze', ?, ?)",
            (
                "expert-1",
                '[{"kind":"library","title":"Legacy paper","url":"https://arxiv.org/abs/2608.00001","marker":"L1"}]',
                timestamp,
            ),
        )

    migrated = Database(path)
    migrated.initialize()

    papers = migrated.list_papers("expert-1")
    conversations = migrated.list_conversations("expert-1")
    messages = migrated.list_messages(conversations[0].id)
    assert len(papers) == 39
    assert {paper.origin for paper in papers} == {"topic"}
    assert conversations[0].title == "How should robots learn from demonstrations"
    assert [message.content for message in messages] == [
        "How should robots learn from demonstrations?",
        "Legacy answer",
    ]
    assert messages[1].citations == (citation,)
    with migrated.connect() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 3
