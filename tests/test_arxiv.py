from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

import httpx

from arxiv_expert.arxiv import ArxivClient, canonical_arxiv_id, parse_atom_feed

FEED = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"
      xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/"
      xmlns:arxiv="http://arxiv.org/schemas/atom">
  <opensearch:totalResults>1</opensearch:totalResults>
  <entry>
    <id>http://arxiv.org/abs/2608.00001v2</id>
    <updated>2026-08-03T12:10:00Z</updated>
    <published>2026-08-03T12:05:00Z</published>
    <title>  Learning\n to Move </title>
    <summary>Robots learn robust control.</summary>
    <author><name>Ada Robot</name></author>
    <link href="http://arxiv.org/abs/2608.00001v2" rel="alternate" type="text/html"/>
    <link title="pdf" href="http://arxiv.org/pdf/2608.00001v2" rel="related" type="application/pdf"/>
    <arxiv:primary_category term="cs.RO"/>
    <category term="cs.RO"/><category term="cs.LG"/>
  </entry>
</feed>"""


def test_parse_feed_preserves_cross_lists_and_ignores_revision_in_canonical_id():
    papers, total = parse_atom_feed(FEED)
    assert total == 1
    assert papers[0].arxiv_id == "2608.00001"
    assert papers[0].version == 2
    assert papers[0].categories == ("cs.RO", "cs.LG")
    assert papers[0].title == "Learning to Move"
    assert papers[0].pdf_url.startswith("https://")


def test_identifier_normalization():
    assert canonical_arxiv_id("https://arxiv.org/pdf/2608.00001v3") == ("2608.00001", 3)
    assert canonical_arxiv_id("arXiv:cs/0701001v2") == ("cs/0701001", 2)


def test_client_builds_category_and_creation_boundary_query():
    seen = {}

    def handler(request):
        seen.update(parse_qs(urlparse(str(request.url)).query))
        return httpx.Response(200, content=FEED, headers={"content-type": "application/atom+xml"})

    client = ArxivClient(
        http_client=httpx.Client(transport=httpx.MockTransport(handler)), sleep=lambda _: None
    )
    papers = client.papers_between(
        "cs.RO",
        datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc),
        datetime(2026, 8, 3, 13, 0, tzinfo=timezone.utc),
    )
    assert len(papers) == 1
    assert seen["search_query"] == ["cat:cs.RO AND submittedDate:[202608031200 TO 202608031300]"]
    assert seen["sortBy"] == ["submittedDate"]


def test_rate_limit_retry_honors_retry_after_header():
    calls = 0
    waits = []

    def handler(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, headers={"retry-after": "7"}, request=request)
        return httpx.Response(200, content=FEED, request=request)

    client = ArxivClient(
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=waits.append,
    )
    papers = client.papers_between(
        "cs.RO",
        datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc),
        datetime(2026, 8, 3, 13, 0, tzinfo=timezone.utc),
    )
    assert len(papers) == 1
    assert calls == 2
    assert 7.0 in waits
