"""Greenhouse job boards.

Discovery uses the public board API, which needs no credential. Submission
uses the documented board application endpoint, which does - and which is
therefore the *basis* of this connector's submission rights.
"""

from __future__ import annotations

import html
from typing import Any, Iterable

import httpx

from ..capabilities import Capability, Route, SubmissionRights
from ..models import Applicant, ApplicationPacket, Posting, Receipt
from .base import ConnectorError, SubmissionCapable
from .http import looks_remote, strip_html
from .registry import register
from .util import file_descriptor, parse_dt, resume_files, safe_json, slugify, split_handle

API = "https://boards-api.greenhouse.io/v1/boards"


@register
class GreenhouseConnector(SubmissionCapable):
    slug = "greenhouse"
    name = "Greenhouse"
    kind = "ats"
    homepage = "https://www.greenhouse.io"

    rights = SubmissionRights(
        can_submit=True,
        basis=(
            "Greenhouse publishes a job board application endpoint "
            "(POST /v1/boards/{board}/jobs/{id}) intended for candidate "
            "submissions; the board owner issues the key."
        ),
        requires_credential="GREENHOUSE_API_KEY",
    )
    capabilities = frozenset(
        {Capability.DISCOVER, Capability.FETCH_DETAIL, Capability.SUBMIT}
    )

    # -- discovery ---------------------------------------------------------

    def discover(self) -> Iterable[Posting]:
        boards = self.config.companies
        if not boards:
            return []
        postings: list[Posting] = []
        for board in boards:
            payload = self.fetcher.json(
                self.slug,
                f"{API}/{board}/jobs",
                key=board,
                params={"content": "true"},
            )
            for job in payload.get("jobs", []):
                posting = self._to_posting(board, job)
                if posting is not None:
                    postings.append(posting)
        return postings

    def _to_posting(self, board: str, job: dict[str, Any]) -> Posting | None:
        job_id = job.get("id")
        if job_id is None:
            return None
        # Greenhouse double-escapes description HTML.
        description = strip_html(html.unescape(job.get("content") or ""))
        location = (job.get("location") or {}).get("name")
        offices = ", ".join(
            o.get("name", "") for o in job.get("offices", []) if o.get("name")
        )
        posted = job.get("first_published") or job.get("updated_at")
        return Posting(
            source=self.slug,
            source_id=str(job_id),
            url=job.get("absolute_url") or f"https://boards.greenhouse.io/{board}/jobs/{job_id}",
            title=job.get("title") or "",
            company=job.get("company_name") or board.replace("-", " ").title(),
            location=location or offices or None,
            remote=looks_remote(location, offices),
            description=description,
            posted_at=parse_dt(posted),
            compensation_raw=self._metadata_value(job, ("salary", "compensation", "pay")),
            apply_handle=f"{board}/{job_id}",
            raw={"board": board, "id": job_id},
        )

    # -- submission --------------------------------------------------------

    def build_submission(
        self, packet: ApplicationPacket, applicant: Applicant, credential: str
    ) -> dict[str, Any]:
        board, job_id = split_handle(packet.posting.apply_handle)
        data = {
            "first_name": applicant.first_name,
            "last_name": applicant.last_name,
            "email": applicant.email,
            "cover_letter_text": packet.draft.cover_letter,
        }
        if applicant.phone:
            data["phone"] = applicant.phone
        for label, url in applicant.links.items():
            data[f"link_{label}"] = url
        for answer in packet.draft.screening_answers:
            data[f"question_{slugify(answer.question)}"] = answer.answer

        return {
            "method": "POST",
            "url": f"{API}/{board}/jobs/{job_id}",
            "auth": "basic(GREENHOUSE_API_KEY, '')",
            "data": data,
            "files": file_descriptor(applicant),
        }

    def submit(
        self, packet: ApplicationPacket, applicant: Applicant, credential: str
    ) -> Receipt:
        request = self.build_submission(packet, applicant, credential)
        files = resume_files(applicant)
        try:
            response = self.fetcher.client.post(
                request["url"],
                data=request["data"],
                files=files,
                auth=(credential, ""),
            )
            response.raise_for_status()
            body = safe_json(response)
        except httpx.HTTPError as exc:
            raise ConnectorError(f"greenhouse submission failed: {exc}") from exc

        return Receipt(
            submitted=True,
            route=Route.SUBMIT,
            connector=self.slug,
            posting_key=packet.posting.key,
            detail=f"HTTP {response.status_code} {body.get('success', '')}".strip(),
            external_id=str(body.get("id") or body.get("application_id") or ""),
        )

    @staticmethod
    def _metadata_value(job: dict[str, Any], names: tuple[str, ...]) -> str | None:
        """Greenhouse boards expose comp as a free-form metadata field, if at all."""
        for item in job.get("metadata") or []:
            label = str(item.get("name", "")).lower()
            if any(n in label for n in names) and item.get("value"):
                return str(item["value"])
        return None
