import os
import stat
from datetime import timedelta

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
    citation = Citation("library", paper.title, paper.abstract_url, "L1")
    db.add_message(expert.id, "user", "What changed?", "Analyze")
    db.add_message(expert.id, "assistant", "It improved. [L1]", "Analyze", (citation,))
    messages = db.list_messages(expert.id)
    assert [item.role for item in messages] == ["user", "assistant"]
    assert messages[-1].citations == (citation,)
    assert db.paper_counts(expert.id)["ready"] == 1
    if os.name != "nt":
        assert stat.S_IMODE(db.path.stat().st_mode) == 0o600
