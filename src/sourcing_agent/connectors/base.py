"""The connector contract.

A connector is the only thing in this system that touches a job source. It has
exactly one required verb - :meth:`Connector.discover` - and one *optional*
verb, ``submit``, which exists only on subclasses of :class:`SubmissionCapable`.

That asymmetry is the whole security design. A read-only connector does not
implement ``submit``; there is no method to call, no flag to flip, no prompt
that can produce one. Submission rights are declared statically alongside the
class and carry a written ``basis``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, ClassVar, Iterable

from ..capabilities import Capability, SubmissionRights, read_only
from ..config import SourceConfig
from ..models import Applicant, ApplicationPacket, Posting, Receipt
from .http import Fetcher


class ConnectorError(RuntimeError):
    """A source failed. One bad source must not abort the run."""


class Connector(ABC):
    """Base class for all 13 sources."""

    slug: ClassVar[str] = ""
    name: ClassVar[str] = ""
    kind: ClassVar[str] = "ats"
    """``ats`` (system of record), ``aggregator`` (index), ``browser`` (rendered)."""

    homepage: ClassVar[str] = ""
    rights: ClassVar[SubmissionRights] = read_only("no rights declared")
    capabilities: ClassVar[frozenset[Capability]] = frozenset({Capability.DISCOVER})

    def __init__(self, config: SourceConfig, fetcher: Fetcher) -> None:
        self.config = config
        self.fetcher = fetcher

    # -- required ----------------------------------------------------------

    @abstractmethod
    def discover(self) -> Iterable[Posting]:
        """Yield normalized postings from this source."""

    # -- optional ----------------------------------------------------------

    def fetch_detail(self, posting: Posting) -> Posting:
        """Hydrate a posting. Most sources return full text on discovery."""
        return posting

    # -- introspection -----------------------------------------------------

    @classmethod
    def can_submit(cls) -> bool:
        """True only when both locks are open: the subclass implements the verb
        *and* the declared rights permit it."""
        return cls.rights.can_submit and issubclass(cls, SubmissionCapable)

    @classmethod
    def describe(cls) -> dict[str, Any]:
        return {
            "slug": cls.slug,
            "name": cls.name,
            "kind": cls.kind,
            "can_submit": cls.can_submit(),
            "basis": cls.rights.basis,
            "credential": cls.rights.requires_credential,
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<{type(self).__name__} {self.slug}>"


class SubmissionCapable(Connector):
    """Mixin granting the ``submit`` verb.

    Subclassing this is necessary but not sufficient - ``rights.can_submit``
    must also be true and the run must opt the connector in. Three locks, all
    of which must be open, none of which a model can open.
    """

    capabilities: ClassVar[frozenset[Capability]] = frozenset(
        {Capability.DISCOVER, Capability.SUBMIT}
    )

    @abstractmethod
    def build_submission(
        self, packet: ApplicationPacket, applicant: Applicant, credential: str
    ) -> dict[str, Any]:
        """Construct the exact HTTP request that would be sent.

        Separated from :meth:`submit` so a dry run can build and validate the
        real payload without transmitting it - the difference between a dry run
        and a live run is one network call, not a different code path.
        """

    @abstractmethod
    def submit(
        self, packet: ApplicationPacket, applicant: Applicant, credential: str
    ) -> Receipt:
        """Transmit an application. Only reachable through
        :func:`sourcing_agent.submitter.Submitter.dispatch`."""
