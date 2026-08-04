from types import SimpleNamespace

import pytest
from test_database import sample_paper

from arxiv_expert.config import Settings
from arxiv_expert.models import Citation
from arxiv_expert.openai_service import OpenAIService


class FakeFiles:
    def __init__(self):
        self.deleted = []

    def create(self, **kwargs):
        return SimpleNamespace(id="file_uploaded")

    def delete(self, file_id):
        self.deleted.append(file_id)


class FakeVectorFiles:
    def __init__(self, fail=False):
        self.fail = fail
        self.created = []

    def create_and_poll(self, **kwargs):
        self.created.append(kwargs)
        if self.fail:
            raise RuntimeError("index failed")
        return SimpleNamespace(status="completed")


class FakeVectorStores:
    def __init__(self, fail_upload=False):
        self.files = FakeVectorFiles(fail_upload)
        self.search_request = None

    def search(self, **kwargs):
        self.search_request = kwargs
        return SimpleNamespace(
            data=[
                SimpleNamespace(
                    file_id="file_uploaded",
                    filename="paper.pdf",
                    score=0.88,
                    content=[SimpleNamespace(type="text", text="retrieved evidence")],
                )
            ]
        )


class FakeResponses:
    def __init__(self):
        self.request = None

    def create(self, **kwargs):
        self.request = kwargs
        annotation = SimpleNamespace(
            type="url_citation", url="https://arxiv.org/abs/9999.00001", title="Web paper"
        )
        content = SimpleNamespace(annotations=[annotation])
        message = SimpleNamespace(type="message", content=[content])
        return SimpleNamespace(output_text="Cited response", output=[message])


class FakeClient:
    def __init__(self, fail_upload=False):
        self.files = FakeFiles()
        self.vector_stores = FakeVectorStores(fail_upload)
        self.responses = FakeResponses()


def objects(db, created_at):
    expert = db.create_expert("Robotics", "cs.RO", "vs_test", created_at=created_at)
    candidate = sample_paper(created_at)
    saved, _ = db.add_paper(expert.id, candidate)
    return expert, saved


def test_search_uses_rewrite_limit_and_score_threshold(db, created_at):
    expert, _ = objects(db, created_at)
    client = FakeClient()
    service = OpenAIService(Settings.create("sk-test"), client=client)
    hits = service.search_library(expert, "adaptive robotics")
    assert hits[0].score == 0.88
    assert hits[0].text == "retrieved evidence"
    assert client.vector_stores.search_request["rewrite_query"] is True
    assert client.vector_stores.search_request["ranking_options"]["score_threshold"] == 0.35


def test_focused_search_uses_arxiv_id_attribute_filter(db, created_at):
    expert, _ = objects(db, created_at)
    client = FakeClient()
    service = OpenAIService(Settings.create("sk-test"), client=client)
    service.search_library(expert, "adaptive robotics", ["2608.00001", "2608.00002"])
    assert client.vector_stores.search_request["filters"] == {
        "type": "in",
        "key": "arxiv_id",
        "value": ["2608.00001", "2608.00002"],
    }


def test_web_answer_sends_fixed_model_policy_and_extracts_citations(db, created_at):
    expert, _ = objects(db, created_at)
    client = FakeClient()
    settings = Settings.create("sk-test")
    service = OpenAIService(settings, client=client)
    library = Citation("library", "Stored paper", "https://arxiv.org/abs/2608.00001", "L1")
    answer = service.answer(
        expert=expert,
        query="Find other work",
        mode="Experiment Designer",
        history=(),
        library_context="[L1] evidence",
        library_citations=(library,),
        use_web=True,
        scope="global",
    )
    request = client.responses.request
    assert request["model"] == "gpt-5.6"
    assert request["reasoning"] == {"effort": "medium"}
    assert request["tools"] == [{"type": "web_search", "search_context_size": "medium"}]
    assert request["safety_identifier"] == settings.safety_identifier
    assert [item.kind for item in answer.citations] == ["library", "web"]


def test_failed_vector_indexing_deletes_uploaded_file(db, created_at, tmp_path):
    expert, paper = objects(db, created_at)
    client = FakeClient(fail_upload=True)
    service = OpenAIService(Settings.create("sk-test"), client=client)
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF fake")
    with pytest.raises(RuntimeError, match="index failed"):
        service.upload_pdf(expert, paper, pdf)
    assert client.files.deleted == ["file_uploaded"]
