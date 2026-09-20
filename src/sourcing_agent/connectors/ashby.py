"""Ashby job boards."""

from __future__ import annotations

import json
from typing import Any, Iterable

import httpx

from ..capabilities import Capability, Route, SubmissionRights
from ..models import Applicant, ApplicationPacket, Posting, Receipt
from .base import ConnectorError, SubmissionCapable
from .http import looks_remote, strip_html
from .registry import register
from .util import file_descriptor, parse_dt, resume_files, safe_json, split_handle

BOARD_API = "https://api.ashbyhq.com/posting-api/job-board"
SUBMIT_API = "https://api.ashbyhq.com/applicationForm.submit"


@register
class AshbyConnector(SubmissionCapable):
    slug = "ashby"
    name = "Ashby"
    kind = "ats"
    homepage = "https://www.ashbyhq.com"

    rights = SubmissionRights(
        can_submit=True,
        basis=(
            "Ashby's applicationForm.submit endpoint accepts candidate "
            "applications against an organisation API key."
        ),
        requires_credential="ASHBY_API_KEY",
    )
    capabilities = frozenset(
        {Capability.DISCOVER, Capability.FETCH_DETAIL, Capability.SUBMIT}
    )

    def discover(self) -> Iterable[Posting]:
        postings: list[Posting] = []
        for board in self.config.companies:
            payload = self.fetcher.json(
                self.slug,
                f"{BOARD_API}/{board}",
                key=board,
                params={"includeCompensation": "true"},
            )
            for job in payload.get("jobs", []):
                posting = self._to_posting(board, job)
                if posting is not None:
                    postings.append(posting)
        return postings

    def _to_posting(self, board: str, job: dict[str, Any]) -> Posting | None:
        job_id = job.get("id")
        if not job_id:
            return None
        location = job.get("location")
        description = job.get("descriptionPlain") or strip_html(job.get("descriptionHtml"))
        comp = job.get("compensation") or {}
        return Posting(
            source=self.slug,
            source_id=str(job_id),
            url=job.get("jobUrl") or f"https://jobs.ashbyhq.com/{board}/{job_id}",
            title=job.get("title") or "",
            company=board.replace("-", " ").title(),
            location=location,
            remote=job.get("isRemote") if job.get("isRemote") is not None
            else looks_remote(location, job.get("employmentType")),
            description=description,
            posted_at=parse_dt(job.get("publishedAt") or job.get("updatedAt")),
            compensation_raw=comp.get("compensationTierSummary")
            or comp.get("summary")
            or None,
            apply_handle=f"{board}/{job_id}",
            raw={"board": board, "id": job_id, "team": job.get("team")},
        )

    def build_submission(
        self, packet: ApplicationPacket, applicant: Applicant, credential: str
    ) -> dict[str, Any]:
        _board, job_posting_id = split_handle(packet.posting.apply_handle)
        field_submissions: list[dict[str, Any]] = [
            {"path": "_systemfield_name", "value": applicant.name},
            {"path": "_systemfield_email", "value": applicant.email},
        ]
        if applicant.phone:
            field_submissions.append(
                {"path": "_systemfield_phone", "value": applicant.phone}
            )
        if packet.draft.cover_letter:
            field_submissions.append(
                {"path": "_systemfield_coverletter", "value": packet.draft.cover_letter}
            )
        for answer in packet.draft.screening_answers:
            field_submissions.append({"path": answer.question, "value": answer.answer})

        return {
            "method": "POST",
            "url": SUBMIT_API,
            "auth": "basic(ASHBY_API_KEY, '')",
            "data": {
                "jobPostingId": job_posting_id,
                "applicationForm": {"fieldSubmissions": field_submissions},
            },
            "files": file_descriptor(applicant),
        }

    def submit(
        self, packet: ApplicationPacket, applicant: Applicant, credential: str
    ) -> Receipt:
        request = self.build_submission(packet, applicant, credential)
        payload = request["data"]
        try:
            response = self.fetcher.client.post(
                request["url"],
                data={
                    "jobPostingId": payload["jobPostingId"],
                    # Ashby wants the form as a JSON string inside multipart.
                    "applicationForm": json.dumps(payload["applicationForm"]),
                },
                files=resume_files(applicant, field="_systemfield_resume"),
                auth=(credential, ""),
            )
            response.raise_for_status()
            body = safe_json(response)
        except httpx.HTTPError as exc:
            raise ConnectorError(f"ashby submission failed: {exc}") from exc

        if not body.get("success", True):
            raise ConnectorError(f"ashby rejected the application: {body.get('errors')}")

        return Receipt(
            submitted=True,
            route=Route.SUBMIT,
            connector=self.slug,
            posting_key=packet.posting.key,
            detail=f"HTTP {response.status_code}",
            external_id=str((body.get("results") or {}).get("id") or ""),
        )
