"""Small, polite client for the official arXiv Atom API."""

from __future__ import annotations

import re
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable
from datetime import datetime, timezone
from urllib.parse import urlencode

import httpx

from .models import ArxivPaper

ARXIV_API_URL = "https://export.arxiv.org/api/query"
ATOM = "{http://www.w3.org/2005/Atom}"
ARXIV = "{http://arxiv.org/schemas/atom}"
OPENSEARCH = "{http://a9.com/-/spec/opensearch/1.1/}"
ID_PATTERN = re.compile(
    r"(?:arxiv:\s*|arxiv\.org/(?:abs|pdf)/)?([a-z.-]+/\d{7}|\d{4}\.\d{4,5})(?:v(\d+))?", re.I
)


def canonical_arxiv_id(value: str) -> tuple[str, int]:
    match = ID_PATTERN.search(value.strip())
    if not match:
        raise ValueError(f"Unrecognized arXiv identifier: {value}")
    return match.group(1), int(match.group(2) or 1)


def _clean(value: str | None) -> str:
    return " ".join((value or "").split())


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.astimezone(timezone.utc)


def parse_atom_feed(xml: bytes | str) -> tuple[list[ArxivPaper], int]:
    root = ET.fromstring(xml)
    total_element = root.find(f"{OPENSEARCH}totalResults")
    total = int(total_element.text or 0) if total_element is not None else 0
    papers: list[ArxivPaper] = []
    for entry in root.findall(f"{ATOM}entry"):
        raw_id = _clean(entry.findtext(f"{ATOM}id"))
        try:
            arxiv_id, version = canonical_arxiv_id(raw_id)
        except ValueError:
            continue
        links = entry.findall(f"{ATOM}link")
        abstract_url = raw_id.replace("http://", "https://")
        pdf_url = f"https://arxiv.org/pdf/{arxiv_id}"
        for link in links:
            href = (link.attrib.get("href") or "").replace("http://", "https://")
            if link.attrib.get("rel") == "alternate":
                abstract_url = href
            if link.attrib.get("title") == "pdf" or link.attrib.get("type") == "application/pdf":
                pdf_url = href
        categories = tuple(
            item.attrib["term"]
            for item in entry.findall(f"{ATOM}category")
            if item.attrib.get("term")
        )
        primary = entry.find(f"{ARXIV}primary_category")
        primary_category = (
            primary.attrib.get("term", categories[0] if categories else "")
            if primary is not None
            else (categories[0] if categories else "")
        )
        authors = tuple(
            _clean(author.findtext(f"{ATOM}name")) for author in entry.findall(f"{ATOM}author")
        )
        papers.append(
            ArxivPaper(
                arxiv_id=arxiv_id,
                version=version,
                title=_clean(entry.findtext(f"{ATOM}title")),
                authors=authors,
                summary=_clean(entry.findtext(f"{ATOM}summary")),
                published_at=_parse_time(entry.findtext(f"{ATOM}published") or ""),
                updated_at=_parse_time(entry.findtext(f"{ATOM}updated") or ""),
                categories=categories,
                primary_category=primary_category,
                abstract_url=abstract_url,
                pdf_url=pdf_url,
            )
        )
    return papers, total


class ArxivClient:
    def __init__(
        self,
        *,
        http_client: httpx.Client | None = None,
        page_size: int = 100,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._owned_client = http_client is None
        self.http = http_client or httpx.Client(
            timeout=httpx.Timeout(60.0, connect=15.0),
            follow_redirects=True,
            headers={"User-Agent": "arxiv-expert/0.1 (personal research assistant)"},
        )
        self.page_size = page_size
        self.sleep = sleep
        self.clock = clock
        self._last_api_request: float | None = None

    def close(self) -> None:
        if self._owned_client:
            self.http.close()

    def papers_between(self, category: str, start: datetime, end: datetime) -> list[ArxivPaper]:
        start_utc = start.astimezone(timezone.utc)
        end_utc = end.astimezone(timezone.utc)
        date_range = f"submittedDate:[{start_utc:%Y%m%d%H%M} TO {end_utc:%Y%m%d%H%M}]"
        query = f"cat:{category} AND {date_range}"
        offset = 0
        found: list[ArxivPaper] = []
        total = 1
        while offset < total:
            parameters = {
                "search_query": query,
                "start": offset,
                "max_results": self.page_size,
                "sortBy": "submittedDate",
                "sortOrder": "ascending",
            }
            response = self._get_with_retry(f"{ARXIV_API_URL}?{urlencode(parameters)}")
            page, total = parse_atom_feed(response.content)
            found.extend(page)
            offset += len(page)
            if not page or offset >= total:
                break
        # The API's date syntax is minute-granular. Exact filtering preserves the
        # expert's creation boundary while the overlap window prevents gaps.
        return [
            paper
            for paper in found
            if start_utc <= paper.published_at <= end_utc and category in paper.categories
        ]

    def download_pdf(self, url: str, target) -> None:
        last_error: Exception | None = None
        for attempt in range(4):
            try:
                with self.http.stream("GET", url) as response:
                    response.raise_for_status()
                    content_type = response.headers.get("content-type", "").lower()
                    if "pdf" not in content_type and not url.lower().endswith(".pdf"):
                        raise RuntimeError(
                            f"Expected a PDF from arXiv, received {content_type or 'unknown content'}"
                        )
                    with open(target, "wb") as destination:
                        for chunk in response.iter_bytes():
                            destination.write(chunk)
                return
            except (httpx.RequestError, httpx.HTTPStatusError) as exc:
                last_error = exc
                status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else 0
                if status and status not in {429, 500, 502, 503, 504}:
                    raise
                if attempt < 3:
                    self.sleep(self._retry_delay(exc, attempt))
        assert last_error is not None
        raise last_error

    def _get_with_retry(self, url: str) -> httpx.Response:
        last_error: Exception | None = None
        for attempt in range(4):
            try:
                self._pace_api()
                response = self.http.get(url)
                self._last_api_request = self.clock()
                response.raise_for_status()
                return response
            except (httpx.RequestError, httpx.HTTPStatusError) as exc:
                last_error = exc
                status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else 0
                if status and status not in {429, 500, 502, 503, 504}:
                    raise
                if attempt < 3:
                    self.sleep(self._retry_delay(exc, attempt))
        assert last_error is not None
        raise last_error

    def _pace_api(self) -> None:
        if self._last_api_request is None:
            return
        remaining = 3.0 - (self.clock() - self._last_api_request)
        if remaining > 0:
            self.sleep(remaining)

    @staticmethod
    def _retry_delay(exc: Exception, attempt: int) -> float:
        if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 429:
            retry_after = exc.response.headers.get("retry-after")
            if retry_after:
                try:
                    return min(max(float(retry_after), 3.0), 60.0)
                except ValueError:
                    pass
            return float(3 * (attempt + 1))
        return 2.0**attempt
