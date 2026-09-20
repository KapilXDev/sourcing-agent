"""Connector registry.

Connectors self-register at import time. Nothing else in the system knows the
name of a concrete connector class - the agent, the CLI and the submitter all
work through this table.
"""

from __future__ import annotations

from typing import Type, TypeVar

from .base import Connector

REGISTRY: dict[str, Type[Connector]] = {}

C = TypeVar("C", bound=Type[Connector])


def register(cls: C) -> C:
    if not cls.slug:
        raise ValueError(f"{cls.__name__} must declare a slug")
    if cls.slug in REGISTRY:
        raise ValueError(f"duplicate connector slug {cls.slug!r}")
    REGISTRY[cls.slug] = cls
    return cls


def get(slug: str) -> Type[Connector]:
    try:
        return REGISTRY[slug]
    except KeyError as exc:
        known = ", ".join(sorted(REGISTRY))
        raise KeyError(f"unknown connector {slug!r}; known: {known}") from exc


def submit_capable() -> list[Type[Connector]]:
    return [c for c in REGISTRY.values() if c.can_submit()]


def submit_capable_slugs() -> frozenset[str]:
    """Slugs of sources an application can actually be transmitted through.

    Used as the primary dedupe tie-break: see :meth:`sourcing_agent.store.Store.dedupe`.
    """
    return frozenset(slug for slug, cls in REGISTRY.items() if cls.can_submit())
