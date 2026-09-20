"""Aggregator sources: RemoteOK, Remotive, Arbeitnow, Hacker News.

All four are read-only. An aggregator is an index, not a system of record -
the actual application lives on the employer's own ATS, and "applying" through
an aggregator means following its outbound link. There is nothing here that a
submission could legitimately be transmitted to, so none of these classes
inherit :class:`SubmissionCapable`.

They are still worth crawling: aggregators surface companies whose board
tokens you do not know, and a posting found here often dedupes against the
same role discovered directly on Greenhouse or Lever - at which point the
ATS copy, with its richer description, wins.
"""

from __future__ import annotations

import re
from typing import Any, Iterable

from ..capabilities import Capability, read_only
from ..models import Posting
from .base import Connector
from .http import looks_remote, strip_html
from .registry import register
from .util import parse_dt

_AGGREGATOR_BASIS = (
    "index only - the employer's ATS is the system of record, and the "
    "aggregator exposes no application endpoint"
)


@register
class RemoteOKConnector(Connector):
    slug = "remoteok"
    name = "RemoteOK"
    kind = "aggregator"
    homepage = "https://remoteok.com"

    rights = read_only(_AGGREGATOR_BASIS)
    capabilities = frozenset({Capability.DISCOVER})

    def discover(self) -> Iterable[Posting]:
        payload = self.fetcher.json(self.slug, "https://remoteok.com/api", key="feed")
        if not isinstance(payload, list):
            return []
        postings = []
        # The first element of the feed is a legal notice, not a job.
        for job in payload:
            if not isinstance(job, dict) or not job.get("id"):
                continue
            if job.get("legal"):
                continue
            salary = None
            if job.get("salary_min") and job.get("salary_max"):
                salary = f"${job['salary_min']:,} - ${job['salary_max']:,}"
            postings.append(
                Posting(
                    source=self.slug,
                    source_id=str(job["id"]),
                    url=job.get("url") or f"https://remoteok.com/remote-jobs/{job['id']}",
                    title=job.get("position") or job.get("title") or "",
                    company=job.get("company") or "",
                    location=job.get("location") or "Remote",
                    remote=True,
                    description=strip_html(job.get("description")),
                    posted_at=parse_dt(job.get("date") or job.get("epoch")),
                    compensation_raw=salary,
                    raw={"tags": job.get("tags", [])},
                )
            )
        return postings[: self.config.limit]


@register
class RemotiveConnector(Connector):
    slug = "remotive"
    name = "Remotive"
    kind = "aggregator"
    homepage = "https://remotive.com"

    rights = read_only(_AGGREGATOR_BASIS)
    capabilities = frozenset({Capability.DISCOVER})

    def discover(self) -> Iterable[Posting]:
        params: dict[str, Any] = {"limit": self.config.limit}
        if self.config.query:
            params["search"] = self.config.query
        payload = self.fetcher.json(
            self.slug, "https://remotive.com/api/remote-jobs", key="feed", params=params
        )
        postings = []
        for job in payload.get("jobs", []):
            job_id = job.get("id")
            if not job_id:
                continue
            location = job.get("candidate_required_location")
            postings.append(
                Posting(
                    source=self.slug,
                    source_id=str(job_id),
                    url=job.get("url") or "",
                    title=job.get("title") or "",
                    company=job.get("company_name") or "",
                    location=location,
                    remote=True,
                    description=strip_html(job.get("description")),
                    posted_at=parse_dt(job.get("publication_date")),
                    compensation_raw=job.get("salary") or None,
                    raw={"category": job.get("category"), "type": job.get("job_type")},
                )
            )
        return postings


@register
class ArbeitnowConnector(Connector):
    slug = "arbeitnow"
    name = "Arbeitnow"
    kind = "aggregator"
    homepage = "https://www.arbeitnow.com"

    rights = read_only(_AGGREGATOR_BASIS)
    capabilities = frozenset({Capability.DISCOVER})

    def discover(self) -> Iterable[Posting]:
        payload = self.fetcher.json(
            self.slug,
            "https://www.arbeitnow.com/api/job-board-api",
            key="feed",
        )
        postings = []
        for job in payload.get("data", []):
            slug = job.get("slug")
            if not slug:
                continue
            postings.append(
                Posting(
                    source=self.slug,
                    source_id=str(slug),
                    url=job.get("url") or f"https://www.arbeitnow.com/view/{slug}",
                    title=job.get("title") or "",
                    company=job.get("company_name") or "",
                    location=job.get("location"),
                    remote=bool(job.get("remote")),
                    description=strip_html(job.get("description")),
                    posted_at=parse_dt(job.get("created_at")),
                    raw={"tags": job.get("tags", []), "types": job.get("job_types", [])},
                )
            )
        return postings[: self.config.limit]


_HN_HEADER = re.compile(r"^\s*(?P<company>[^|]{2,80}?)\s*\|\s*(?P<rest>.+)$")


@register
class HackerNewsConnector(Connector):
    """The monthly "Ask HN: Who is hiring?" thread.

    Structurally different from every other source: the postings are free-text
    comments with a loose ``Company | Role | Location | Remote`` convention.
    Parsing is deliberately shallow - pull out the header line, keep the rest as
    the description, and let the funnel deal with the mess. Comments that do not
    look like a posting at all are dropped before they can cost anything.
    """

    slug = "hackernews"
    name = "Hacker News (Who is hiring)"
    kind = "aggregator"
    homepage = "https://news.ycombinator.com"

    rights = read_only(
        "a discussion thread - applications go to whatever address the "
        "comment names, which is not an endpoint this connector owns"
    )
    capabilities = frozenset({Capability.DISCOVER})

    def discover(self) -> Iterable[Posting]:
        story_id = self._latest_thread_id()
        if story_id is None:
            return []
        payload = self.fetcher.json(
            self.slug,
            f"https://hn.algolia.com/api/v1/items/{story_id}",
            key=f"thread-{story_id}",
        )
        postings = []
        for child in payload.get("children") or []:
            posting = self._to_posting(story_id, child)
            if posting is not None:
                postings.append(posting)
        return postings[: self.config.limit]

    def _latest_thread_id(self) -> int | None:
        payload = self.fetcher.json(
            self.slug,
            "https://hn.algolia.com/api/v1/search_by_date",
            key="latest-thread",
            params={
                "query": "Ask HN: Who is hiring?",
                "tags": "story,author_whoishiring",
                "hitsPerPage": 1,
            },
        )
        hits = payload.get("hits") or []
        if not hits:
            return None
        try:
            return int(hits[0]["objectID"])
        except (KeyError, TypeError, ValueError):
            return None

    def _to_posting(self, story_id: int, comment: dict[str, Any]) -> Posting | None:
        comment_id = comment.get("id")
        text = strip_html(comment.get("text"))
        if not comment_id or len(text) < 120:
            return None
        first_line = next((ln for ln in text.splitlines() if ln.strip()), "")
        match = _HN_HEADER.match(first_line)
        if not match:
            return None  # not in the posting convention; not worth a model call
        company = match.group("company").strip()
        rest = [p.strip() for p in match.group("rest").split("|") if p.strip()]
        title = rest[0] if rest else "Unspecified role"
        location = " / ".join(rest[1:3]) if len(rest) > 1 else None
        return Posting(
            source=self.slug,
            source_id=str(comment_id),
            url=f"https://news.ycombinator.com/item?id={comment_id}",
            title=title[:160],
            company=company[:120],
            location=location,
            remote=looks_remote(first_line),
            description=text,
            posted_at=parse_dt(comment.get("created_at")),
            raw={"thread": story_id, "author": comment.get("author")},
        )
