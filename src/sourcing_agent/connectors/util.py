"""Small helpers shared by the ATS connectors."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import httpx

from ..models import Applicant
from .base import ConnectorError


def parse_dt(value: Any) -> datetime | None:
    """Parse the half-dozen date shapes the job boards actually emit."""
    if not value:
        return None
    if isinstance(value, (int, float)):
        seconds = value / 1000 if value > 10_000_000_000 else value
        try:
            return datetime.fromtimestamp(seconds, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value).strip().replace("Z", "+00:00")
    for candidate in (text, text.split("T")[0], text.split(" ")[0]):
        try:
            parsed = datetime.fromisoformat(candidate)
        except ValueError:
            continue
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return None


def split_handle(handle: str | None, parts: int = 2) -> tuple[str, ...]:
    """Split an ``a/b`` apply handle, failing loudly if it is malformed."""
    if not handle:
        raise ConnectorError("posting has no apply handle")
    pieces = handle.split("/", parts - 1)
    if len(pieces) != parts or not all(pieces):
        raise ConnectorError(f"malformed apply handle: {handle!r}")
    return tuple(pieces)


def file_descriptor(applicant: Applicant) -> dict[str, Any]:
    """JSON-safe stand-in for the multipart resume, so a dry run can print the
    request without dumping the file into a log."""
    if not applicant.resume_bytes:
        return {}
    return {
        "resume": {
            "filename": applicant.resume_filename,
            "bytes": len(applicant.resume_bytes),
            "content_type": "application/pdf",
        }
    }


def resume_files(applicant: Applicant, field: str = "resume") -> dict[str, Any] | None:
    if not applicant.resume_bytes:
        return None
    return {
        field: (applicant.resume_filename, applicant.resume_bytes, "application/pdf")
    }


def safe_json(response: httpx.Response) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}


def slugify(text: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in text.lower())[:60].strip("_")
