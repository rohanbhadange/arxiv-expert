"""Explicit, deterministic paper import and per-chat focus commands."""

from __future__ import annotations

import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from urllib.parse import urlparse

from .arxiv import ID_PATTERN, ArxivClient, canonical_arxiv_id
from .database import Database
from .models import (
    ArxivPaper,
    Expert,
    Paper,
    PaperImportCandidate,
    PaperImportResult,
)

MAX_FOCUS_PAPERS = 8


class ImportBackend(Protocol):
    def upload_pdf(self, expert: Expert, paper: Paper, pdf_path: Path) -> str: ...


@dataclass(frozen=True, slots=True)
class PaperCommand:
    action: str
    target: str


@dataclass(frozen=True, slots=True)
class CommandOutcome:
    message: str
    candidates: tuple[PaperImportCandidate, ...] = ()


_IMPORT = re.compile(r"^(?:bring in|import|add(?: the)? paper)\s+(.+?)\s*$", re.I)
_FOCUS = re.compile(r"^focus on\s+(.+?)\s*$", re.I)
_REMOVE = re.compile(r"^remove\s+(.+?)\s+from this chat\s*$", re.I)


def parse_paper_command(value: str) -> PaperCommand | None:
    """Recognize only explicit, user-authored commands at the start of a turn."""
    clean = value.strip()
    for pattern, action in ((_REMOVE, "remove"), (_FOCUS, "focus"), (_IMPORT, "import")):
        match = pattern.fullmatch(clean)
        if match:
            return PaperCommand(action, match.group(1).strip())
    return None


def _exact_id(value: str) -> str | None:
    target = value.strip().strip('"').strip("'")
    if target.lower().startswith(("http://", "https://")):
        parsed = urlparse(target)
        if parsed.hostname not in {"arxiv.org", "www.arxiv.org"}:
            raise ValueError("Only arXiv abstract or PDF links can be imported.")
        if not re.match(r"^/(?:abs|pdf)/", parsed.path, re.I):
            raise ValueError("Use an arXiv abstract or PDF link.")
        identifier = re.sub(r"^/(?:abs|pdf)/", "", parsed.path, flags=re.I)
        identifier = re.sub(r"\.pdf$", "", identifier, flags=re.I)
        try:
            return canonical_arxiv_id(identifier)[0]
        except ValueError as exc:
            raise ValueError("The arXiv link does not contain a valid paper ID.") from exc
    matches = list(ID_PATTERN.finditer(target))
    if len(matches) > 1:
        raise ValueError("Import one paper at a time.")
    if not matches:
        return None
    match = matches[0]
    residue = (target[: match.start()] + target[match.end() :]).strip().lower()
    allowed_residue = {"", ".pdf"}
    if residue not in allowed_residue:
        return None
    return canonical_arxiv_id(target)[0]


def _candidate(paper: ArxivPaper | Paper) -> PaperImportCandidate:
    return PaperImportCandidate(
        arxiv_id=paper.arxiv_id,
        title=paper.title,
        authors=paper.authors,
        published_at=paper.published_at,
        abstract_url=paper.abstract_url,
    )


class PaperImportService:
    """Add a requested arXiv paper to the canonical store once, then focus it."""

    def __init__(self, database: Database, arxiv: ArxivClient, backend: ImportBackend):
        self.database = database
        self.arxiv = arxiv
        self.backend = backend

    def title_candidates(self, expert: Expert, title: str) -> list[PaperImportCandidate]:
        local = self.database.search_papers(expert.id, title, limit=5)
        if local:
            return [_candidate(paper) for paper in local]
        return [_candidate(paper) for paper in self.arxiv.search_by_title(title, limit=5)]

    def import_id(self, expert: Expert, conversation_id: str, value: str) -> PaperImportResult:
        arxiv_id, _ = canonical_arxiv_id(value)
        existing = self.database.find_paper(expert.id, arxiv_id)
        if existing and existing.status == "ready":
            focused = self.database.focus_paper(conversation_id, existing.id)
            return PaperImportResult(existing, imported=False, focused=focused)
        paper = self.arxiv.paper_by_id(arxiv_id)
        if paper is None:
            raise ValueError(f"arXiv paper {arxiv_id} was not found.")
        return self.import_paper(expert, conversation_id, paper)

    def import_paper(
        self, expert: Expert, conversation_id: str, candidate: ArxivPaper
    ) -> PaperImportResult:
        existing_focus = {
            paper.id for paper in self.database.list_conversation_papers(conversation_id)
        }
        existing = self.database.find_paper(expert.id, candidate.arxiv_id)
        if existing and existing.id in existing_focus and existing.status == "ready":
            return PaperImportResult(existing, imported=False, focused=False)
        if len(existing_focus) >= MAX_FOCUS_PAPERS:
            raise ValueError(f"A chat can focus on at most {MAX_FOCUS_PAPERS} papers.")
        if existing and existing.status == "ready":
            focused = self.database.focus_paper(conversation_id, existing.id)
            return PaperImportResult(existing, imported=False, focused=focused)
        if not self.database.acquire_sync_lock(expert.id):
            raise RuntimeError("A paper sync is running. Try the import again when it finishes.")
        try:
            # Recheck after acquiring the shared ingestion lock.
            paper = self.database.find_paper(expert.id, candidate.arxiv_id)
            created = False
            if paper is None:
                paper, created = self.database.add_paper(expert.id, candidate, origin="manual")
            focused = self.database.focus_paper(conversation_id, paper.id)
            if paper.status != "ready":
                self._ingest(expert, paper)
            refreshed = self.database.get_paper(paper.id)
            assert refreshed is not None
            return PaperImportResult(refreshed, imported=created, focused=focused)
        finally:
            self.database.release_sync_lock(expert.id)

    def _ingest(self, expert: Expert, paper: Paper) -> None:
        self.database.set_paper_status(paper.id, "indexing", error=None)
        try:
            with tempfile.TemporaryDirectory(prefix="arxiv-expert-") as directory:
                safe_id = paper.arxiv_id.replace("/", "_")
                target = Path(directory) / f"{safe_id}v{paper.version}.pdf"
                self.arxiv.download_pdf(paper.pdf_url, target)
                file_id = self.backend.upload_pdf(expert, paper, target)
            self.database.set_paper_status(paper.id, "ready", openai_file_id=file_id, error=None)
        except Exception as exc:
            self.database.set_paper_status(paper.id, "failed", error=str(exc)[:1000])


