"""The three-stage model funnel.

Each stage is more expensive per item and sees fewer items than the last:

    stage 1  triage   Haiku 4.5   batched, ~12 postings per call, title + snippet
    stage 2  fit      Sonnet 5    one call per posting, full description
    stage 3  draft    Opus 5      one call per finalist, writes the application

The shape matters more than the model names. A flat design that sent every
gate survivor to Opus would cost roughly 25x more for the same shortlist,
because the expensive model would spend most of its time rejecting postings
that a cheap one can reject just as well.

Two invariants hold across all three stages:

* every call is reserved against the spend cap before dispatch, so running out
  of budget truncates the funnel cleanly instead of failing mid-write;
* the per-stage system prompt is byte-identical across its calls, which makes
  it a stable cache prefix - stage 2 pays for the resume once, not 40 times.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

from .config import FunnelConfig, Profile, StageConfig
from .ledger import BudgetExceeded, SpendLedger
from .llm import LLM
from .models import (
    ApplicationDraft,
    FitAssessment,
    GateDecision,
    Posting,
    StageCount,
    TriageBatch,
)

MAX_DESCRIPTION_CHARS = 12_000
SNIPPET_CHARS = 600


# --------------------------------------------------------------------------
# Prompts
# --------------------------------------------------------------------------


def _profile_block(profile: Profile, resume: str) -> str:
    """The candidate description shared by every stage."""
    lines = [
        f"Name: {profile.name}",
        f"Target titles: {', '.join(profile.titles) or 'unspecified'}",
        f"Target seniority: {', '.join(profile.seniority) or 'unspecified'}",
        f"Core skills: {', '.join(profile.must_have_any) or 'unspecified'}",
        f"Also valuable: {', '.join(profile.nice_to_have) or 'none listed'}",
        f"Locations: {', '.join(profile.locations) or 'any'}"
        + (" (remote only)" if profile.remote_only else ""),
    ]
    if profile.min_base_salary:
        lines.append(f"Minimum base salary: ${profile.min_base_salary:,}")
    auth = profile.work_authorization
    lines.append(
        f"Work authorization: {auth.country}"
        + (", requires visa sponsorship" if auth.needs_sponsorship else ", no sponsorship needed")
    )
    if profile.exclude_keywords:
        lines.append(f"Hard avoids: {', '.join(profile.exclude_keywords)}")
    if profile.summary:
        lines.append(f"\nSummary: {profile.summary}")
    if resume:
        lines.append(f"\n--- RESUME ---\n{resume.strip()}")
    return "\n".join(lines)


TRIAGE_SYSTEM = """\
You are triaging job postings for one candidate. You will see a numbered batch \
of postings, each reduced to its title, company, location and the opening of its \
description.

For each posting decide whether it is worth a full, expensive review. Keep it \
when the role plausibly matches the candidate's target titles, level and core \
skills. Drop it when it is clearly a different discipline, a badly mismatched \
level, or an obvious mismatch on location or work authorization.

You are the cheap first pass, so bias toward keeping anything genuinely \
ambiguous - a later stage reads the full description and can reject properly. \
Drop only what you are confident about. Give a short reason either way, and a \
confidence from 0 to 1.

Return one result per posting, using the posting's `ref` number.

CANDIDATE
{profile}"""


FIT_SYSTEM = """\
You are assessing one job posting against one candidate, in detail.

Extract what the posting actually states - required seniority, years of \
experience, remote policy, visa sponsorship, salary range, must-have skills. \
Where the posting is silent, say so ("unclear") rather than inferring. Do not \
import assumptions from similar roles you have seen.

Then score fit from 0 to 100:
  85-100  strong match; the candidate clears the stated bar with evidence
  70-84   good match with one or two addressable gaps
  50-69   plausible but a real stretch on level, stack or domain
  0-49    mismatch

Be specific and be honest. List matched strengths only where the resume \
supports them, name the real gaps, and record anything that is an outright \
dealbreaker given the candidate's constraints. An inflated score costs the \
candidate a wasted application; an unfair low score costs them an opportunity.

CANDIDATE
{profile}"""


DRAFT_SYSTEM = """\
You are writing an application for one job the candidate is a strong match for.

Produce:
  * A cover letter, 180-280 words. Specific to this posting and this company. \
