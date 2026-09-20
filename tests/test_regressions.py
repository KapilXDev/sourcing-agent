"""Regressions found reviewing the first cut.

Each of these passed silently before the fix, which is why they are here.
"""

from __future__ import annotations

import os
import pathlib

import pytest

from sourcing_agent.agent import SourcingAgent
from sourcing_agent.capabilities import Route
from sourcing_agent.config import Profile, SourceConfig, SubmissionConfig
from sourcing_agent.connectors import REGISTRY
from sourcing_agent.connectors.http import Fetcher, strip_html
from sourcing_agent.connectors.registry import submit_capable_slugs
from sourcing_agent.llm import Call
from sourcing_agent.models import ApplicationDraft, ApplicationPacket, FitAssessment
from sourcing_agent.store import Store
from sourcing_agent.submitter import Submitter
from tests.conftest import make_posting, scripted_handler


def _packet(route, posting, proceed=True):
    h = scripted_handler(proceed=proceed)
    mk = lambda schema: h(
        Call(stage="x", model="m", system="", user="", schema=schema, max_tokens=1)
    )
    return ApplicationPacket(
        posting=posting,
        assessment=mk(FitAssessment),
        draft=mk(ApplicationDraft),
        route=route,
        route_reason="test",
    )


# -- dedupe must not throw away the only submittable copy ------------------


def test_dedupe_keeps_the_copy_that_can_be_applied_through(store: Store):
    """SmartRecruiters and Workday list endpoints omit the body. Ranking by
    description length alone hands the win to an aggregator and silently
    destroys the apply handle."""
    ats = make_posting(source="smartrecruiters", source_id="743", description="", apply_handle="743")
    aggregator = make_posting(source="remoteok", source_id="770", apply_handle=None)

    kept = store.dedupe([aggregator, ats], submit_capable_slugs())
    assert len(kept) == 1
    assert kept[0].source == "smartrecruiters"
    assert kept[0].apply_handle == "743"


def test_dedupe_still_prefers_the_richer_body_among_equals(store: Store):
    stub = make_posting(source="remoteok", source_id="1", description="x" * 100)
    full = make_posting(source="remotive", source_id="2", description="y" * 900)
    assert store.dedupe([stub, full], submit_capable_slugs())[0].source == "remotive"


# -- a dry run must preview the live run -----------------------------------


def test_dry_run_respects_the_per_run_submission_limit(store: Store, tmp_path, monkeypatch):
    monkeypatch.setenv("GREENHOUSE_API_KEY", "key")
    config = SubmissionConfig(
        enabled=True, dry_run=True, max_per_run=2, allow_connectors=["greenhouse"]
    )
    submitter = Submitter(
        config=config,
        profile=Profile(name="Ada Lovelace", email="ada@example.com"),
        store=store,
        out_dir=tmp_path / "out",
        run_id="r",
    )
    connector = REGISTRY["greenhouse"](SourceConfig(), Fetcher(offline=True))

    routes = []
    for i in range(4):
        posting = make_posting(source="greenhouse", source_id=str(i), apply_handle=f"b/{i}")
        decision = submitter.decide(connector, packet_proceed=True, posting_key=posting.key)
        routes.append(decision.route)
        submitter.dispatch(_packet(decision.route, posting), connector)

    assert routes == [Route.SUBMIT, Route.SUBMIT, Route.EXPORT, Route.EXPORT]
    assert submitter.submitted_this_run == 2


# -- entity decoding -------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("<p>a &amp;lt; b</p>", "a &lt; b"),   # decoded once, not twice
        ("<p>AT&amp;T</p>", "AT&T"),
        ("<p>x &lt;b&gt; y</p>", "x <b> y"),
        ("<p>R&amp;D &mdash; 24&#37; faster</p>", "R&D - 24% faster"),
    ],
)
def test_strip_html_decodes_entities_exactly_once(raw, expected):
    assert strip_html(raw) == expected


def test_greenhouse_double_escaping_still_resolves(source_fetcher):
    """Greenhouse escapes its HTML twice; that needs two decodes, and the
    connector supplies the first one itself."""
    from sourcing_agent.config import load_settings

    settings = load_settings(pathlib.Path(__file__).resolve().parent.parent / "profiles" / "example.yaml")
    connector = REGISTRY["greenhouse"](settings.sources["greenhouse"], source_fetcher)
    body = list(connector.discover())[0].description
    assert "<p>" not in body and "&lt;" not in body and "&amp;" not in body


@pytest.fixture
def source_fetcher() -> Fetcher:
    root = pathlib.Path(__file__).resolve().parent.parent
    return Fetcher(fixtures_dir=root / "fixtures", offline=True)


# -- the run record must be closed even on a crash -------------------------


def test_the_run_row_is_finished_even_when_the_funnel_cannot_start(settings):
    """Previously an unavailable backend left finished_at NULL and no report,
    so a crashed run was invisible to `sourcing-agent budget`."""
    store = Store(settings.db_path)
    agent = SourcingAgent(settings, store=store)

    with pytest.raises(Exception):
        agent.run()

    row = store.last_runs(1)[0]
    assert row["finished_at"] is not None
    assert row["report_json"]


# -- an unconfigured source should say so ----------------------------------


def test_requesting_an_unconfigured_source_reports_it(settings):
    settings.sources.pop("greenhouse")
    agent = SourcingAgent(settings)
    errors: list[str] = []
    assert agent.discover(["greenhouse"], errors) == []
    assert any("no `sources.greenhouse` block" in e for e in errors)


# -- the cover letter must reach every submit-capable connector ------------


@pytest.mark.parametrize("slug", sorted(submit_capable_slugs()))
def test_every_submit_capable_connector_carries_the_cover_letter(slug, tmp_path):
    """Stage 3 spends Opus tokens writing this; a connector that drops it is
    paying for nothing."""
    connector = REGISTRY[slug](SourceConfig(), Fetcher(offline=True))
    posting = make_posting(source=slug, apply_handle="board/123")
    packet = _packet(Route.SUBMIT, posting)
    applicant = Submitter(
        config=SubmissionConfig(),
        profile=Profile(name="Ada Lovelace", email="ada@example.com"),
        store=Store(tmp_path / f"{slug}.db"),
        out_dir=tmp_path / "out",
    ).applicant

    request = connector.build_submission(packet, applicant, "credential")
    blob = repr(request)
    assert packet.draft.cover_letter in blob or "cover_letter" in blob, (
        f"{slug} builds a request that carries no cover letter"
    )
