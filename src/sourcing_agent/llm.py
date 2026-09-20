"""The model layer: every call is priced before it is made.

:class:`LLM` is the only thing in this codebase that talks to Claude, and it
refuses to do so without a reservation from the :class:`~sourcing_agent.ledger.SpendLedger`.
The sequence for every single call is fixed:

1. ``count_tokens`` against the real prompt - not an estimate, the API's own count
2. reserve worst-case cost (counted input + full ``max_tokens`` of output)
3. dispatch, with the response validated against a Pydantic schema
4. commit actual usage

The backend is pluggable so the funnel can be exercised end to end without an
API key: :class:`ScriptedBackend` replays canned results and reports plausible
usage, which is what the tests run against.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol, Type, TypeVar

from pydantic import BaseModel, ValidationError

from .ledger import SpendLedger

T = TypeVar("T", bound=BaseModel)

# Models that accept adaptive thinking and an effort level. Haiku 4.5 predates
# both, so asking for either would be a 400 rather than a cheaper answer.
ADAPTIVE_THINKING_MODELS = frozenset(
    {"claude-opus-5", "claude-opus-4-8", "claude-sonnet-5", "claude-fable-5"}
)
EFFORT_MODELS = ADAPTIVE_THINKING_MODELS


class LLMError(RuntimeError):
    pass


class ModelRefusal(LLMError):
    """The model declined the request (``stop_reason == "refusal"``)."""


class LLMUnavailable(LLMError):
    """No usable backend - missing SDK or missing credentials."""


@dataclass
class Call:
    """One prepared request."""

    stage: str
    model: str
    system: str
    user: str
    schema: Type[BaseModel]
    max_tokens: int
    effort: str | None = None
    thinking: bool = False

    @property
    def messages(self) -> list[dict[str, Any]]:
        return [{"role": "user", "content": self.user}]


@dataclass
class Result:
    value: Any
    model: str
    cost_usd: float
    input_tokens: int
    output_tokens: int
    cached_tokens: int = 0


class Backend(Protocol):
    """What the budget wrapper needs from a model provider."""

    def count_tokens(self, call: Call) -> int: ...

    def invoke(self, call: Call) -> tuple[BaseModel, Any]: ...


# --------------------------------------------------------------------------
# Anthropic
# --------------------------------------------------------------------------


class AnthropicBackend:
    """Claude via the official SDK."""

    def __init__(self, client: Any = None) -> None:
        if client is None:
            try:
                import anthropic
            except ImportError as exc:
                raise LLMUnavailable(
                    "the model funnel needs the Anthropic SDK: "
                    "pip install 'sourcing-agent[llm]'"
                ) from exc
            try:
                client = anthropic.Anthropic()
            except Exception as exc:  # noqa: BLE001 - surfaced as a clear message
                raise LLMUnavailable(f"could not construct an Anthropic client: {exc}") from exc
        self.client = client
        self._effort_supported: dict[str, bool] = {}

    # -- prompt shape ------------------------------------------------------

    @staticmethod
    def _system_blocks(call: Call) -> list[dict[str, Any]]:
        """The stage's system prompt is identical across every call in that
        stage, so it is a perfect cache prefix - marked once, read thereafter."""
        return [
            {
                "type": "text",
                "text": call.system,
                "cache_control": {"type": "ephemeral"},
            }
        ]

    def _extra(self, call: Call) -> dict[str, Any]:
        extra: dict[str, Any] = {}
        if call.thinking and call.model in ADAPTIVE_THINKING_MODELS:
            extra["thinking"] = {"type": "adaptive"}
        if (
            call.effort
            and call.model in EFFORT_MODELS
            and self._effort_supported.get(call.model, True)
        ):
            extra["output_config"] = {"effort": call.effort}
        return extra

    # -- Backend -----------------------------------------------------------

    def count_tokens(self, call: Call) -> int:
        """The API's own tokenizer. Never estimate with a third-party one -
        they undercount Claude tokens badly, and an undercount here is a
        budget breach."""
        try:
            response = self.client.messages.count_tokens(
                model=call.model,
                system=self._system_blocks(call),
                messages=call.messages,
            )
            return int(response.input_tokens)
        except Exception:  # noqa: BLE001 - counting must never hard-fail a run
            # Conservative fallback: over-count rather than under-count, so the
            # cap still holds when the counting endpoint is unreachable.
            return int(len(call.system + call.user) / 2.5) + 512

    def invoke(self, call: Call) -> tuple[BaseModel, Any]:
        kwargs: dict[str, Any] = dict(
            model=call.model,
            max_tokens=call.max_tokens,
            system=self._system_blocks(call),
            messages=call.messages,
            output_format=call.schema,
        )
        extra = self._extra(call)
        try:
            response = self.client.messages.parse(**kwargs, **extra)
        except TypeError:
            # Older SDK that does not accept output_config alongside parse().
            self._effort_supported[call.model] = False
            extra.pop("output_config", None)
            response = self.client.messages.parse(**kwargs, **extra)
        except Exception as exc:  # noqa: BLE001
            if extra.get("output_config") and _is_bad_request(exc):
                self._effort_supported[call.model] = False
                extra.pop("output_config", None)
                response = self.client.messages.parse(**kwargs, **extra)
            else:
                raise

        if getattr(response, "stop_reason", None) == "refusal":
            details = getattr(response, "stop_details", None)
            raise ModelRefusal(
                f"{call.model} declined the {call.stage} request "
                f"({getattr(details, 'category', 'unspecified')})"
            )

        parsed = getattr(response, "parsed_output", None)
        if parsed is None:
            raise LLMError(f"{call.stage}: model returned no parseable output")
        return parsed, getattr(response, "usage", None)


def _is_bad_request(exc: Exception) -> bool:
    status = getattr(exc, "status_code", None)
    return status == 400 or "invalid_request" in str(exc).lower()


# --------------------------------------------------------------------------
# Scripted (tests, dry runs, demos)
# --------------------------------------------------------------------------


@dataclass
class ScriptedUsage:
    input_tokens: int
    output_tokens: int
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0


@dataclass
class ScriptedBackend:
    """Replays canned responses. No network, no key, no cost.

    ``handler`` receives the prepared :class:`Call` and returns an instance of
    ``call.schema``. Everything downstream - budget accounting, validation,
    stage bookkeeping - behaves exactly as it does against the real API.
    """

    handler: Callable[[Call], BaseModel]
    output_tokens: int = 400
    calls: list[Call] = field(default_factory=list)

    def count_tokens(self, call: Call) -> int:
        return max(1, int(len(call.system + call.user) / 4))

    def invoke(self, call: Call) -> tuple[BaseModel, Any]:
        self.calls.append(call)
        value = self.handler(call)
        if not isinstance(value, call.schema):
            raise LLMError(
                f"scripted handler returned {type(value).__name__}, "
                f"expected {call.schema.__name__}"
            )
        usage = ScriptedUsage(
            input_tokens=self.count_tokens(call),
            output_tokens=self.output_tokens,
        )
        return value, usage


# --------------------------------------------------------------------------
# The budget-gated wrapper
# --------------------------------------------------------------------------


class LLM:
    """Budget-enforcing façade over a :class:`Backend`."""

    def __init__(self, backend: Backend, ledger: SpendLedger) -> None:
        self.backend = backend
        self.ledger = ledger

    def structured(
        self,
        *,
        stage: str,
        model: str,
        system: str,
        user: str,
        schema: Type[T],
        max_tokens: int,
        effort: str | None = None,
        thinking: bool = False,
    ) -> Result:
        """Make one validated, budgeted call.

        Raises :class:`~sourcing_agent.ledger.BudgetExceeded` *before* dispatch
        when the worst case would not fit under the cap.
        """
        call = Call(
            stage=stage,
            model=model,
            system=system,
            user=user,
            schema=schema,
            max_tokens=max_tokens,
            effort=effort,
            thinking=thinking,
        )

        input_tokens = self.backend.count_tokens(call)
        reservation = self.ledger.reserve(stage, model, input_tokens, max_tokens)

        try:
            value, usage = self.backend.invoke(call)
        except Exception:
            # The call never completed; give the reservation back so a cheaper
            # stage can still use the headroom.
            self.ledger.release(reservation)
            raise

        cost = self.ledger.commit(reservation, usage)
        return Result(
            value=value,
            model=model,
            cost_usd=cost,
            input_tokens=getattr(usage, "input_tokens", input_tokens),
            output_tokens=getattr(usage, "output_tokens", 0),
            cached_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
        )


def default_backend() -> Backend:
    """An Anthropic backend, or a clear error explaining what is missing."""
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        # The SDK also resolves `ant auth login` profiles, so absence of the env
        # var is not proof of absence of credentials - let the SDK decide.
        pass
    return AnthropicBackend()


__all__ = [
    "LLM",
    "Call",
    "Result",
    "Backend",
    "AnthropicBackend",
    "ScriptedBackend",
    "ScriptedUsage",
    "LLMError",
    "LLMUnavailable",
    "ModelRefusal",
    "default_backend",
    "ValidationError",
]
