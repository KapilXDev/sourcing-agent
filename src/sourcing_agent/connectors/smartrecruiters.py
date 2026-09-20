"""SmartRecruiters postings."""

from __future__ import annotations

from typing import Any, Iterable

import httpx

from ..capabilities import Capability, Route, SubmissionRights
from ..models import Applicant, ApplicationPacket, Posting, Receipt
from .base import ConnectorError, SubmissionCapable
from .http import FetchError, looks_remote, strip_html
from .registry import register
from .util import file_descriptor, parse_dt, resume_files, safe_json

API = "https://api.smartrecruiters.com/v1"


@register
class SmartRecruitersConnector(SubmissionCapable):
    slug = "smartrecruiters"
    name = "SmartRecruiters"
    kind = "ats"
    homepage = "https://www.smartrecruiters.com"

    rights = SubmissionRights(
        can_submit=True,
        basis=(
            "SmartRecruiters exposes POST /v1/postings/{id}/candidates as its "
            "public application endpoint, keyed per company."
        ),
        requires_credential="SMARTRECRUITERS_API_KEY",
    )
    capabilities = frozenset(
        {Capability.DISCOVER, Capability.FETCH_DETAIL, Capability.SUBMIT}
    )

    def discover(self) -> Iterable[Posting]:
        postings: list[Posting] = []
        for company in self.config.companies:
            payload = self.fetcher.json(
                self.slug,
                f"{API}/companies/{company}/postings",
                key=company,
                params={"limit": min(self.config.limit, 100)},
            )
            for job in payload.get("content", []):
                posting = self._to_posting(company, job)
                if posting is not None:
                    postings.append(posting)
        return postings

    def _to_posting(self, company: str, job: dict[str, Any]) -> Posting | None:
        job_id = job.get("id") or job.get("uuid")
        if not job_id:
            return None
        loc = job.get("location") or {}
        location = ", ".join(
            str(part)
            for part in (loc.get("city"), loc.get("region"), loc.get("country"))
            if part
        )
        company_name = (job.get("company") or {}).get("name") or company.title()
        return Posting(
            source=self.slug,
            source_id=str(job_id),
            url=job.get("applyUrl")
            or job.get("ref")
            or f"https://jobs.smartrecruiters.com/{company}/{job_id}",
            title=job.get("name") or "",
            company=company_name,
            location=location or None,
            remote=bool(loc.get("remote")) if loc.get("remote") is not None
            else looks_remote(location),
            description="",  # filled by fetch_detail; the list endpoint has no body
            posted_at=parse_dt(job.get("releasedDate") or job.get("createdOn")),
            apply_handle=str(job_id),
            raw={"company": company, "id": job_id},
        )

    def fetch_detail(self, posting: Posting) -> Posting:
        """SmartRecruiters is the one source whose list endpoint omits the body.

        Detail is fetched lazily - only for postings that survive the gate - so
        a crawl of 500 postings does not become 500 extra HTTP calls.
        """
        if posting.description:
            return posting
        try:
            payload = self.fetcher.json(
                self.slug,
                f"{API}/postings/{posting.source_id}",
                key=f"detail-{posting.source_id}",
            )
        except FetchError:
            return posting
        sections = ((payload.get("jobAd") or {}).get("sections")) or {}
        parts = [
            strip_html((sections.get(name) or {}).get("text"))
            for name in ("companyDescription", "jobDescription", "qualifications", "additionalInformation")
        ]
        return posting.model_copy(
            update={"description": "\n\n".join(p for p in parts if p)}
        )

    def build_submission(
        self, packet: ApplicationPacket, applicant: Applicant, credential: str
    ) -> dict[str, Any]:
        candidate: dict[str, Any] = {
            "firstName": applicant.first_name,
            "lastName": applicant.last_name,
            "email": applicant.email,
        }
        if applicant.phone:
            candidate["phoneNumber"] = applicant.phone
        if applicant.location:
            candidate["location"] = {"city": applicant.location}
        if applicant.links.get("linkedin"):
            candidate["web"] = {"linkedin": applicant.links["linkedin"]}
        answers = [
            {"questionId": a.question, "answer": a.answer}
            for a in packet.draft.screening_answers
        ]
        if answers:
            candidate["answers"] = answers

        # SmartRecruiters has no cover-letter field on the candidate object, so
        # it goes up as a second attachment. Listed here so the dry run shows
        # every request the live path would make, not just the first.
        attachments = []
        if applicant.resume_bytes:
            attachments.append({"field": "resume", **file_descriptor(applicant)["resume"]})
        if packet.draft.cover_letter:
            attachments.append(
                {
                    "field": "cover_letter",
                    "filename": "cover-letter.txt",
                    "bytes": len(packet.draft.cover_letter.encode("utf-8")),
                    "content_type": "text/plain",
                }
            )

        return {
            "method": "POST",
            "url": f"{API}/postings/{packet.posting.apply_handle}/candidates",
            "auth": "header x-api-key=SMARTRECRUITERS_API_KEY",
            "json": candidate,
            "files": file_descriptor(applicant),
            "then_attachments": attachments,
        }

    def submit(
        self, packet: ApplicationPacket, applicant: Applicant, credential: str
    ) -> Receipt:
        request = self.build_submission(packet, applicant, credential)
        try:
            response = self.fetcher.client.post(
                request["url"],
                json=request["json"],
                headers={"x-api-key": credential},
            )
            response.raise_for_status()
            body = safe_json(response)
            candidate_id = str(body.get("id") or "")

            if candidate_id:
                for files in self._attachments(packet, applicant):
                    attach = self.fetcher.client.post(
                        f"{API}/candidates/{candidate_id}/attachments",
                        files=files,
                        headers={"x-api-key": credential},
                    )
                    attach.raise_for_status()
        except httpx.HTTPError as exc:
            raise ConnectorError(f"smartrecruiters submission failed: {exc}") from exc

        return Receipt(
            submitted=True,
            route=Route.SUBMIT,
            connector=self.slug,
            posting_key=packet.posting.key,
            detail=f"HTTP {response.status_code}",
            external_id=candidate_id or None,
        )

    @staticmethod
    def _attachments(packet: ApplicationPacket, applicant: Applicant) -> list[dict[str, Any]]:
        """Resume and cover letter, in the order the live path uploads them."""
        files: list[dict[str, Any]] = []
        if applicant.resume_bytes:
            files.append(resume_files(applicant, field="file"))
        if packet.draft.cover_letter:
            files.append(
                {
                    "file": (
                        "cover-letter.txt",
                        packet.draft.cover_letter.encode("utf-8"),
                        "text/plain",
                    )
                }
            )
        return files