class PaperCommandService:
    """Handle explicit paper-control messages without asking the language model."""

    def __init__(self, database: Database, importer: PaperImportService):
        self.database = database
        self.importer = importer

    def handle(
        self,
        expert: Expert,
        conversation_id: str,
        query: str,
        mode: str,
    ) -> CommandOutcome | None:
        command = parse_paper_command(query)
        if command is None:
            return None
        self.database.add_message(expert.id, conversation_id, "user", query.strip(), mode)
        try:
            if command.action == "remove":
                outcome = self._remove(expert, conversation_id, command.target)
            else:
                outcome = self._add_or_focus(expert, conversation_id, command.target)
        except Exception as exc:
            outcome = CommandOutcome(str(exc))
        self.database.add_message(expert.id, conversation_id, "assistant", outcome.message, mode)
        return outcome

    def confirm_candidate(
        self,
        expert: Expert,
        conversation_id: str,
        arxiv_id: str,
        mode: str,
    ) -> CommandOutcome:
        try:
            result = self.importer.import_id(expert, conversation_id, arxiv_id)
            outcome = CommandOutcome(self._result_message(result))
            self.database.title_new_conversation(conversation_id, result.paper.title)
        except Exception as exc:
            outcome = CommandOutcome(str(exc))
        self.database.add_message(expert.id, conversation_id, "assistant", outcome.message, mode)
        return outcome

    def _add_or_focus(self, expert: Expert, conversation_id: str, target: str) -> CommandOutcome:
        arxiv_id = _exact_id(target)
        if arxiv_id:
            result = self.importer.import_id(expert, conversation_id, arxiv_id)
            self.database.title_new_conversation(conversation_id, result.paper.title)
            return CommandOutcome(self._result_message(result))

        local = self.database.search_papers(expert.id, target, limit=5)
        if len(local) == 1:
            focused = self.database.focus_paper(conversation_id, local[0].id)
            self.database.title_new_conversation(conversation_id, local[0].title)
            suffix = "Added" if focused else "Already included"
            return CommandOutcome(f"{suffix} **{local[0].title}** in this chat's focus.")
        candidates = (
            tuple(_candidate(paper) for paper in local)
            if local
            else tuple(self.importer.title_candidates(expert, target))
        )
        if not candidates:
            return CommandOutcome(f'No arXiv papers matched "{target}".')
        lines = ["Choose the paper to import and focus:"]
        lines.extend(
            f"{index}. **{paper.title}** — {', '.join(paper.authors[:3])} "
            f"({paper.published_at.date().isoformat()}, arXiv:{paper.arxiv_id})"
            for index, paper in enumerate(candidates, start=1)
        )
        return CommandOutcome("\n\n".join(lines), candidates)

    def _remove(self, expert: Expert, conversation_id: str, target: str) -> CommandOutcome:
        focused = self.database.list_conversation_papers(conversation_id)
        try:
            arxiv_id = _exact_id(target)
        except ValueError:
            arxiv_id = None
        clean = target.strip().strip('"').strip("'").lower()
        matches = [
            paper
            for paper in focused
            if (arxiv_id and paper.arxiv_id == arxiv_id)
            or paper.title.lower() == clean
            or clean in paper.title.lower()
        ]
        if not matches:
            return CommandOutcome("That paper is not in this chat's focus.")
        if len(matches) > 1:
            ids = ", ".join(f"arXiv:{paper.arxiv_id}" for paper in matches)
            return CommandOutcome(f"More than one focused paper matched. Use one ID: {ids}.")
        self.database.unfocus_paper(conversation_id, matches[0].id)
        return CommandOutcome(f"Removed **{matches[0].title}** from this chat's focus.")

    @staticmethod
    def _result_message(result: PaperImportResult) -> str:
        if result.paper.status == "failed":
            return (
                f"Added **{result.paper.title}** to this chat, but indexing failed: "
                f"{result.paper.error}. It will retry during the next sync."
            )
        if result.imported:
            return f"Imported and focused **{result.paper.title}**."
        if result.focused:
            return f"Focused **{result.paper.title}** from the saved library."
        return f"**{result.paper.title}** is already included in this chat's focus."
