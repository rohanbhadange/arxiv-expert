"""Opt-in real arXiv + OpenAI smoke test with complete remote cleanup."""

from __future__ import annotations

import os
import tempfile
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from arxiv_expert.arxiv import ArxivClient
from arxiv_expert.chat import ChatService
from arxiv_expert.config import Settings, load_settings
from arxiv_expert.database import Database
from arxiv_expert.openai_service import OpenAIService


def settings() -> Settings:
    key = os.environ.get("OPENAI_API_KEY")
    if key:
        return Settings.create(key)
    return load_settings()


def main() -> None:
    configured = settings()
    backend = OpenAIService(configured)
    arxiv = ArxivClient(page_size=100)
    store_id = ""
    file_id = ""
    try:
        now = datetime.now(timezone.utc)
        papers = arxiv.papers_between("cs.RO", now - timedelta(days=7), now)
        if not papers:
            raise RuntimeError("No cs.RO paper was returned in the last 7 days.")
        with tempfile.TemporaryDirectory(prefix="arxiv-expert-live-") as directory:
            candidate = None
            pdf = Path(directory) / "paper.pdf"
            for available in papers:
                try:
                    arxiv.download_pdf(available.pdf_url, pdf)
                    candidate = available
                    break
                except httpx.HTTPStatusError as exc:
                    if exc.response.status_code not in {404, 410}:
                        raise
            if candidate is None:
                raise RuntimeError("No downloadable cs.RO paper was available for the live check.")

            store_id = backend.create_vector_store("Live smoke test", "cs.RO")
            db = Database(Path(directory) / "smoke.sqlite3")
            db.initialize()
            expert = db.create_expert(
                "Live smoke test", "cs.RO", store_id, created_at=candidate.published_at
            )
            paper, _ = db.add_paper(expert.id, candidate)
            file_id = backend.upload_pdf(expert, paper, pdf)
            db.set_paper_status(paper.id, "ready", openai_file_id=file_id)
            conversation = db.list_conversations(expert.id)[0]
            db.focus_paper(conversation.id, paper.id)
            chat = ChatService(db, backend)
            analysis = chat.ask(
                expert,
                conversation.id,
                "What is this paper's primary contribution and evaluation setup?",
                "Analyze",
            )
            if not analysis.text.strip() or analysis.used_web:
                raise RuntimeError("Library-grounded analysis did not complete as expected.")
            if not any(item.kind == "library" for item in analysis.citations):
                raise RuntimeError("Library analysis did not expose a library citation.")
            if analysis.citations[0].marker != "F1":
                raise RuntimeError("Focused analysis did not use a stable F1 citation.")
            if not any(f"[{item.marker}]" in analysis.text for item in analysis.citations):
                raise RuntimeError("Library analysis did not cite its evidence in the answer text.")

            second_conversation = db.create_conversation(expert.id)
            experiment = chat.ask(
                expert,
                second_conversation.id,
                "Search the web for a different recent robotics paper and design a follow-up experiment that connects it to this paper.",
                "Experiment Designer",
            )
            if not experiment.text.strip() or not experiment.used_web:
                raise RuntimeError("Web-backed experiment design did not complete as expected.")
            if not any(
                item.kind == "web" and item.url.startswith("http") for item in experiment.citations
            ):
                raise RuntimeError(
                    "Web-backed experiment design returned no clickable web citation."
                )
            required_sections = (
                "Hypothesis",
                "Why it follows",
                "Setup",
                "Baselines",
                "Metrics",
                "Ablations",
                "Expected outcomes",
                "Risks",
                "Evidence",
            )
            missing = [
                section
                for section in required_sections
                if section.lower() not in experiment.text.lower()
            ]
            if missing:
                raise RuntimeError(
                    f"Experiment design omitted required sections: {', '.join(missing)}"
                )
            if len(db.list_messages(conversation.id)) != 2:
                raise RuntimeError("Focused chat history was not isolated.")
            if len(db.list_messages(second_conversation.id)) != 2:
                raise RuntimeError("Unfocused chat history was not isolated.")
            if db.list_conversation_papers(second_conversation.id):
                raise RuntimeError("Paper focus leaked into another conversation.")
            print(
                f"PASS: indexed {candidate.arxiv_id}; produced focused library-cited analysis; "
                "produced web-cited experiment design; isolated two persistent chats."
            )
    finally:
        arxiv.close()
        if file_id:
            with suppress(Exception):
                backend.delete_file(file_id)
        if store_id:
            with suppress(Exception):
                backend.delete_vector_store(store_id)


if __name__ == "__main__":
    main()
