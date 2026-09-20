"""Lever postings."""

from __future__ import annotations

from typing import Any, Iterable

import httpx

from ..capabilities import Capability, Route, SubmissionRights
from ..models import Applicant, ApplicationPacket, Posting, Receipt
from .base import ConnectorError, SubmissionCapable
from .http import looks_remote, strip_html
from .registry import register
from .util import file_descriptor, parse_dt, resume_files, safe_json, slugify, split_handle

API = "https://api.lever.co/v0/postings"


@register
class LeverConnector(SubmissionCapable):
    slug = "lever"
    name = "Lever"
    kind = "ats"
    homepage = "https://www.lever.co"

    rights = SubmissionRights(
        can_submit=True,
        basis=(
            "Lever's postings API exposes POST /v0/postings/{site}/{id} for "
            "candidate applications against a site-issued key."
        ),
        requires_credential="LEVER_API_KEY",
    )
    capabilities = frozenset(
        {Capability.DISCOVER, Capability.FETCH_DETAIL, Capability.SUBMIT}
    )

    def discover(self) -> Iterable[Posting]:
        postings: list[Posting] = []
        for site in self.config.companies:
            payload = self.fetcher.json(
                self.slug, f"{API}/{site}", key=site, params={"mode": "json"}
            )
            if not isinstance(payload, list):
                continue
            for job in payload:
                posting = self._to_posting(site, job)
                if posting is not None:
                    postings.append(posting)
        return postings

    def _to_posting(self, site: str, job: dict[str, Any]) -> Posting | None:
        job_id = job.get("id")
        if not job_id:
            return None
        categories = job.get("categories") or {}
        location = categories.get("location")
        workplace = job.get("workplaceType")

        # Lever splits the body across descriptionPlain and a list of sections.
        body = [job.get("descriptionPlain") or strip_html(job.get("description"))]
        for block in job.get("lists") or []:
            body.append(strip_html(block.get("text")))
            body.append(strip_html(block.get("content")))
        body.append(job.get("additionalPlain") or strip_html(job.get("additional")))
        description = "\n\n".join(part for part in body if part)

        return Posting(
            source=self.slug,
            source_id=str(job_id),
            url=job.get("hostedUrl") or job.get("applyUrl") or f"https://jobs.lever.co/{site}/{job_id}",
            title=job.get("text") or "",
            company=site.replace("-", " ").title(),
            location=location,
            remote=looks_remote(location, workplace, categories.get("commitment")),
            description=description,
            posted_at=parse_dt(job.get("createdAt")),
            compensation_raw=(job.get("salaryRange") or {}).get("text")
            if isinstance(job.get("salaryRange"), dict)
            else None,
            apply_handle=f"{site}/{job_id}",
            raw={"site": site, "id": job_id, "team": categories.get("team")},
        )

    def build_submission(
        self, packet: ApplicationPacket, applicant: Applicant, credential: str
    ) -> dict[str, Any]:
        site, posting_id = split_handle(packet.posting.apply_handle)
        data: dict[str, Any] = {
            "name": applicant.name,
            "email": applicant.email,
            "comments": packet.draft.cover_letter,
        }
        if applicant.phone:
            data["phone"] = applicant.phone
        for label, url in applicant.links.items():
            data[f"urls[{label}]"] = url
        for answer in packet.draft.screening_answers:
            data[f"cards[{slugify(answer.question)}]"] = answer.answer

        return {
            "method": "POST",
            "url": f"{API}/{site}/{posting_id}",
            "auth": "query key=LEVER_API_KEY",
            "data": data,
            "files": file_descriptor(applicant),
        }

    def submit(
        self, packet: ApplicationPacket, applicant: Applicant, credential: str
    ) -> Receipt:
        request = self.build_submission(packet, applicant, credential)
        try:
            response = self.fetcher.client.post(
                request["url"],
                params={"key": credential},
                data=request["data"],
                files=resume_files(applicant),
            )
            response.raise_for_status()
            body = safe_json(response)
        except httpx.HTTPError as exc:
            raise ConnectorError(f"lever submission failed: {exc}") from exc

        return Receipt(
            submitted=True,
            route=Route.SUBMIT,
            connector=self.slug,
            posting_key=packet.posting.key,
            detail=f"HTTP {response.status_code}",
            external_id=str(body.get("applicationId") or body.get("id") or ""),
        )
