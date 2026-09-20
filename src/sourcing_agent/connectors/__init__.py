"""The 13 sources.

Importing this package registers every connector. Five hold submission rights;
eight are read-only. Which is which is a property of the class, visible with
``sourcing-agent sources``.
"""

from __future__ import annotations

from .base import Connector, ConnectorError, SubmissionCapable
from .http import Fetcher, FetchError, FixtureMissing
from .registry import REGISTRY, get, register, submit_capable

# Import for side effects: each module registers its connector(s).
from . import aggregators  # noqa: F401
from . import ashby  # noqa: F401
from . import browser  # noqa: F401
from . import greenhouse  # noqa: F401
from . import lever  # noqa: F401
from . import recruitee  # noqa: F401
from . import smartrecruiters  # noqa: F401
from . import workable  # noqa: F401

EXPECTED_SOURCES = 13

if len(REGISTRY) != EXPECTED_SOURCES:  # pragma: no cover - import-time guard
    raise RuntimeError(
        f"expected {EXPECTED_SOURCES} connectors, registered {len(REGISTRY)}: "
        f"{sorted(REGISTRY)}"
    )

__all__ = [
    "Connector",
    "ConnectorError",
    "SubmissionCapable",
    "Fetcher",
    "FetchError",
    "FixtureMissing",
    "REGISTRY",
    "get",
    "register",
    "submit_capable",
    "EXPECTED_SOURCES",
]
