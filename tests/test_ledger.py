"""The cap must hold under adversarial conditions, not just typical ones."""

from __future__ import annotations

import pytest

from sourcing_agent.ledger import (
    BudgetExceeded,
    PRICING,
    SpendLedger,
    UnknownModel,
    cost_usd,
    price_for,
)
from sourcing_agent.llm import LLM, Call, ScriptedBackend, ScriptedUsage
from sourcing_agent.models import TriageBatch, TriageItem
from sourcing_agent.store import Store


@pytest.fixture
def ledger(store: Store) -> SpendLedger:
    return SpendLedger(store, run_id="r1", cap_usd=1.00)


def test_pricing_matches_published_rates():
    assert price_for("claude-opus-5") == PRICING["claude-opus-5"]
    assert PRICING["claude-opus-5"].input_per_mtok == 5.00
    assert PRICING["claude-sonnet-5"].output_per_mtok == 10.00
    assert PRICING["claude-haiku-4-5"].input_per_mtok == 1.00


def test_unpriced_model_is_refused_not_guessed():
    with pytest.raises(UnknownModel):
        price_for("claude-something-new")


def test_cache_reads_are_cheaper_than_fresh_input():
    fresh = cost_usd("claude-sonnet-5", input_tokens=100_000)
    cached = cost_usd("claude-sonnet-5", cache_read=100_000)
    assert cached == pytest.approx(fresh * 0.10)


def test_estimate_prices_the_worst_case(ledger):
    """The reservation assumes output runs to max_tokens - that is what makes
    the cap a cap rather than a hope."""
    estimate = ledger.estimate("claude-opus-5", input_tokens=10_000, max_output_tokens=8_000)
    assert estimate == pytest.approx((10_000 * 5 + 8_000 * 25) / 1_000_000)


def test_reserve_refuses_before_dispatch(ledger):
    with pytest.raises(BudgetExceeded) as exc:
        ledger.reserve("draft", "claude-opus-5", input_tokens=1_000_000, max_output_tokens=100_000)
    assert exc.value.stage == "draft"
    assert ledger.spent == 0.0, "a refused call must not be billed"


def test_reservations_accumulate_before_commit(ledger):
    ledger.reserve("fit", "claude-sonnet-5", 10_000, 4_000)
    ledger.reserve("fit", "claude-sonnet-5", 10_000, 4_000)
    assert ledger.remaining < ledger.cap_usd
    assert ledger.spent == 0.0


def test_commit_settles_against_actual_usage(ledger, store):
    reservation = ledger.reserve("fit", "claude-sonnet-5", 10_000, 4_000)
    actual = ledger.commit(reservation, ScriptedUsage(input_tokens=10_000, output_tokens=500))
    assert actual == pytest.approx((10_000 * 2 + 500 * 10) / 1_000_000)
    assert actual < reservation.estimated_usd, "real output is under the ceiling"
    assert store.run_spend("r1") == pytest.approx(actual)


def test_a_reservation_cannot_be_committed_twice(ledger):
    reservation = ledger.reserve("fit", "claude-sonnet-5", 100, 100)
    ledger.commit(reservation, None)
    with pytest.raises(RuntimeError):
        ledger.commit(reservation, None)


def test_release_returns_headroom_when_a_call_fails(ledger):
    reservation = ledger.reserve("draft", "claude-opus-5", 10_000, 8_000)
    before = ledger.remaining
    ledger.release(reservation)
    assert ledger.remaining > before
    assert ledger.spent == 0.0


def test_headroom_multiplier_tightens_the_cap(store):
    tight = SpendLedger(store, "r2", cap_usd=0.05, headroom=1.5)
    loose = SpendLedger(store, "r3", cap_usd=0.05, headroom=1.0)
    args = ("fit", "claude-sonnet-5", 10_000, 3_000)
    loose.reserve(*args)
    with pytest.raises(BudgetExceeded):
        tight.reserve(*args)


def test_spend_resumes_from_the_database(store):
    first = SpendLedger(store, "r4", cap_usd=1.00)
    first.commit(first.reserve("fit", "claude-sonnet-5", 10_000, 1_000), None)
    spent = first.spent

    resumed = SpendLedger(store, "r4", cap_usd=1.00)
    assert resumed.spent == pytest.approx(spent)
    assert resumed.remaining == pytest.approx(1.00 - spent)


def test_the_cap_holds_across_a_long_run(store):
    """Hammer the ledger with calls that would each fit but together would not."""
    ledger = SpendLedger(store, "r5", cap_usd=0.10)
    refusals = 0
    for _ in range(500):
        try:
            reservation = ledger.reserve("fit", "claude-sonnet-5", 5_000, 2_000)
        except BudgetExceeded:
            refusals += 1
            continue
        ledger.commit(reservation, ScriptedUsage(input_tokens=5_000, output_tokens=2_000))
    assert refusals > 0
    assert ledger.spent <= 0.10


def test_llm_never_dispatches_a_call_it_cannot_afford(store):
    """The budget check sits in front of the backend, not behind it."""
    ledger = SpendLedger(store, "r6", cap_usd=0.0001)
    backend = ScriptedBackend(
        handler=lambda call: TriageBatch(
            results=[TriageItem(ref=0, keep=True, reason="x", confidence=1.0)]
        )
    )
    llm = LLM(backend, ledger)

    with pytest.raises(BudgetExceeded):
        llm.structured(
            stage="triage",
            model="claude-opus-5",
            system="s" * 4000,
            user="u" * 4000,
            schema=TriageBatch,
            max_tokens=8000,
        )
    assert backend.calls == [], "the backend must never have been reached"


def test_llm_commits_real_usage_after_a_successful_call(store):
    ledger = SpendLedger(store, "r7", cap_usd=1.00)
    backend = ScriptedBackend(
        handler=lambda call: TriageBatch(
            results=[TriageItem(ref=0, keep=True, reason="x", confidence=1.0)]
        ),
        output_tokens=250,
    )
    result = LLM(backend, ledger).structured(
        stage="triage",
        model="claude-haiku-4-5",
        system="system prompt",
        user="user prompt",
        schema=TriageBatch,
        max_tokens=1000,
    )
    assert isinstance(result.value, TriageBatch)
    assert result.output_tokens == 250
    assert ledger.spent == pytest.approx(result.cost_usd)
    assert store.spend_by_stage("r7") == {"triage": pytest.approx(result.cost_usd)}


def test_a_backend_failure_releases_the_reservation(store):
    ledger = SpendLedger(store, "r8", cap_usd=1.00)

    def explode(call: Call):
        raise RuntimeError("API is down")

    llm = LLM(ScriptedBackend(handler=explode), ledger)
    with pytest.raises(RuntimeError):
        llm.structured(
            stage="fit",
            model="claude-sonnet-5",
            system="s",
            user="u",
            schema=TriageBatch,
            max_tokens=1000,
        )
    assert ledger.spent == 0.0
    assert ledger.remaining == pytest.approx(1.00)
