"""Store, funnel and the whole run, end to end and offline."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from sourcing_agent.agent import SourcingAgent
from sourcing_agent.capabilities import Route
from sourcing_agent.funnel import Funnel
from sourcing_agent.ledger import SpendLedger
from sourcing_agent.llm import LLM, ScriptedBackend
from sourcing_agent.models import FitAssessment, GateDecision, Receipt
from sourcing_agent.store import AlreadySubmitted, Store
from tests.conftest import make_posting, scripted_handler


# --------------------------------------------------------------------------
# Store
# --------------------------------------------------------------------------


def test_upsert_is_idempotent(store: Store):
    posting = make_posting()
    store.upsert_postings([posting])
    store.upsert_postings([posting])
    assert len(store.all_postings()) == 1


def test_upsert_keeps_the_longer_description(store: Store):
    stub = make_posting(description="x" * 300)
    full = make_posting(description="y" * 900)
    store.upsert_postings([full])
    store.upsert_postings([stub])
    assert len(store.get_posting(full.key).description) == 900


def test_upsert_counts_cross_source_duplicates(store: Store):
    greenhouse = make_posting(source="greenhouse", source_id="1")
    linkedin = make_posting(source="linkedin", source_id="9")
    store.upsert_postings([greenhouse])
    _, duplicates = store.upsert_postings([linkedin])
    assert duplicates == 1


def test_dedupe_prefers_the_richest_copy(store: Store):
    stub = make_posting(source="linkedin", source_id="9", description="short but long enough")
    full = make_posting(source="greenhouse", source_id="1")
    kept = store.dedupe([stub, full])
    assert len(kept) == 1
    assert kept[0].source == "greenhouse"


def test_recently_assessed_window(store: Store):
    posting = make_posting()
    store.record_assessment("r1", posting.key, _assessment())
    assert posting.key in store.recently_assessed(within_days=14)
    assert "greenhouse:never-seen" not in store.recently_assessed(within_days=14)

    stale = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat()
    store.conn.execute(
        "UPDATE assessments SET created_at = ? WHERE posting_key = ?", (stale, posting.key)
    )
    store.conn.commit()
    assert posting.key not in store.recently_assessed(within_days=14)


def test_a_posting_can_only_be_submitted_once(store: Store):
    """Enforced by a partial unique index, not by application logic."""
    receipt = Receipt(
        submitted=True,
        route=Route.SUBMIT,
        connector="greenhouse",
        posting_key="greenhouse:1",
        detail="ok",
    )
    store.record_receipt("r1", receipt)
    assert store.has_submitted("greenhouse:1")
    with pytest.raises(AlreadySubmitted):
        store.record_receipt("r2", receipt)


def test_dry_runs_and_exports_do_not_block_a_later_submission(store: Store):
    base = dict(connector="greenhouse", posting_key="greenhouse:1", detail="")
    store.record_receipt("r1", Receipt(submitted=False, route=Route.EXPORT, **base))
    store.record_receipt(
        "r1", Receipt(submitted=True, route=Route.SUBMIT, dry_run=True, **base)
    )
    assert not store.has_submitted("greenhouse:1")
    store.record_receipt("r2", Receipt(submitted=True, route=Route.SUBMIT, **base))
    assert store.has_submitted("greenhouse:1")


# --------------------------------------------------------------------------
# Funnel
# --------------------------------------------------------------------------


def _assessment(**overrides) -> FitAssessment:
    base = dict(
        fit_score=88,
        seniority="senior",
        remote_policy="remote",
        sponsorship="unclear",
        must_have_skills=[],
        matched_strengths=[],
        gaps=[],
        dealbreakers=[],
        summary="s",
    )
    base.update(overrides)
    return FitAssessment(**base)


def build_funnel(store, settings, handler=None, cap=5.0):
    ledger = SpendLedger(store, "run", cap_usd=cap)
    backend = ScriptedBackend(handler=handler or scripted_handler())
    funnel = Funnel(
        llm=LLM(backend, ledger),
        config=settings.funnel,
        profile=settings.profile,
        resume="Seven years of backend work.",
        ledger=ledger,
    )
    return funnel, backend, ledger


def entries(n: int):
    return [
        (make_posting(source_id=str(i), title=f"Senior Backend Engineer {i}"), GateDecision(passed=True))
        for i in range(n)
    ]


def test_the_funnel_narrows_at_every_stage(store, settings):
    settings.funnel.fit.keep_top = 6
    settings.funnel.draft.keep_top = 2
    funnel, backend, _ = build_funnel(store, settings)

    result = funnel.run(entries(20))
    triage, fit, draft = result.stages

    assert triage.considered == 20 and triage.advanced == 20
    assert fit.considered == 6, "keep_top caps what reaches the middle stage"
    assert draft.considered == 2
    assert len(result.finalists) == 2
    assert triage.model == "claude-haiku-4-5"
    assert fit.model == "claude-sonnet-5"
    assert draft.model == "claude-opus-5"


def test_triage_batches_rather_than_calling_per_posting(store, settings):
    settings.funnel.triage.batch_size = 5
    funnel, backend, _ = build_funnel(store, settings)
    funnel.run(entries(20))
    triage_calls = [c for c in backend.calls if c.stage == "triage"]
    assert len(triage_calls) == 4, "20 postings at batch_size 5"


def test_dropped_postings_do_not_reach_the_expensive_stages(store, settings):
    funnel, backend, _ = build_funnel(store, settings, handler=scripted_handler(keep=False))
    result = funnel.run(entries(12))
    assert result.stages[0].advanced == 0
    assert not [c for c in backend.calls if c.stage in {"fit", "draft"}]
    assert result.finalists == []


def test_a_posting_the_model_forgot_to_rule_on_survives(store, settings):
    """Omission is not rejection - a silent drop is the worst failure mode."""
    from sourcing_agent.models import TriageBatch, TriageItem

    def partial(call):
        if call.schema is TriageBatch:
            # Rules on only the first two of the batch.
            return TriageBatch(
                results=[
                    TriageItem(ref=0, keep=False, reason="no", confidence=0.9),
                    TriageItem(ref=1, keep=True, reason="yes", confidence=0.9),
                ]
            )
        return scripted_handler()(call)

    funnel, _, _ = build_funnel(store, settings, handler=partial)
    result = funnel.run(entries(5))
    assert result.stages[0].advanced == 4, "one explicit drop, four survivors"


def test_low_scores_never_reach_the_draft_stage(store, settings):
    settings.funnel.draft.min_score = 70
    funnel, backend, _ = build_funnel(store, settings, handler=scripted_handler(fit_score=55))
    result = funnel.run(entries(4))
    assert result.stages[2].considered == 0
    assert not [c for c in backend.calls if c.stage == "draft"]


def test_dealbreakers_stop_a_high_scorer(store, settings):
    handler = scripted_handler(fit_score=95, dealbreakers=["requires relocation to Zurich"])
    funnel, _, _ = build_funnel(store, settings, handler=handler)
    result = funnel.run(entries(3))
    assert result.stages[2].considered == 0


def test_running_out_of_budget_truncates_cleanly(store, settings):
    """A run that hits the cap must still report, not raise."""
    funnel, backend, ledger = build_funnel(store, settings, cap=0.02)
    result = funnel.run(entries(40))

    assert ledger.spent <= 0.02
    assert result.budget_exhausted
    assert any(s.skipped_for_budget for s in result.stages)
    # Whatever did complete is still usable.
    assert all(c.assessment is not None for c in result.finalists)


def test_spend_is_attributed_per_stage(store, settings):
    funnel, _, _ = build_funnel(store, settings)
    result = funnel.run(entries(10))
    by_stage = store.spend_by_stage("run")
    assert set(by_stage) == {"triage", "fit", "draft"}
    assert by_stage["draft"] > 0
    for stage in result.stages:
        assert stage.cost_usd == pytest.approx(by_stage[stage.name], rel=1e-6)


def test_a_failing_stage_call_does_not_abort_the_run(store, settings):
    calls = {"n": 0}

    def flaky(call):
        if call.schema is FitAssessment:
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("transient 500")
        return scripted_handler()(call)

    funnel, _, _ = build_funnel(store, settings, handler=flaky)
    result = funnel.run(entries(4))
    assert result.stages[1].advanced == 3
    assert result.stages[1].skipped_for_budget == 1
    assert any("transient 500" in e for e in result.errors)


# --------------------------------------------------------------------------
# Full run
# --------------------------------------------------------------------------


def test_full_offline_run(settings, backend):
    agent = SourcingAgent(settings, backend=backend)
    report = agent.run()

    assert report.discovered == 31
    assert report.deduped == 1, "the same role on Greenhouse and Remotive"
    assert report.gate_passed == 18
    assert report.gate_rejected == 12
    assert [s.name for s in report.stages] == ["triage", "fit", "draft"]
    assert report.spend_usd > 0
    assert report.spend_usd <= report.budget_usd
    assert report.receipts
    assert not report.errors


def test_a_run_with_submission_off_exports_everything(settings, backend):
    report = SourcingAgent(settings, backend=backend).run()
    assert all(r.route is Route.EXPORT for r in report.receipts)
    assert all(not r.submitted for r in report.receipts)

    written = list((settings.out_dir / "applications").glob("*.md"))
    assert len(written) == len(report.receipts)


def test_no_funnel_run_spends_nothing(settings):
    """`--no-funnel` exercises 13 sources and the gate for exactly $0."""
    agent = SourcingAgent(settings, backend=None)
    report = agent.run(skip_funnel=True)
    assert report.spend_usd == 0.0
    assert report.gate_passed > 0
    assert all(s.considered == 0 for s in report.stages)


def test_a_dead_source_does_not_abort_the_run(settings, backend, monkeypatch):
    from sourcing_agent.connectors.greenhouse import GreenhouseConnector

    def explode(self):
        raise RuntimeError("boards-api is down")

    monkeypatch.setattr(GreenhouseConnector, "discover", explode)
    report = SourcingAgent(settings, backend=backend).run()

    assert any("greenhouse" in e for e in report.errors)
    assert report.discovered > 0, "the other twelve sources still ran"


def test_a_second_run_skips_recently_assessed_postings(settings, backend):
    store = Store(settings.db_path)
    first = SourcingAgent(settings, store=store, backend=backend).run()
    assert first.gate_passed > 0

    second = SourcingAgent(settings, store=store, backend=backend, run_id="second").run()
    assert second.gate_passed == 0, "everything assessed was skipped by the gate"
    assert second.spend_usd == 0.0


def test_the_run_report_is_persisted_and_reloadable(settings, backend):
    import json

    store = Store(settings.db_path)
    report = SourcingAgent(settings, store=store, backend=backend).run()
    row = store.last_runs(1)[0]
    reloaded = json.loads(row["report_json"])
    assert reloaded["run_id"] == report.run_id
    assert reloaded["gate_passed"] == report.gate_passed
    assert row["spend_usd"] == pytest.approx(report.spend_usd)
