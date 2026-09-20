"""Browser-backed sources: Workday, LinkedIn, Wellfound.

These three are where the "submission rights bound to the connector" rule earns
its keep. All three are technically automatable end to end - a browser can fill
and submit their forms. None of them grant a job seeker an application API, and
two of them prohibit automated interaction outright. So all three are read-only:
they discover, and the packet is exported for you to submit yourself.

That is a deliberate, written-down limit, not an oversight. The classes below
do not inherit :class:`SubmissionCapable`, so no amount of configuration - or
model output - can produce a submission through them.
"""

from __future__ import annotations

import json
import re
from typing import Any, Iterable

from ..capabilities import Capability, read_only
from ..models import Posting
from .base import Connector, ConnectorError
from .http import FetchError, looks_remote, strip_html
from .registry import register
from .util import parse_dt


@register
class WorkdayConnector(Connector):
    """Workday tenants, via the CXS endpoint their own front end calls.

    Configure as ``tenant/site`` pairs, e.g. ``nvidia/NVIDIAExternalCareerSite``.
    The list endpoint is a POST; job bodies come from a follow-up GET, fetched
    lazily so an unfiltered crawl does not multiply into hundreds of calls.
    """

    slug = "workday"
    name = "Workday"
    kind = "browser"
    homepage = "https://www.workday.com"

    rights = read_only(
        "Workday exposes no candidate-facing application API; applying means "
        "driving a multi-step form behind a tenant account, which is not "
        "authorised automation"
    )
    capabilities = frozenset({Capability.DISCOVER, Capability.FETCH_DETAIL})

    def discover(self) -> Iterable[Posting]:
        postings: list[Posting] = []
        for entry in self.config.companies:
            tenant, _, site = entry.partition("/")
            if not tenant or not site:
                raise ConnectorError(
                    f"workday source must be 'tenant/site', got {entry!r}"
                )
            host = f"https://{tenant}.wd1.myworkdayjobs.com"
            payload = self.fetcher.json_post(
                self.slug,
                f"{host}/wday/cxs/{tenant}/{site}/jobs",
                key=f"{tenant}-{site}",
                body={
                    "appliedFacets": {},
                    "limit": min(self.config.limit, 20),
                    "offset": 0,
                    "searchText": self.config.query or "",
                },
            )
            for job in payload.get("jobPostings", []):
                posting = self._to_posting(tenant, site, host, job)
                if posting is not None:
                    postings.append(posting)
        return postings

    def _to_posting(
        self, tenant: str, site: str, host: str, job: dict[str, Any]
    ) -> Posting | None:
        path = job.get("externalPath")
        if not path:
            return None
        location = job.get("locationsText")
        # bulletFields is where tenants stash the requisition id.
        bullets = job.get("bulletFields") or []
        return Posting(
            source=self.slug,
            source_id=f"{tenant}-{path.rsplit('/', 1)[-1]}",
            url=f"{host}/en-US/{site}{path}",
            title=job.get("title") or "",
            company=tenant.replace("-", " ").title(),
            location=location,
            remote=looks_remote(location, job.get("title")),
            description="",
            posted_at=_relative_date(job.get("postedOn")),
            apply_handle=None,
            raw={
                "tenant": tenant,
                "site": site,
                "path": path,
                "req": bullets[0] if bullets else None,
            },
        )

    def fetch_detail(self, posting: Posting) -> Posting:
        if posting.description:
            return posting
        tenant = posting.raw.get("tenant")
        site = posting.raw.get("site")
        path = posting.raw.get("path")
        if not (tenant and site and path):
            return posting
        try:
            payload = self.fetcher.json(
                self.slug,
                f"https://{tenant}.wd1.myworkdayjobs.com/wday/cxs/{tenant}/{site}{path}",
                key=f"detail-{posting.source_id}",
            )
        except FetchError:
            return posting
        info = payload.get("jobPostingInfo") or {}
        return posting.model_copy(
            update={
                "description": strip_html(info.get("jobDescription")),
                "posted_at": posting.posted_at or parse_dt(info.get("startDate")),
            }
        )


_LI_CARD = re.compile(r"<li>(.*?)</li>", re.S)
_LI_FIELD = {
    "title": re.compile(r'class="[^"]*base-search-card__title[^"]*"[^>]*>(.*?)<', re.S),
    "company": re.compile(r'class="[^"]*base-search-card__subtitle[^"]*"[^>]*>\s*<a[^>]*>(.*?)<', re.S),
    "location": re.compile(r'class="[^"]*job-search-card__location[^"]*"[^>]*>(.*?)<', re.S),
}
_LI_URL = re.compile(r'href="(https://www\.linkedin\.com/jobs/view/[^"?]+)')
_LI_DATE = re.compile(r'datetime="([0-9-]+)"')


