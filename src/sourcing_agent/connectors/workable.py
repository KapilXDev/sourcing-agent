"""Workable-hosted career pages."""

from __future__ import annotations

from typing import Any, Iterable

import httpx

from ..capabilities import Capability, Route, SubmissionRights
from ..models import Applicant, ApplicationPacket, Posting, Receipt
from .base import ConnectorError, SubmissionCapable
from .http import looks_remote, strip_html
from .registry import register
from .util import file_descriptor, parse_dt, safe_json, split_handle

WIDGET_API = "https://apply.workable.com/api/v1/widget/accounts"


@register
class WorkableConnector(SubmissionCapable):
    slug = "workable"
    name = "Workable"
    kind = "ats"
    homepage = "https://www.workable.com"

    rights = SubmissionRights(
        can_submit=True,
        basis=(
            "Workable's SPI exposes POST /spi/v3/jobs/{shortcode}/candidates "
            "for applications made with an account access token."
        ),
        requires_credential="WORKABLE_API_KEY",
    )
    capabilities = frozenset(
        {Capability.DISCOVER, Capability.FETCH_DETAIL, Capability.SUBMIT}
    )

    def discover(self) -> Iterable[Posting]:
        postings: list[Posting] = []
        for account in self.config.companies:
            payload = self.fetcher.json(
                self.slug,
                f"{WIDGET_API}/{account}",
                key=account,
                params={"details": "true"},
            )
            company = payload.get("name") or account.replace("-", " ").title()
            for job in payload.get("jobs", []):
                posting = self._to_posting(account, company, job)
                if posting is not None:
                    postings.append(posting)
        return postings

    def _to_posting(
        self, account: str, company: str, job: dict[str, Any]
    ) -> Posting | None:
        shortcode = job.get("shortcode") or job.get("id")
        if not shortcode:
            return None
        location = ", ".join(
            part
            for part in (job.get("city"), job.get("state"), job.get("country"))
            if part
        )
        description = "\n\n".join(
            strip_html(job.get(field))
            for field in ("description", "requirements", "benefits")
            if job.get(field)
        )
        telecommuting = job.get("telecommuting")
        return Posting(
            source=self.slug,
            source_id=f"{account}-{shortcode}",
            url=job.get("url") or job.get("application_url") or "",
            title=job.get("title") or "",
            company=company,
            location=location or None,
            remote=bool(telecommuting) if telecommuting is not None
            else looks_remote(location, job.get("employment_type")),
            description=description,
            posted_at=parse_dt(job.get("published_on") or job.get("created_at")),
            apply_handle=f"{account}/{shortcode}",
            raw={"account": account, "shortcode": shortcode},
        )

    def build_submission(
        self, packet: ApplicationPacket, applicant: Applicant, credential: str
    ) -> dict[str, Any]:
        account, shortcode = split_handle(packet.posting.apply_handle)
        candidate: dict[str, Any] = {
            "name": applicant.name,
            "email": applicant.email,
            "summary": packet.draft.cover_letter[:2000],
        }
        if applicant.phone:
            candidate["phone"] = applicant.phone
        if applicant.resume_bytes:
            candidate["resume"] = {
                "name": applicant.resume_filename,
                "data": "<base64 resume omitted from the dry-run record>",
            }
        if applicant.links:
            candidate["social_profiles"] = [
                {"type": label, "url": url} for label, url in applicant.links.items()
            ]
        answers = [
            {"question": a.question, "body": a.answer}
            for a in packet.draft.screening_answers
        ]
        if answers:
            candidate["answers"] = answers

        return {
            "method": "POST",
            "url": f"https://{account}.workable.com/spi/v3/jobs/{shortcode}/candidates",
            "auth": "bearer(WORKABLE_API_KEY)",
            "json": {"sourced": False, "candidate": candidate},
            "files": file_descriptor(applicant),
        }

    def submit(
        self, packet: ApplicationPacket, applicant: Applicant, credential: str
    ) -> Receipt:
        import base64

        request = self.build_submission(packet, applicant, credential)
        payload = request["json"]
        if applicant.resume_bytes:
            payload["candidate"]["resume"] = {
                "name": applicant.resume_filename,
                "data": base64.b64encode(applicant.resume_bytes).decode("ascii"),
            }
        try:
            response = self.fetcher.client.post(
                request["url"],
                json=payload,
                headers={"Authorization": f"Bearer {credential}"},
            )
            response.raise_for_status()
            body = safe_json(response)
        except httpx.HTTPError as exc:
            raise ConnectorError(f"workable submission failed: {exc}") from exc

        return Receipt(
            submitted=True,
            route=Route.SUBMIT,
            connector=self.slug,
            posting_key=packet.posting.key,
            detail=f"HTTP {response.status_code}",
            external_id=str((body.get("candidate") or {}).get("id") or ""),
        )
