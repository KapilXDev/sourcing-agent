"""Orchestration: discover -> dedupe -> gate -> funnel -> route.

The agent owns sequencing and failure containment. Nothing else in the system
knows about more than one stage, and no single source, posting or model call is
allowed to abort a run - a source that 500s, a posting that fails to parse and
a stage that runs out of budget are all recorded and stepped over.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Iterable, Sequence

from .capabilities import Capability
from .config import Settings
from .connectors import REGISTRY, Fetcher
from .connectors.registry import submit_capable_slugs
from .connectors.base import Connector
from .funnel import Candidate, Funnel
from .gate import DecisionGate
from .ledger import SpendLedger
from .llm import LLM, Backend, default_backend
from .models import ApplicationPacket, GateDecision, Posting, RunReport, StageCount
from .store import Store
from .submitter import Submitter

# A posting rejected *only* for these reasons might pass once its body is
# fetched - they are the rules that depend on description text.
_RESCUABLE = ("content:too_thin", "keywords:insufficient")


class SourcingAgent:
    def __init__(
        self,
        settings: Settings,
        store: Store | None = None,
        backend: Backend | None = None,
        run_id: str | None = None,
    ) -> None:
        self.settings = settings
        self.store = store or Store(settings.resolve(settings.db_path))
        self.run_id = run_id or f"run-{datetime.now(timezone.utc):%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:6]}"
        self._backend = backend
        self.fetcher = Fetcher(
            fixtures_dir=settings.resolve(settings.fixtures_dir),
            offline=settings.offline,
        )
        self._connectors: dict[str, Connector] = {}

    # -- connectors --------------------------------------------------------

    def enabled_sources(self, only: Sequence[str] | None = None) -> list[str]:
        slugs = [
            slug
            for slug, cfg in self.settings.sources.items()
            if cfg.enabled and slug in REGISTRY
        ]
        if only:
            requested = set(only)
            unknown = requested - set(REGISTRY)
            if unknown:
                raise KeyError(f"unknown source(s): {', '.join(sorted(unknown))}")
            slugs = [s for s in REGISTRY if s in requested]
        return slugs

    def connector(self, slug: str) -> Connector:
        if slug not in self._connectors:
            cls = REGISTRY[slug]
            self._connectors[slug] = cls(self.settings.source(slug), self.fetcher)
        return self._connectors[slug]

    # -- stage: discovery --------------------------------------------------

    def discover(
        self, only: Sequence[str] | None = None, errors: list[str] | None = None
    ) -> list[Posting]:
        errors = errors if errors is not None else []
        found: list[Posting] = []
        for slug in self.enabled_sources(only):
            if slug not in self.settings.sources:
                # Explicitly requested but unconfigured: the ATS connectors need
                # board tokens, so this would otherwise return nothing at all
                # and look like "the source had no jobs".
                errors.append(
                    f"{slug}: no `sources.{slug}` block in this profile; "
                    "it will return nothing"
                )
            try:
                postings = list(self.connector(slug).discover())
            except Exception as exc:  # noqa: BLE001 - a dead source is not fatal
                errors.append(f"{slug}: {type(exc).__name__}: {exc}")
                continue
            found.extend(postings)
        return found

    # -- stage: gate -------------------------------------------------------

    def gate_postings(
        self, postings: Iterable[Posting], errors: list[str] | None = None
    ) -> tuple[list[tuple[Posting, GateDecision]], list[tuple[Posting, GateDecision]]]:
        errors = errors if errors is not None else []
        gate = DecisionGate(
            profile=self.settings.profile,
            config=self.settings.gate,
            seen_keys=self.store.recently_assessed(self.settings.gate.skip_seen_days),
        )
        passed, rejected = gate.partition(postings)

        # Rescue pass: some sources (SmartRecruiters, Workday) return a list
        # without bodies. Fetching every body up front would multiply the crawl;
        # fetching only for postings that failed on body-dependent rules alone
        # keeps the extra requests proportional to genuine near-misses.
        still_rejected: list[tuple[Posting, GateDecision]] = []
        for posting, decision in rejected:
            if not self._is_rescuable(posting, decision):
                still_rejected.append((posting, decision))
                continue
            try:
                hydrated = self.connector(posting.source).fetch_detail(posting)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{posting.key}: detail fetch failed: {exc}")
                still_rejected.append((posting, decision))
                continue
            if hydrated.description == posting.description:
                still_rejected.append((posting, decision))
                continue
            self.store.upsert_postings([hydrated])
            retry = gate.evaluate(hydrated)
            if retry.passed:
                passed.append((hydrated, retry))
            else:
                still_rejected.append((hydrated, retry))

        passed.sort(key=lambda pair: pair[1].prefilter_score, reverse=True)
        for posting, decision in passed + still_rejected:
            self.store.record_decision(self.run_id, posting.key, decision)
        return passed, still_rejected

    def _is_rescuable(self, posting: Posting, decision: GateDecision) -> bool:
        if posting.description:
            return False
        cls = REGISTRY.get(posting.source)
        if cls is None or Capability.FETCH_DETAIL not in cls.capabilities:
            return False
        return all(r.startswith(_RESCUABLE) for r in decision.rejections)

    # -- the whole run -----------------------------------------------------

    def run(self, only: Sequence[str] | None = None, skip_funnel: bool = False) -> RunReport:
        started = datetime.now(timezone.utc)
        report = RunReport(
            run_id=self.run_id,
            started_at=started,
            budget_usd=self.settings.budget.cap_usd,
        )
        self.store.start_run(self.run_id, self.settings.budget.cap_usd)

        raw = self.discover(only, report.errors)
        report.discovered = len(raw)

        postings = self.store.dedupe(raw, submit_capable_slugs())
        report.deduped = len(raw) - len(postings)
        self.store.upsert_postings(postings)

        passed, rejected = self.gate_postings(postings, report.errors)
        report.gate_passed = len(passed)
        report.gate_rejected = len(rejected)

        queue = passed[: self.settings.gate.max_to_funnel]
        ledger = SpendLedger(
            self.store,
            self.run_id,
            cap_usd=self.settings.budget.cap_usd,
            headroom=self.settings.budget.reserve_headroom,
        )

        if skip_funnel or not queue:
            report.stages = [
                StageCount(name=name, considered=0)
                for name in ("triage", "fit", "draft")
            ]
            return self._finish(report, ledger)

        try:
            return self._funnel_and_route(report, queue, ledger)
        finally:
            # The run row is closed even if a stage raises, so a crashed run is
            # still visible in `sourcing-agent budget` instead of hanging open.
            self._finish(report, ledger)

    def _funnel_and_route(self, report: RunReport, queue, ledger: SpendLedger) -> RunReport:
        funnel = Funnel(
            llm=LLM(self._resolve_backend(), ledger),
            config=self.settings.funnel,
            profile=self.settings.profile,
            resume=self.settings.profile.resolved_resume_text(self.settings.base_dir),
            ledger=ledger,
        )
        outcome = funnel.run(queue)
        report.stages = outcome.stages
        report.errors.extend(outcome.errors)
        report.budget_exhausted = outcome.budget_exhausted

        # Every assessment is recorded, not just the ones that became
        # applications - a posting stage 2 has already judged should not be
        # paid for again on the next run.
        for candidate in outcome.assessed:
            if candidate.assessment is not None:
                self.store.record_assessment(
                    self.run_id, candidate.posting.key, candidate.assessment
                )

        report.receipts = self._route(outcome.finalists)
        return report

    # -- stage: routing ----------------------------------------------------

    def _route(self, finalists: Sequence[Candidate]) -> list:
        submitter = Submitter(
            config=self.settings.submission,
            profile=self.settings.profile,
            store=self.store,
            out_dir=self.settings.resolve(self.settings.out_dir),
            run_id=self.run_id,
            base_dir=self.settings.base_dir,
        )

        receipts = []
        for candidate in finalists:
            if candidate.assessment is None or candidate.draft is None:
                continue
            connector = self.connector(candidate.posting.source)
            decision = submitter.decide(
                connector,
                packet_proceed=candidate.draft.proceed,
                posting_key=candidate.posting.key,
            )
            packet = ApplicationPacket(
                posting=candidate.posting,
                assessment=candidate.assessment,
                draft=candidate.draft,
                route=decision.route,
                route_reason=decision.reason,
            )
            receipts.append(submitter.dispatch(packet, connector))
        return receipts

    # -- helpers -----------------------------------------------------------

    def _resolve_backend(self) -> Backend:
        if self._backend is None:
            self._backend = default_backend()
        return self._backend

    def _finish(self, report: RunReport, ledger: SpendLedger) -> RunReport:
        report.spend_usd = ledger.spent
        report.finished_at = datetime.now(timezone.utc)
        self.store.finish_run(self.run_id, report.spend_usd, report.model_dump_json())
        self.fetcher.close()
        return report
