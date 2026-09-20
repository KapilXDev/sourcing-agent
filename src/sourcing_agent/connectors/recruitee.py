"""Recruitee career sites.

Read-only by design. Recruitee's public ``/api/offers/`` endpoint is a
discovery surface; creating candidates requires a company-scoped admin token
on a different host, which is a recruiter credential rather than a candidate
one. Since the basis for submission rights cannot be written down honestly,
the connector does not get them - and therefore has no ``submit`` method.
"""

from __future__ import annotations

from typing import Any, Iterable

from ..capabilities import Capability, read_only
from ..models import Posting
from .base import Connector
from .http import looks_remote, strip_html
from .registry import register
from .util import parse_dt


@register
class RecruiteeConnector(Connector):
    slug = "recruitee"
    name = "Recruitee"
    kind = "ats"
    homepage = "https://recruitee.com"

    rights = read_only(
        "the public offers API is discovery-only; candidate creation needs a "
        "company admin token, which a job seeker does not hold"
    )
    capabilities = frozenset({Capability.DISCOVER})

    def discover(self) -> Iterable[Posting]:
        postings: list[Posting] = []
        for company in self.config.companies:
            payload = self.fetcher.json(
                self.slug,
                f"https://{company}.recruitee.com/api/offers/",
                key=company,
            )
            for offer in payload.get("offers", []):
                posting = self._to_posting(company, offer)
                if posting is not None:
                    postings.append(posting)
        return postings

    def _to_posting(self, company: str, offer: dict[str, Any]) -> Posting | None:
        offer_id = offer.get("id")
        if not offer_id:
            return None
        location = offer.get("location") or ", ".join(
            str(p) for p in (offer.get("city"), offer.get("country")) if p
        )
        description = "\n\n".join(
            strip_html(offer.get(field))
            for field in ("description", "requirements")
            if offer.get(field)
        )
        return Posting(
            source=self.slug,
            source_id=f"{company}-{offer_id}",
            url=offer.get("careers_url")
            or offer.get("careers_apply_url")
            or f"https://{company}.recruitee.com/o/{offer.get('slug', offer_id)}",
            title=offer.get("title") or "",
            company=offer.get("company_name") or company.replace("-", " ").title(),
            location=location or None,
            remote=bool(offer.get("remote")) if offer.get("remote") is not None
            else looks_remote(location),
            description=description,
            posted_at=parse_dt(offer.get("published_at") or offer.get("created_at")),
            compensation_raw=offer.get("salary") if isinstance(offer.get("salary"), str) else None,
            apply_handle=None,  # nothing to submit through
            raw={"company": company, "id": offer_id},
        )
