"""Capability model: what a connector is *allowed* to do.

The central invariant of this project:

    The right to transmit an application is a static property of the
    connector, not a decision made by the agent or by a language model.

Two independent locks enforce it, and both must be open:

1. **Structural.** Only a connector that subclasses :class:`SubmissionCapable`
   has a ``submit`` method at all. A read-only connector does not implement the
   verb, so there is no code path to reach - nothing to be talked into.
2. **Declarative.** The connector must also declare ``rights.can_submit``, with
   a written ``basis`` recording *why* submission is permitted (a documented
   application API, an account you hold) or forbidden (terms of service, no
   public endpoint).

Models produce *recommendations*. Routing is computed from the connector's
rights (:func:`resolve_route`), never from model output.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Capability(str, Enum):
    """Verbs a connector may expose."""

    DISCOVER = "discover"
    """List postings from the source."""

    FETCH_DETAIL = "fetch_detail"
    """Hydrate a posting with its full description."""

    SUBMIT = "submit"
    """Transmit an application on the user's behalf."""


class Route(str, Enum):
    """Where a finished application packet is sent."""

    SUBMIT = "submit"
    """The connector holds submission rights; transmit through it."""

    EXPORT = "export"
    """No submission rights (or no credential); write to disk for manual apply."""


@dataclass(frozen=True)
class SubmissionRights:
    """A connector's standing authority to transmit applications.

    ``basis`` is mandatory and is surfaced in ``sourcing-agent sources``. If you
    cannot write down why submission is permitted, it is not permitted.
    """

    can_submit: bool
    basis: str
    requires_credential: str | None = None
    """Environment variable that must be present before any real submission."""

    requires_explicit_optin: bool = True
    """Even with rights and a credential, config must opt in per run."""

    def __post_init__(self) -> None:
        if not self.basis.strip():
            raise ValueError("SubmissionRights.basis must explain the grant or denial")
        if self.can_submit and not self.requires_credential:
            raise ValueError(
                "a submit-capable connector must name the credential it submits under"
            )


def read_only(basis: str) -> SubmissionRights:
    """Declare a connector read-only. The common case."""
    return SubmissionRights(can_submit=False, basis=basis, requires_credential=None)


class RightsError(PermissionError):
    """Raised when something tries to submit through a connector that may not."""


__all__ = [
    "Capability",
    "Route",
    "SubmissionRights",
    "RightsError",
    "read_only",
]
