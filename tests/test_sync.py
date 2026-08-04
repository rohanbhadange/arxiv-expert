from dataclasses import replace
from datetime import timedelta

from test_database import sample_paper

from arxiv_expert.sync import SyncService


class FakeArxiv:
    def __init__(self, papers):
        self.papers = papers
        self.queries = []

    def papers_between(self, category, start, end):
        self.queries.append((category, start, end))
        return self.papers

    def download_pdf(self, url, target):
        target.write_bytes(b"%PDF-1.4 fake")


class FakeIndexer:
    def __init__(self, failures=0):
        self.failures = failures
        self.uploads = []

    def upload_pdf(self, expert, paper, pdf_path):
        self.uploads.append((expert.id, paper.arxiv_id, pdf_path.exists()))
        if self.failures:
            self.failures -= 1
            raise RuntimeError("temporary indexing failure")
        return f"file_{paper.arxiv_id}"


def test_sync_ingests_cross_list_once_and_ignores_revision(db, created_at):
    expert = db.create_expert("Robotics", "cs.RO", "vs_sync", created_at=created_at)
    first = sample_paper(created_at)
    fake_arxiv = FakeArxiv([first])
    backend = FakeIndexer()
    service = SyncService(db, fake_arxiv, backend)

    first_result = service.sync_expert(expert, now=created_at + timedelta(hours=1))
    refreshed = db.get_expert(expert.id)
    fake_arxiv.papers = [sample_paper(created_at, version=2)]
    second_result = service.sync_expert(refreshed, now=created_at + timedelta(days=1))

    assert first_result.discovered == first_result.ingested == 1
    assert second_result.discovered == second_result.ingested == 0
    assert len(backend.uploads) == 1
    assert db.list_papers(expert.id)[0].version == 1


def test_failed_ingestion_is_retried_on_next_sync(db, created_at):
    expert = db.create_expert("Robotics", "cs.RO", "vs_retry", created_at=created_at)
    fake_arxiv = FakeArxiv([sample_paper(created_at)])
    backend = FakeIndexer(failures=1)
    service = SyncService(db, fake_arxiv, backend)
    first = service.sync_expert(expert, now=created_at + timedelta(hours=1))
    refreshed = db.get_expert(expert.id)
    fake_arxiv.papers = []
    second = service.sync_expert(refreshed, now=created_at + timedelta(days=1))
    assert first.failed == 1
    assert second.retried == 1 and second.ingested == 1
    assert db.list_papers(expert.id)[0].status == "ready"


def test_creation_time_excludes_overlap_history(db, created_at):
    expert = db.create_expert("Robotics", "cs.RO", "vs_boundary", created_at=created_at)
    older = replace(sample_paper(created_at), published_at=created_at - timedelta(minutes=1))
    service = SyncService(db, FakeArxiv([older]), FakeIndexer())
    result = service.sync_expert(expert, now=created_at + timedelta(hours=1))
    assert result.discovered == 0


def test_concurrent_sync_is_skipped_without_uploading(db, created_at):
    expert = db.create_expert("Robotics", "cs.RO", "vs_locked", created_at=created_at)
    assert db.acquire_sync_lock(expert.id, acquired_at=created_at) is True
    backend = FakeIndexer()
    result = SyncService(db, FakeArxiv([sample_paper(created_at)]), backend).sync_expert(
        expert, now=created_at + timedelta(minutes=1)
    )
    assert result.skipped is True
    assert backend.uploads == []
    db.release_sync_lock(expert.id)