@register
class LinkedInConnector(Connector):
    """LinkedIn's guest job-search fragment.

    Discovery only, and shallow by construction: the guest surface returns
    cards, not bodies. Postings from here are mostly useful as dedupe evidence -
    when the same role is already known from an ATS, the ATS copy wins.
    """

    slug = "linkedin"
    name = "LinkedIn"
    kind = "browser"
    homepage = "https://www.linkedin.com/jobs"

    rights = read_only(
        "LinkedIn's terms prohibit automated interaction with the service; "
        "Easy Apply is not automatable within them"
    )
    capabilities = frozenset({Capability.DISCOVER})

    def discover(self) -> Iterable[Posting]:
        query = self.config.query or ""
        if not query:
            return []
        url = (
            "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
            f"?keywords={query.replace(' ', '%20')}&start=0"
        )
        html = self.fetcher.text(self.slug, url, key=_slug(query))
        postings = []
        for card in _LI_CARD.findall(html):
            posting = self._to_posting(card)
            if posting is not None:
                postings.append(posting)
        return postings[: self.config.limit]

    def _to_posting(self, card: str) -> Posting | None:
        url_match = _LI_URL.search(card)
        if not url_match:
            return None
        url = url_match.group(1)
        job_id = url.rstrip("/").rsplit("-", 1)[-1]
        fields = {
            name: strip_html(pattern.search(card).group(1)) if pattern.search(card) else ""
            for name, pattern in _LI_FIELD.items()
        }
        if not fields["title"]:
            return None
        date_match = _LI_DATE.search(card)
        return Posting(
            source=self.slug,
            source_id=job_id,
            url=url,
            title=fields["title"],
            company=fields["company"],
            location=fields["location"] or None,
            remote=looks_remote(fields["location"], fields["title"]),
            description="",
            posted_at=parse_dt(date_match.group(1)) if date_match else None,
            apply_handle=None,
            raw={"card": True},
        )


_NEXT_DATA = re.compile(
    r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S
)


@register
class WellfoundConnector(Connector):
    """Wellfound (formerly AngelList Talent).

    The page is a Next.js app, so the data arrives as a JSON blob in
    ``__NEXT_DATA__`` rather than as markup - which makes parsing stable, but
    still requires rendering the page to get it.
    """

    slug = "wellfound"
    name = "Wellfound"
    kind = "browser"
    homepage = "https://wellfound.com"

    rights = read_only(
        "Wellfound's terms prohibit automated access and its apply flow is "
        "account-bound with no candidate API"
    )
    capabilities = frozenset({Capability.DISCOVER})

    def discover(self) -> Iterable[Posting]:
        role = self.config.query or "software-engineer"
        url = f"https://wellfound.com/role/r/{role}"
        html = self.fetcher.render(
            self.slug, url, key=_slug(role), wait_for="script#__NEXT_DATA__"
        )
        match = _NEXT_DATA.search(html)
        if not match:
            return []
        try:
            blob = json.loads(match.group(1))
        except ValueError as exc:
            raise ConnectorError("wellfound: __NEXT_DATA__ was not valid JSON") from exc

        postings = []
        for node in _walk_for_jobs(blob):
            posting = self._to_posting(node)
            if posting is not None:
                postings.append(posting)
        return postings[: self.config.limit]

    def _to_posting(self, node: dict[str, Any]) -> Posting | None:
        job_id = node.get("id")
        title = node.get("title")
        if not job_id or not title:
            return None
        company = node.get("startup") or node.get("company") or {}
        company_name = company.get("name") if isinstance(company, dict) else str(company)
        location = node.get("locationNames") or node.get("location")
        if isinstance(location, list):
            location = ", ".join(str(x) for x in location)
        comp = None
        if node.get("compensation"):
            comp = str(node["compensation"])
        elif node.get("salaryMin") and node.get("salaryMax"):
            comp = f"${node['salaryMin']:,} - ${node['salaryMax']:,}"
        return Posting(
            source=self.slug,
            source_id=str(job_id),
            url=node.get("url") or f"https://wellfound.com/jobs/{job_id}",
            title=str(title),
            company=str(company_name or ""),
            location=location or None,
            remote=bool(node.get("remote")) if node.get("remote") is not None
            else looks_remote(location),
            description=strip_html(node.get("description")),
            posted_at=parse_dt(node.get("liveStartAt") or node.get("createdAt")),
            compensation_raw=comp,
            apply_handle=None,
            raw={"slug": node.get("slug")},
        )


# --------------------------------------------------------------------------


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "default"


_RELATIVE = re.compile(r"(\d+)\+?\s*(day|week|month|hour)", re.I)


def _relative_date(text: str | None):
    """Workday reports 'Posted 5 Days Ago' rather than a timestamp."""
    if not text:
        return None
    from datetime import datetime, timedelta, timezone

    if "today" in text.lower():
        return datetime.now(timezone.utc)
    match = _RELATIVE.search(text)
    if not match:
        return None
    amount = int(match.group(1))
    unit = match.group(2).lower()
    days = {"hour": amount / 24, "day": amount, "week": amount * 7, "month": amount * 30}[unit]
    return datetime.now(timezone.utc) - timedelta(days=days)


def _walk_for_jobs(node: Any, depth: int = 0) -> Iterable[dict[str, Any]]:
    """Find job-shaped dicts anywhere in a Next.js payload.

    Wellfound reshuffles its prop tree often enough that pinning an exact path
    is a maintenance liability; matching on shape survives their refactors.
    """
    if depth > 12:
        return
    if isinstance(node, dict):
        if "title" in node and ("startup" in node or "company" in node) and "id" in node:
            yield node
            return
        for value in node.values():
            yield from _walk_for_jobs(value, depth + 1)
    elif isinstance(node, list):
        for item in node:
            yield from _walk_for_jobs(item, depth + 1)
