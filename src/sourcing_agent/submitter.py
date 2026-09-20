"""Routing and submission.

This module decides what happens to a finished application packet, and it is
the only place in the codebase that may call ``connector.submit``.

The decision is a sequence of independent locks, evaluated in order. Every one
of them must be open for a packet to be transmitted; any one of them closed
routes the packet to disk instead, with the reason recorded:

1. the run opted into submission at all
2. the connector *class* holds submission rights and implements the verb
3. the connector is on the run's explicit allowlist
4. the credential that connector submits under is present
5. this posting has not already been applied to
6. the per-run submission limit has not been reached
7. the model recommended proceeding

Note where the model sits: seventh, and only ever as a veto. It can stop an
application; it cannot start one. Nothing it emits can open locks 1-6, because
those are read from configuration, the environment, the class hierarchy and the
database - none of which are in its output path.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .capabilities import Route, RightsError
from .config import Profile, SubmissionConfig
from .connectors.base import Connector, ConnectorError, SubmissionCapable
from .models import Applicant, ApplicationPacket, Receipt
from .store import AlreadySubmitted, Store


@dataclass(frozen=True)
class RouteDecision:
    route: Route
    reason: str

    @property
    def submits(self) -> bool:
        return self.route is Route.SUBMIT


def resolve_route(
    connector: Connector,
    config: SubmissionConfig,
    *,
    credential_present: bool,
    already_submitted: bool,
    submissions_this_run: int,
    model_recommends: bool,
) -> RouteDecision:
    """Pure function: given the world, where does this packet go?

    Kept free of I/O so the routing table can be tested exhaustively - the
    interesting cases are the ones where a connector *could* submit but must
    not, and those are easy to get wrong in a method that also does HTTP.
    """
    cls = type(connector)

    if not config.enabled:
        return RouteDecision(Route.EXPORT, "submission is disabled for this run")

    if not cls.can_submit():
        return RouteDecision(
            Route.EXPORT, f"{cls.slug} is read-only: {cls.rights.basis}"
        )

    if cls.slug not in config.allow_connectors:
        return RouteDecision(
            Route.EXPORT, f"{cls.slug} is not on this run's submission allowlist"
        )

    if not credential_present:
        return RouteDecision(
            Route.EXPORT, f"{cls.rights.requires_credential} is not set"
        )

    if already_submitted:
        return RouteDecision(Route.EXPORT, "already applied to this posting")

    if submissions_this_run >= config.max_per_run:
        return RouteDecision(
            Route.EXPORT, f"per-run submission limit of {config.max_per_run} reached"
        )

    if not model_recommends:
        return RouteDecision(Route.EXPORT, "the draft stage recommended against applying")

    return RouteDecision(Route.SUBMIT, f"{cls.slug} holds submission rights")


class Submitter:
    """Routes packets, writes exports, and is the sole caller of ``submit``."""

    def __init__(
        self,
        config: SubmissionConfig,
        profile: Profile,
        store: Store,
        out_dir: Path,
        run_id: str = "unknown",
        base_dir: Path = Path("."),
    ) -> None:
        self.store_run_id = run_id
        self.config = config
        self.profile = profile
        self.store = store
        self.out_dir = out_dir
        self.base_dir = base_dir
        self.submitted_this_run = 0
        self._applicant: Applicant | None = None

    # -- applicant ---------------------------------------------------------

    @property
    def applicant(self) -> Applicant:
        """Built once from the profile; connectors never see the profile."""
        if self._applicant is None:
            resume_bytes = None
            filename = "resume.pdf"
            if self.profile.resume_path:
                path = self.profile.resume_path
                if not path.is_absolute():
                    path = self.base_dir / path
                if path.exists():
                    resume_bytes = path.read_bytes()
                    filename = path.name
            self._applicant = Applicant(
                name=self.profile.name,
                email=self.profile.email,
                phone=self.profile.phone,
                location=self.profile.location,
                links=self.profile.links,
                resume_filename=filename,
                resume_bytes=resume_bytes,
            )
        return self._applicant

    # -- routing -----------------------------------------------------------

    def decide(self, connector: Connector, packet_proceed: bool, posting_key: str) -> RouteDecision:
        credential_env = type(connector).rights.requires_credential
        return resolve_route(
            connector,
            self.config,
            credential_present=bool(credential_env and os.environ.get(credential_env)),
            already_submitted=self.store.has_submitted(posting_key),
            submissions_this_run=self.submitted_this_run,
            model_recommends=packet_proceed,
        )

    def dispatch(self, packet: ApplicationPacket, connector: Connector) -> Receipt:
        """Execute the routing decision already recorded on the packet."""
        if packet.route is Route.EXPORT:
            return self.export(packet)

        cls = type(connector)
        # Belt and braces. `resolve_route` already checked rights; this asserts
        # the structural half again at the only call site that matters, so a
        # future connector that declares rights without implementing the verb
        # fails loudly here rather than at an AttributeError deep in a request.
        if not isinstance(connector, SubmissionCapable) or not cls.rights.can_submit:
            raise RightsError(
                f"{cls.slug} does not hold submission rights: {cls.rights.basis}"
            )

        credential_env = cls.rights.requires_credential or ""
        credential = os.environ.get(credential_env, "")
        if not credential:
            raise RightsError(f"{cls.slug}: {credential_env} is not set")

        if self.config.dry_run:
            # Build the real request and validate it, then stop short of the
            # wire. A dry run that skipped construction would not prove much.
            request = connector.build_submission(packet, self.applicant, credential)
            path = self._write_dry_run(packet, request)
            receipt = Receipt(
                submitted=False,
                route=Route.SUBMIT,
                connector=cls.slug,
                posting_key=packet.posting.key,
                detail=f"dry run: {request['method']} {request['url']} -> {path}",
                dry_run=True,
            )
            self.store.record_receipt(self.store_run_id, receipt)
            # Counted like a real submission: a dry run that ignored the
            # per-run limit would not preview the live run it stands in for.
            self.submitted_this_run += 1
            return receipt

        try:
            receipt = connector.submit(packet, self.applicant, credential)
        except ConnectorError as exc:
            receipt = Receipt(
                submitted=False,
                route=Route.SUBMIT,
                connector=cls.slug,
                posting_key=packet.posting.key,
                detail=f"submission failed: {exc}",
            )
            self.store.record_receipt(self.store_run_id, receipt)
            return receipt

        try:
            self.store.record_receipt(self.store_run_id, receipt)
        except AlreadySubmitted:
            # The database refused a second submission for this posting. The
            # request already went out, so report it honestly rather than
            # pretending it did not happen.
            receipt = receipt.model_copy(
                update={"detail": receipt.detail + " (duplicate receipt suppressed)"}
            )
        self.submitted_this_run += 1
        self.export(packet, suffix="submitted")
        return receipt

    # -- export ------------------------------------------------------------

    def export(self, packet: ApplicationPacket, suffix: str = "") -> Receipt:
        """Write the packet to disk for a human to submit."""
        path = self._write_packet(packet, suffix)
        receipt = Receipt(
            submitted=False,
            route=Route.EXPORT,
            connector=packet.posting.source,
            posting_key=packet.posting.key,
            detail=f"{packet.route_reason} -> {path}",
        )
        if not suffix:
            self.store.record_receipt(self.store_run_id, receipt)
        return receipt

    def _packet_dir(self) -> Path:
        directory = self.out_dir / "applications"
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def _safe_name(self, packet: ApplicationPacket) -> str:
        raw = f"{packet.posting.company}-{packet.posting.title}-{packet.posting.source_id}"
        return "".join(c if c.isalnum() or c in "-_" else "-" for c in raw)[:80].strip("-")

    def _write_packet(self, packet: ApplicationPacket, suffix: str = "") -> Path:
        name = self._safe_name(packet) + (f".{suffix}" if suffix else "")
        directory = self._packet_dir()
        (directory / f"{name}.json").write_text(
            packet.model_dump_json(indent=2), encoding="utf-8"
        )
        markdown = directory / f"{name}.md"
        markdown.write_text(_render_markdown(packet), encoding="utf-8")
        return markdown

    def _write_dry_run(self, packet: ApplicationPacket, request: dict[str, Any]) -> Path:
        directory = self.out_dir / "dry-run"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{self._safe_name(packet)}.json"
        path.write_text(json.dumps(request, indent=2, default=str), encoding="utf-8")
        return path


def _render_markdown(packet: ApplicationPacket) -> str:
    posting = packet.posting
    assessment = packet.assessment
    draft = packet.draft
    lines = [
        f"# {posting.title} - {posting.company}",
        "",
        f"- **Source**: {posting.source}",
        f"- **URL**: {posting.url}",
        f"- **Location**: {posting.location or 'unspecified'}",
        f"- **Fit score**: {assessment.fit_score}/100",
        f"- **Route**: {packet.route.value} ({packet.route_reason})",
        "",
        "## Assessment",
        "",
        assessment.summary,
        "",
        f"**Strengths**: {', '.join(assessment.matched_strengths) or 'none recorded'}",
        "",
        f"**Gaps**: {', '.join(assessment.gaps) or 'none recorded'}",
        "",
    ]
    if assessment.dealbreakers:
        lines += [f"**Dealbreakers**: {', '.join(assessment.dealbreakers)}", ""]
    lines += ["## Cover letter", "", draft.cover_letter, ""]
    if draft.resume_bullets:
        lines += ["## Tailored resume bullets", ""]
        lines += [f"- {bullet}" for bullet in draft.resume_bullets]
        lines.append("")
    if draft.screening_answers:
        lines += ["## Screening answers", ""]
        for answer in draft.screening_answers:
            lines += [f"**{answer.question}**", "", answer.answer, ""]
    if draft.tailoring_notes:
        lines += ["## Notes", ""]
        lines += [f"- {note}" for note in draft.tailoring_notes]
        lines.append("")
    return "\n".join(lines)