Lead with the single most relevant thing the candidate has actually done. No \
generic enthusiasm, no restating the job description back, no invented facts - \
every claim must be traceable to the resume below.
  * Up to 5 resume bullets rewritten to foreground what this posting asks for. \
Same underlying facts, different emphasis. Never add achievements, numbers or \
technologies that are not already in the resume.
  * Answers to any screening questions the posting asks for explicitly.
  * Short tailoring notes for the candidate: what to emphasise if they get a \
call, and what gap to be ready to address.

Set `proceed` to false if, having read the posting closely, you think this \
application would be a waste of the candidate's time - a hidden dealbreaker, a \
level mismatch the earlier stage missed. That is a recommendation only; whether \
anything is transmitted is decided elsewhere.

CANDIDATE
{profile}"""


def _posting_block(posting: Posting, limit: int = MAX_DESCRIPTION_CHARS) -> str:
    description = posting.description[:limit]
    if len(posting.description) > limit:
        description += "\n[description truncated]"
    parts = [
        f"Title: {posting.title}",
        f"Company: {posting.company}",
        f"Location: {posting.location or 'unspecified'}",
        f"Source: {posting.source}",
        f"URL: {posting.url}",
    ]
    if posting.compensation_raw:
        parts.append(f"Stated compensation: {posting.compensation_raw}")
    if posting.posted_at:
        parts.append(f"Posted: {posting.posted_at.date().isoformat()}")
    parts.append(f"\nDescription:\n{description}")
    return "\n".join(parts)


# --------------------------------------------------------------------------
# Results
# --------------------------------------------------------------------------


@dataclass
class Candidate:
    """A posting travelling through the funnel, accumulating verdicts."""

    posting: Posting
    gate: GateDecision
    assessment: FitAssessment | None = None
    draft: ApplicationDraft | None = None


@dataclass
class FunnelResult:
    finalists: list[Candidate] = field(default_factory=list)
    assessed: list[Candidate] = field(default_factory=list)
    """Everything stage 2 scored. Persisting all of these - not only the
    finalists - is what stops the next run paying to re-judge them."""

    stages: list[StageCount] = field(default_factory=list)
    budget_exhausted: bool = False
    errors: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------
# The funnel
# --------------------------------------------------------------------------


class Funnel:
    def __init__(
        self,
        llm: LLM,
        config: FunnelConfig,
        profile: Profile,
        resume: str = "",
        ledger: SpendLedger | None = None,
    ) -> None:
        self.llm = llm
        self.config = config
        self.profile = profile
        self.resume = resume
        self.ledger = ledger or llm.ledger
        self._profile_text = _profile_block(profile, resume)

    def run(self, entries: Sequence[tuple[Posting, GateDecision]]) -> FunnelResult:
        result = FunnelResult()
        candidates = [Candidate(posting=p, gate=d) for p, d in entries]

        kept, stage = self._triage(candidates, result)
        result.stages.append(stage)
        if self.ledger.exhausted:
            result.budget_exhausted = True

        assessed, stage = self._assess(kept, result)
        result.assessed = assessed
        result.stages.append(stage)
        if self.ledger.exhausted:
            result.budget_exhausted = True

        finalists, stage = self._draft(assessed, result)
        result.stages.append(stage)
        result.finalists = finalists
        if self.ledger.exhausted:
            result.budget_exhausted = True
        return result

    # -- stage 1 -----------------------------------------------------------

    def _triage(
        self, candidates: list[Candidate], result: FunnelResult
    ) -> tuple[list[Candidate], StageCount]:
        cfg: StageConfig = self.config.triage
        count = StageCount(name="triage", model=cfg.model, considered=len(candidates))
        if not candidates:
            return [], count

        system = TRIAGE_SYSTEM.format(profile=self._profile_text)
        batch_size = cfg.batch_size or 12
        kept: list[Candidate] = []

        for start in range(0, len(candidates), batch_size):
            batch = candidates[start : start + batch_size]
            user = "\n\n".join(
                f"### ref {i}\n"
                f"Title: {c.posting.title}\n"
                f"Company: {c.posting.company}\n"
                f"Location: {c.posting.location or 'unspecified'}\n"
                f"Opening: {c.posting.description[:SNIPPET_CHARS]}"
                for i, c in enumerate(batch)
            )
            try:
                result = self.llm.structured(
                    stage="triage",
                    model=cfg.model,
                    system=system,
                    user=user,
                    schema=TriageBatch,
                    max_tokens=cfg.max_tokens,
                    thinking=cfg.thinking,
                    effort=cfg.effort,
                )
            except BudgetExceeded:
                count.skipped_for_budget += len(candidates) - start
                break
            except Exception as exc:  # noqa: BLE001 - one bad batch is not fatal
                count.skipped_for_budget += len(batch)
                result.errors.append(self._note(exc, f"triage batch at {start}"))
                continue

            count.cost_usd += result.cost_usd
            verdicts = {item.ref: item for item in result.value.results}
            for offset, candidate in enumerate(batch):
                verdict = verdicts.get(offset)
                # A posting the model forgot to rule on survives rather than
                # silently disappearing - omission is not rejection.
                if verdict is None or verdict.keep:
                    kept.append(candidate)

        count.advanced = len(kept)
        return kept, count

    # -- stage 2 -----------------------------------------------------------

    def _assess(
        self, candidates: list[Candidate], result: FunnelResult
    ) -> tuple[list[Candidate], StageCount]:
        cfg: StageConfig = self.config.fit
        queue = candidates[: cfg.keep_top] if cfg.keep_top else candidates
        count = StageCount(name="fit", model=cfg.model, considered=len(queue))
        if not queue:
            return [], count

        system = FIT_SYSTEM.format(profile=self._profile_text)
        assessed: list[Candidate] = []

        for index, candidate in enumerate(queue):
            try:
                outcome = self.llm.structured(
                    stage="fit",
                    model=cfg.model,
                    system=system,
                    user=_posting_block(candidate.posting),
                    schema=FitAssessment,
                    max_tokens=cfg.max_tokens,
                    thinking=cfg.thinking,
                    effort=cfg.effort,
                )
            except BudgetExceeded:
                count.skipped_for_budget += len(queue) - index
                break
            except Exception as exc:  # noqa: BLE001
                count.skipped_for_budget += 1
                result.errors.append(self._note(exc, f"fit {candidate.posting.key}"))
                continue

            count.cost_usd += outcome.cost_usd
            candidate.assessment = outcome.value
            assessed.append(candidate)

        assessed.sort(key=lambda c: c.assessment.fit_score if c.assessment else 0, reverse=True)
        count.advanced = len(assessed)
        return assessed, count

    # -- stage 3 -----------------------------------------------------------

    def _draft(
        self, candidates: list[Candidate], result: FunnelResult
    ) -> tuple[list[Candidate], StageCount]:
        cfg: StageConfig = self.config.draft
        eligible = [
            c
            for c in candidates
            if c.assessment
            and not c.assessment.dealbreakers
            and (cfg.min_score is None or c.assessment.fit_score >= cfg.min_score)
        ]
        queue = eligible[: cfg.keep_top] if cfg.keep_top else eligible
        count = StageCount(name="draft", model=cfg.model, considered=len(queue))
        if not queue:
            return [], count

        system = DRAFT_SYSTEM.format(profile=self._profile_text)
        finalists: list[Candidate] = []

        for index, candidate in enumerate(queue):
            assessment = candidate.assessment
            assert assessment is not None  # guaranteed by the filter above
            user = (
                _posting_block(candidate.posting)
                + "\n\n--- PRIOR ASSESSMENT ---\n"
                + f"Fit score: {assessment.fit_score}\n"
                + f"Strengths: {', '.join(assessment.matched_strengths) or 'none recorded'}\n"
                + f"Gaps: {', '.join(assessment.gaps) or 'none recorded'}\n"
                + f"Summary: {assessment.summary}"
            )
            try:
                outcome = self.llm.structured(
                    stage="draft",
                    model=cfg.model,
                    system=system,
                    user=user,
                    schema=ApplicationDraft,
                    max_tokens=cfg.max_tokens,
                    thinking=cfg.thinking,
                    effort=cfg.effort,
                )
            except BudgetExceeded:
                count.skipped_for_budget += len(queue) - index
                break
            except Exception as exc:  # noqa: BLE001
                count.skipped_for_budget += 1
                result.errors.append(self._note(exc, f"draft {candidate.posting.key}"))
                continue

            count.cost_usd += outcome.cost_usd
            candidate.draft = outcome.value
            finalists.append(candidate)

        count.advanced = len(finalists)
        return finalists, count

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _note(exc: Exception, where: str) -> str:
        return f"{where}: {type(exc).__name__}: {exc}"
