from test_database import sample_paper

from arxiv_expert.paper_import import (
    PaperCommandService,
    PaperImportService,
    parse_paper_command,
)


class FakeImportArxiv:
    def __init__(self, paper, *, title_results=None):
        self.paper = paper
        self.title_results = title_results if title_results is not None else [paper]
        self.lookups = []
        self.searches = []
        self.downloads = 0

    def paper_by_id(self, value):
        self.lookups.append(value)
        return self.paper if self.paper and self.paper.arxiv_id == value else None

    def search_by_title(self, title, limit=5):
        self.searches.append((title, limit))
        return self.title_results[:limit]

    def download_pdf(self, url, target):
        self.downloads += 1
        target.write_bytes(b"%PDF-1.4 fake")


class FakeImportBackend:
    def __init__(self, failures=0):
        self.failures = failures
        self.uploads = []

    def upload_pdf(self, expert, paper, pdf_path):
        self.uploads.append((expert.id, paper.arxiv_id, pdf_path.exists()))
        if self.failures:
            self.failures -= 1
            raise RuntimeError("index unavailable")
        return f"file_{paper.arxiv_id}"


def objects(db, created_at, *, backend=None):
    expert = db.create_expert("Robotics", "cs.RO", "vs_import", created_at=created_at)
    conversation = db.list_conversations(expert.id)[0]
    candidate = sample_paper(created_at)
    arxiv = FakeImportArxiv(candidate)
    indexer = backend or FakeImportBackend()
    importer = PaperImportService(db, arxiv, indexer)
    return expert, conversation, candidate, arxiv, indexer, importer


def test_only_explicit_user_commands_are_recognized():
    assert parse_paper_command("Import arXiv:2608.00001").action == "import"
    assert parse_paper_command("Focus on Learning to Move").action == "focus"
    assert parse_paper_command("Remove 2608.00001 from this chat").action == "remove"
    assert parse_paper_command("The answer says import arXiv:2608.00001") is None
    assert parse_paper_command("Can you perhaps import a paper?") is None


def test_exact_id_imports_immediately_once_and_frontloads_chat_title(db, created_at):
    expert, conversation, _, arxiv, indexer, importer = objects(db, created_at)
    outcome = PaperCommandService(db, importer).handle(
        expert, conversation.id, "Bring in arXiv:2608.00001", "Analyze"
    )

    paper = db.find_paper(expert.id, "2608.00001")
    assert outcome.candidates == ()
    assert "Imported and focused" in outcome.message
    assert paper.origin == "manual" and paper.status == "ready"
    assert db.list_conversation_papers(conversation.id) == [paper]
    assert len(indexer.uploads) == arxiv.downloads == 1
    assert db.get_conversation(conversation.id).title == "Learning to Move"
    assert [message.role for message in db.list_messages(conversation.id)] == [
        "user",
        "assistant",
    ]


def test_existing_global_paper_is_reused_across_chats_without_reupload(db, created_at):
    expert, first, candidate, _, indexer, importer = objects(db, created_at)
    importer.import_paper(expert, first.id, candidate)
    second = db.create_conversation(expert.id)

    result = importer.import_id(expert, second.id, candidate.arxiv_id)

    assert result.imported is False and result.focused is True
    assert len(indexer.uploads) == 1
    assert db.list_conversation_papers(first.id)[0].id == result.paper.id
    assert db.list_conversation_papers(second.id)[0].id == result.paper.id


def test_title_query_shows_candidates_and_requires_confirmation(db, created_at):
    expert, conversation, _, arxiv, indexer, importer = objects(db, created_at)
    commands = PaperCommandService(db, importer)

    outcome = commands.handle(
        expert, conversation.id, 'Add the paper "Learning to Move"', "Analyze"
    )
    assert len(outcome.candidates) == 1
    assert indexer.uploads == []
    assert arxiv.searches == [('"Learning to Move"', 5)]

    confirmed = commands.confirm_candidate(
        expert, conversation.id, outcome.candidates[0].arxiv_id, "Analyze"
    )
    assert "Imported and focused" in confirmed.message
    assert len(indexer.uploads) == 1


def test_arbitrary_url_and_multiple_ids_are_rejected_without_network(db, created_at):
    expert, conversation, _, arxiv, _, importer = objects(db, created_at)
    commands = PaperCommandService(db, importer)

    bad_url = commands.handle(
        expert, conversation.id, "Import https://example.com/paper.pdf", "Analyze"
    )
    multiple = commands.handle(
        expert,
        conversation.id,
        "Import arXiv:2608.00001 and arXiv:2608.00002",
        "Analyze",
    )

    assert "Only arXiv" in bad_url.message
    assert "one paper" in multiple.message.lower()
    assert arxiv.lookups == [] and arxiv.downloads == 0


def test_failed_manual_import_is_visible_and_retryable(db, created_at):
    backend = FakeImportBackend(failures=1)
    expert, conversation, candidate, _, _, importer = objects(db, created_at, backend=backend)

    first = importer.import_paper(expert, conversation.id, candidate)
    second = importer.import_id(expert, conversation.id, candidate.arxiv_id)

    assert first.paper.status == "failed"
    assert "index unavailable" in first.paper.error
    assert second.paper.status == "ready"
    assert len(backend.uploads) == 2


def test_manual_import_respects_shared_sync_lock(db, created_at):
    expert, conversation, candidate, _, indexer, importer = objects(db, created_at)
    assert db.acquire_sync_lock(expert.id)
    try:
        try:
            importer.import_paper(expert, conversation.id, candidate)
        except RuntimeError as exc:
            assert "sync is running" in str(exc)
        else:
            raise AssertionError("Import unexpectedly ignored the sync lock")
    finally:
        db.release_sync_lock(expert.id)
    assert indexer.uploads == []


def test_remove_command_changes_only_chat_focus(db, created_at):
    expert, conversation, candidate, _, _, importer = objects(db, created_at)
    paper = importer.import_paper(expert, conversation.id, candidate).paper
    outcome = PaperCommandService(db, importer).handle(
        expert,
        conversation.id,
        f"Remove arXiv:{candidate.arxiv_id} from this chat",
        "Analyze",
    )
    assert "Removed" in outcome.message
    assert db.list_conversation_papers(conversation.id) == []
    assert db.get_paper(paper.id) is not None
