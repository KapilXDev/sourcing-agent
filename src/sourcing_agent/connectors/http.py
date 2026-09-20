"""HTTP with a fixture layer.

Every network read goes through :class:`Fetcher`. In offline mode it serves
from ``fixtures/<slug>/<key>.json``; with ``SOURCING_RECORD=1`` a live read is
written back to that path. The consequence is that the full pipeline - all 13
sources, the gate, and the funnel against a stub model - runs in CI with no
network and no API key.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from html import unescape
from pathlib import Path
from typing import Any

import httpx

USER_AGENT = (
    "sourcing-agent/0.1 (+https://github.com/KapilXDev/sourcing-agent) "
    "personal job search; contact via repository"
)

_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def fixture_key(*parts: str) -> str:
    joined = "-".join(p for p in parts if p)
    return _SAFE.sub("-", joined).strip("-").lower() or "default"


class FetchError(RuntimeError):
    pass


class FixtureMissing(FetchError):
    """Offline mode asked for a fixture that has not been recorded."""


@dataclass
class Fetcher:
    """Shared HTTP client, rate limiter, and fixture cache."""

    fixtures_dir: Path = Path("fixtures")
    offline: bool = False
    timeout: float = 20.0
    min_interval: float = 0.34
    """Politeness floor between requests to the same host (~3 req/s)."""

    record: bool = field(default_factory=lambda: os.environ.get("SOURCING_RECORD") == "1")
    _client: httpx.Client | None = field(default=None, repr=False)
    _last_call: dict[str, float] = field(default_factory=dict, repr=False)

    # -- plumbing ----------------------------------------------------------

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(
                timeout=self.timeout,
                follow_redirects=True,
                headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            )
        return self._client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def _throttle(self, url: str) -> None:
        host = httpx.URL(url).host or ""
        last = self._last_call.get(host)
        if last is not None:
            wait = self.min_interval - (time.monotonic() - last)
            if wait > 0:
                time.sleep(wait)
        self._last_call[host] = time.monotonic()

    def _path(self, slug: str, key: str, suffix: str) -> Path:
        return self.fixtures_dir / slug / f"{key}{suffix}"

    # -- reads -------------------------------------------------------------

    def json(
        self,
        slug: str,
        url: str,
        key: str,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        path = self._path(slug, key, ".json")
        if self.offline:
            if not path.exists():
                raise FixtureMissing(
                    f"{slug}: no fixture at {path}. Record one with "
                    f"SOURCING_RECORD=1 and SOURCING_OFFLINE=0."
                )
            return json.loads(path.read_text(encoding="utf-8"))

        self._throttle(url)
        try:
            response = self.client.get(url, params=params, headers=headers)
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPError as exc:
            raise FetchError(f"{slug}: GET {url} failed: {exc}") from exc
        except ValueError as exc:
            raise FetchError(f"{slug}: GET {url} returned non-JSON") from exc

        if self.record:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload, indent=2)[:4_000_000], encoding="utf-8")
        return payload

    def text(
        self,
        slug: str,
        url: str,
        key: str,
        headers: dict[str, str] | None = None,
    ) -> str:
        path = self._path(slug, key, ".html")
        if self.offline:
            if not path.exists():
                raise FixtureMissing(f"{slug}: no fixture at {path}")
            return path.read_text(encoding="utf-8")

        self._throttle(url)
        try:
            response = self.client.get(url, headers=headers)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise FetchError(f"{slug}: GET {url} failed: {exc}") from exc

        if self.record:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(response.text, encoding="utf-8")
        return response.text


    def json_post(
        self,
        slug: str,
        url: str,
        key: str,
        body: dict[str, Any],
        headers: dict[str, str] | None = None,
    ) -> Any:
        """POST-based search endpoints (Workday's CXS is the notable one).

        Offline this is indistinguishable from :meth:`json` - the fixture is
        keyed by name, not by request shape.
        """
        path = self._path(slug, key, ".json")
        if self.offline:
            if not path.exists():
                raise FixtureMissing(f"{slug}: no fixture at {path}")
            return json.loads(path.read_text(encoding="utf-8"))

        self._throttle(url)
        try:
            response = self.client.post(url, json=body, headers=headers)
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPError as exc:
            raise FetchError(f"{slug}: POST {url} failed: {exc}") from exc
        except ValueError as exc:
            raise FetchError(f"{slug}: POST {url} returned non-JSON") from exc

        if self.record:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload, indent=2)[:4_000_000], encoding="utf-8")
        return payload

    def render(
        self,
        slug: str,
        url: str,
        key: str,
        wait_for: str | None = None,
        timeout_ms: int = 30_000,
    ) -> str:
        """Fetch a JavaScript-rendered page through Playwright.

        Offline, this serves the recorded HTML fixture - which is how the
        browser-backed sources stay testable in CI. Online with Playwright
        missing it raises rather than quietly serving a stale fixture: a live
        run reading yesterday's HTML is worse than a failed one.
        """
        path = self._path(slug, key, ".html")
        if self.offline:
            if not path.exists():
                raise FixtureMissing(f"{slug}: no fixture at {path}")
            return path.read_text(encoding="utf-8")

        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:  # pragma: no cover - depends on extras
            raise FetchError(
                f"{slug} needs a browser: pip install 'sourcing-agent[browser]' "
                "&& playwright install chromium"
            ) from exc

        self._throttle(url)
        with sync_playwright() as pw:  # pragma: no cover - requires a browser
            browser = pw.chromium.launch(headless=True)
            try:
                page = browser.new_page(user_agent=USER_AGENT)
                page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
                if wait_for:
                    page.wait_for_selector(wait_for, timeout=timeout_ms)
                html = page.content()
            finally:
                browser.close()

        if self.record:  # pragma: no cover
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(html, encoding="utf-8")
        return html


_TAG = re.compile(r"<[^>]+>")
_DASHES = str.maketrans({"—": "-", "–": "-", "’": "'", " ": " "})


def strip_html(html: str | None) -> str:
    """Flatten an HTML job description to plain text.

    Job descriptions arrive as HTML from almost every ATS. A real parser is
    overkill for text the model will read anyway; what matters is preserving
    line structure so bullet lists stay legible.
    """
    if not html:
        return ""
    text = re.sub(r"(?i)<br\s*/?>", "\n", html)
    text = re.sub(r"(?i)</(p|div|li|h[1-6]|tr)>", "\n", text)
    text = re.sub(r"(?i)<li[^>]*>", "- ", text)
    text = _TAG.sub("", text)
    # One pass, via the stdlib. A hand-rolled table decodes twice: replacing
    # `&amp;` first turns `&amp;lt;` into `&lt;`, which the next rule then eats.
    text = unescape(text).translate(_DASHES)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def looks_remote(*values: str | None) -> bool | None:
    """Tri-state remote detection. ``None`` means the source did not say."""
    joined = " ".join(v for v in values if v).lower()
    if not joined:
        return None
    if any(w in joined for w in ("remote", "anywhere", "distributed", "work from home")):
        return True
    if any(w in joined for w in ("on-site", "onsite", "in-office", "hybrid")):
        return False
    return None
