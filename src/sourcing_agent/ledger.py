"""The spend ledger: a hard cap, enforced before the call rather than after.

A budget checked after the fact is a report, not a cap. Every model call here
goes through :meth:`SpendLedger.reserve` first, which prices the *worst case* -
counted input tokens plus the full ``max_tokens`` of output, as if the model
were to run to its ceiling. If that worst case would not fit under the cap, the
call is refused and never dispatched.

The consequence is that the cap cannot be breached by an unexpectedly long
response, only approached. Actual usage is settled afterwards by
:meth:`commit`, and the difference between estimate and actual is kept in the
``spend`` table so the headroom can be tuned against real data.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .store import Store

# Per-MTok list prices, Anthropic first-party API.
CACHE_WRITE_MULTIPLIER = 1.25
CACHE_READ_MULTIPLIER = 0.10


@dataclass(frozen=True)
class Price:
    input_per_mtok: float
    output_per_mtok: float

    @property
    def cache_write_per_mtok(self) -> float:
        return self.input_per_mtok * CACHE_WRITE_MULTIPLIER

    @property
    def cache_read_per_mtok(self) -> float:
        return self.input_per_mtok * CACHE_READ_MULTIPLIER


PRICING: dict[str, Price] = {
    "claude-opus-5": Price(5.00, 25.00),
    "claude-sonnet-5": Price(2.00, 10.00),
    "claude-haiku-4-5": Price(1.00, 5.00),
    "claude-opus-4-8": Price(5.00, 25.00),
    "claude-fable-5": Price(10.00, 50.00),
}


class UnknownModel(KeyError):
    """A model with no price entry. Refusing to guess is the point."""


def price_for(model: str) -> Price:
    try:
        return PRICING[model]
    except KeyError as exc:
        raise UnknownModel(
            f"no price on file for {model!r}; add it to PRICING before spending on it"
        ) from exc


def cost_usd(
    model: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read: int = 0,
    cache_write: int = 0,
) -> float:
    p = price_for(model)
    return (
        input_tokens * p.input_per_mtok
        + output_tokens * p.output_per_mtok
        + cache_read * p.cache_read_per_mtok
        + cache_write * p.cache_write_per_mtok
    ) / 1_000_000


class BudgetExceeded(RuntimeError):
    """The next call would breach the cap. Raised *before* dispatch."""

    def __init__(self, stage: str, model: str, estimate: float, remaining: float) -> None:
        super().__init__(
            f"{stage}/{model}: estimated ${estimate:.4f} exceeds remaining "
            f"${remaining:.4f}"
        )
        self.stage = stage
        self.model = model
        self.estimate = estimate
        self.remaining = remaining


@dataclass
class Reservation:
    stage: str
    model: str
    input_tokens: int
    max_output_tokens: int
    estimated_usd: float
    committed: bool = field(default=False, repr=False)


class SpendLedger:
    """Tracks and caps spend for one run."""

    def __init__(
        self,
        store: Store,
        run_id: str,
        cap_usd: float,
        headroom: float = 1.0,
    ) -> None:
        self.store = store
        self.run_id = run_id
        self.cap_usd = cap_usd
        self.headroom = headroom
        self._spent = store.run_spend(run_id)
        self._reserved = 0.0
        self.exhausted = False

    # -- accounting --------------------------------------------------------

    @property
    def spent(self) -> float:
        return self._spent

    @property
    def committed_and_reserved(self) -> float:
        return self._spent + self._reserved

    @property
    def remaining(self) -> float:
        return max(0.0, self.cap_usd - self.committed_and_reserved)

    def can_afford(self, estimate: float) -> bool:
        return estimate * self.headroom <= self.remaining

    # -- the gate on every call -------------------------------------------

    def estimate(self, model: str, input_tokens: int, max_output_tokens: int) -> float:
        """Worst-case price of a call: full input, output run to its ceiling."""
        return cost_usd(model, input_tokens=input_tokens, output_tokens=max_output_tokens)

    def reserve(
        self, stage: str, model: str, input_tokens: int, max_output_tokens: int
    ) -> Reservation:
        est = self.estimate(model, input_tokens, max_output_tokens)
        if not self.can_afford(est):
            self.exhausted = True
            raise BudgetExceeded(stage, model, est * self.headroom, self.remaining)
        self._reserved += est
        return Reservation(
            stage=stage,
            model=model,
            input_tokens=input_tokens,
            max_output_tokens=max_output_tokens,
            estimated_usd=est,
        )

    def commit(self, reservation: Reservation, usage: Any = None) -> float:
        """Settle a reservation against real usage and persist it."""
        if reservation.committed:
            raise RuntimeError("reservation already committed")

        in_tok = _usage_int(usage, "input_tokens", reservation.input_tokens)
        out_tok = _usage_int(usage, "output_tokens", reservation.max_output_tokens)
        cache_read = _usage_int(usage, "cache_read_input_tokens", 0)
        cache_write = _usage_int(usage, "cache_creation_input_tokens", 0)

        actual = cost_usd(
            reservation.model,
            input_tokens=in_tok,
            output_tokens=out_tok,
            cache_read=cache_read,
            cache_write=cache_write,
        )

        self._reserved = max(0.0, self._reserved - reservation.estimated_usd)
        self._spent += actual
        reservation.committed = True

        self.store.record_spend(
            run_id=self.run_id,
            stage=reservation.stage,
            model=reservation.model,
            estimated_usd=reservation.estimated_usd,
            actual_usd=actual,
            input_tokens=in_tok,
            output_tokens=out_tok,
            cache_read=cache_read,
            cache_write=cache_write,
        )
        if self._spent >= self.cap_usd:
            self.exhausted = True
        return actual

    def release(self, reservation: Reservation) -> None:
        """Give back a reservation whose call failed before billing."""
        if not reservation.committed:
            self._reserved = max(0.0, self._reserved - reservation.estimated_usd)
            reservation.committed = True


def _usage_int(usage: Any, attr: str, default: int) -> int:
    if usage is None:
        return default
    value = getattr(usage, attr, None)
    if value is None and isinstance(usage, dict):
        value = usage.get(attr)
    return int(value) if value is not None else (0 if attr.startswith("cache") else default)
