"""Typed contracts.

Pydantic sits on both edges of the system: every connector must produce a
:class:`Posting`, and every model stage must produce a validated result object.
A hallucinated or malformed field fails loudly at the boundary instead of
leaking downstream.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .capabilities import Route

# --------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------

_WS = re.compile(r"\s+")
_NON_ALNUM = re.compile(r"[^a-z0-9 ]+")


def _normalize(text: str) -> str:
    return _WS.sub(" ", _NON_ALNUM.sub(" ", text.lower())).strip()


class Posting(BaseModel):
    """A job posting, normalized across all 13 sources."""

    model_config = ConfigDict(extra="forbid")

    source: str
    """Connector slug, e.g. ``greenhouse``."""

    source_id: str
    """Stable id within that source."""

    url: str
    title: str
    company: str
    location: str | None = None
    remote: bool | None = None
    description: str = ""
    posted_at: datetime | None = None
    compensation_raw: str | None = None

    apply_handle: str | None = None
    """Opaque token the owning connector needs in order to submit (board token,
    posting id, form URL). Meaningless to every other connector."""

    raw: dict[str, Any] = Field(default_factory=dict, repr=False)

    @field_validator("posted_at")
    @classmethod
    def _tz_aware(cls, v: datetime | None) -> datetime | None:
        if v is not None and v.tzinfo is None:
            return v.replace(tzinfo=timezone.utc)
        return v

    @property
    def key(self) -> str:
        """Identity *within* a source."""
        return f"{self.source}:{self.source_id}"

    @property
    def fingerprint(self) -> str:
        """Identity *across* sources.

        The same role is listed on Greenhouse, LinkedIn and three aggregators.
        Hashing normalized company + title + location collapses those into one
        row so the funnel never pays to think about the same job twice.
        """
        basis = "|".join(
            (
                _normalize(self.company),
                _normalize(self.title),
                _normalize(self.location or ""),
            )
        )
        return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:32]

    def age_days(self, now: datetime | None = None) -> float | None:
        if self.posted_at is None:
            return None
        now = now or datetime.now(timezone.utc)
        return (now - self.posted_at).total_seconds() / 86400.0


# --------------------------------------------------------------------------
# Stage 0 - the deterministic gate (no LLM)
# --------------------------------------------------------------------------


class GateDecision(BaseModel):
    """Why a posting did or did not earn a model call. Costs zero tokens."""

    model_config = ConfigDict(extra="forbid")

    passed: bool
    rejections: list[str] = Field(default_factory=list)
    """Rule ids that fired, e.g. ``seniority:overqualified``."""

    prefilter_score: int = 0
    """0-100, deterministic. Orders survivors so the cheapest stage sees the
    most promising postings first when the budget is tight."""

    matched_keywords: list[str] = Field(default_factory=list)
    missing_required: list[str] = Field(default_factory=list)

    @property
    def reason(self) -> str:
        return ", ".join(self.rejections) if self.rejections else "passed"


# --------------------------------------------------------------------------
# Stage 1 - triage (Haiku)
# --------------------------------------------------------------------------


class TriageItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ref: int
    """Index into the batch handed to the model. Cheaper and less error-prone
    than asking a small model to echo long opaque ids back."""

    keep: bool
    reason: str = Field(max_length=240)
    confidence: float = Field(ge=0.0, le=1.0)


class TriageBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    results: list[TriageItem]


# --------------------------------------------------------------------------
# Stage 2 - fit assessment (Sonnet)
# --------------------------------------------------------------------------


class FitAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fit_score: int = Field(ge=0, le=100)
    seniority: Literal[
        "intern", "junior", "mid", "senior", "staff", "principal", "lead", "unknown"
    ]
    remote_policy: Literal["remote", "hybrid", "onsite", "unclear"]
    sponsorship: Literal["offered", "not_offered", "unclear"]
    min_years_experience: int | None = Field(default=None, ge=0, le=40)
    salary_min: int | None = None
    salary_max: int | None = None
    must_have_skills: list[str] = Field(default_factory=list, max_length=20)
    matched_strengths: list[str] = Field(default_factory=list, max_length=10)
    gaps: list[str] = Field(default_factory=list, max_length=10)
    dealbreakers: list[str] = Field(default_factory=list, max_length=10)
    summary: str = Field(max_length=800)


# --------------------------------------------------------------------------
# Stage 3 - application draft (Opus)
# --------------------------------------------------------------------------


class ScreeningAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str
    answer: str


class ApplicationDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    proceed: bool
    """The model's *recommendation*. It does not decide whether anything is
    transmitted - see :func:`sourcing_agent.submitter.resolve_route`."""

    rationale: str = Field(max_length=600)
    cover_letter: str = ""
    resume_bullets: list[str] = Field(default_factory=list, max_length=8)
    screening_answers: list[ScreeningAnswer] = Field(default_factory=list, max_length=12)
    tailoring_notes: list[str] = Field(default_factory=list, max_length=8)


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------


class ApplicationPacket(BaseModel):
    """Everything needed to apply, plus the route the *system* chose."""

    model_config = ConfigDict(extra="forbid")

    posting: Posting
    assessment: FitAssessment
    draft: ApplicationDraft
    route: Route
    route_reason: str


class Receipt(BaseModel):
    model_config = ConfigDict(extra="forbid")

    submitted: bool
    route: Route
    connector: str
    posting_key: str
    detail: str
    dry_run: bool = False
    external_id: str | None = None
    at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class StageCount(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    model: str | None = None
    considered: int = 0
    advanced: int = 0
    cost_usd: float = 0.0
    skipped_for_budget: int = 0


class RunReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    started_at: datetime
    finished_at: datetime | None = None
    discovered: int = 0
    deduped: int = 0
    gate_passed: int = 0
    gate_rejected: int = 0
    stages: list[StageCount] = Field(default_factory=list)
    receipts: list[Receipt] = Field(default_factory=list)
    spend_usd: float = 0.0
    budget_usd: float = 0.0
    budget_exhausted: bool = False
    errors: list[str] = Field(default_factory=list)


class Applicant(BaseModel):
    """The identity a submission is made under.

    Assembled from the profile by the submitter and handed to the connector.
    Connectors never read the profile directly - they receive exactly the
    fields a job application form needs, and nothing else.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    email: str
    phone: str | None = None
    location: str | None = None
    links: dict[str, str] = Field(default_factory=dict)
    resume_filename: str = "resume.pdf"
    resume_bytes: bytes | None = Field(default=None, repr=False)

    @property
    def first_name(self) -> str:
        return self.name.split(" ", 1)[0] if self.name else ""

    @property
    def last_name(self) -> str:
        parts = self.name.split(" ", 1)
        return parts[1] if len(parts) > 1 else ""
