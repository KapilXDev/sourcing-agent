"""Submission rights and routing.

The claim under test: nothing a model emits can cause an application to be
transmitted through a connector that does not hold the right to transmit it.
"""

from __future__ import annotations

import pytest

from sourcing_agent.capabilities import Capability, RightsError, Route, SubmissionRights
from sourcing_agent.config import SubmissionConfig
from sourcing_agent.connectors import REGISTRY
from sourcing_agent.connectors.base import Connector, SubmissionCapable
from sourcing_agent.connectors.http import Fetcher
from sourcing_agent.llm import Call
from sourcing_agent.models import ApplicationDraft, ApplicationPacket, FitAssessment
from sourcing_agent.submitter import Submitter, resolve_route
from tests.conftest import make_posting, scripted_handler

SUBMIT_CAPABLE = {"greenhouse", "lever", "ashby", "workable", "smartrecruiters"}
READ_ONLY = set(REGISTRY) - SUBMIT_CAPABLE


# -- the declarations themselves -------------------------------------------


def test_thirteen_sources_are_registered():
    assert len(REGISTRY) == 13


def test_submission_rights_are_exactly_as_declared():
    actual = {slug for slug, cls in REGISTRY.items() if cls.can_submit()}
    assert actual == SUBMIT_CAPABLE


def test_read_only_connectors_have_no_submit_method():
    """The structural half of the lock: the verb does not exist to be called."""
    for slug in READ_ONLY:
        cls = REGISTRY[slug]
        assert not issubclass(cls, SubmissionCapable), slug
        assert not hasattr(cls, "submit"), slug
        assert Capability.SUBMIT not in cls.capabilities, slug


def test_every_connector_states_a_basis_for_its_rights():
    for slug, cls in REGISTRY.items():
        assert cls.rights.basis.strip(), f"{slug} has no written basis"
        assert len(cls.rights.basis) > 25, f"{slug}'s basis is too vague to audit"


def test_submit_capable_connectors_name_their_credential():
    for slug in SUBMIT_CAPABLE:
        assert REGISTRY[slug].rights.requires_credential, slug


def test_rights_cannot_be_declared_without_a_basis():
    with pytest.raises(ValueError):
        SubmissionRights(can_submit=False, basis="   ")


def test_submit_rights_without_a_credential_are_rejected():
    with pytest.raises(ValueError):
        SubmissionRights(can_submit=True, basis="a perfectly good reason, honestly")


# -- the routing table -----------------------------------------------------


def connector(slug: str) -> Connector:
    from sourcing_agent.config import SourceConfig

    return REGISTRY[slug](SourceConfig(), Fetcher(offline=True))


def enabled(**overrides) -> SubmissionConfig:
    base = dict(
        enabled=True, dry_run=True, max_per_run=3, allow_connectors=["greenhouse"]
    )
    base.update(overrides)
    return SubmissionConfig(**base)


def route(slug="greenhouse", config=None, **kwargs):
    defaults = dict(
        credential_present=True,
        already_submitted=False,
        submissions_this_run=0,
        model_recommends=True,
    )
    defaults.update(kwargs)
    return resolve_route(connector(slug), config or enabled(), **defaults)


def test_all_locks_open_routes_to_submit():
    assert route().route is Route.SUBMIT


@pytest.mark.parametrize(
    "case,kwargs,fragment",
    [
        ("submission disabled", {"config": enabled(enabled=False)}, "disabled"),
        ("not allowlisted", {"config": enabled(allow_connectors=[])}, "allowlist"),
        ("no credential", {"credential_present": False}, "GREENHOUSE_API_KEY"),
        ("already applied", {"already_submitted": True}, "already applied"),
        ("run limit hit", {"submissions_this_run": 3}, "limit"),
        ("model said no", {"model_recommends": False}, "recommended against"),
    ],
)
def test_each_closed_lock_routes_to_export(case, kwargs, fragment):
    decision = route(**kwargs)
    assert decision.route is Route.EXPORT, case
    assert fragment in decision.reason


@pytest.mark.parametrize("slug", sorted(READ_ONLY))
def test_read_only_connectors_always_export(slug):
    """Even fully opted in, with a credential, and with the model recommending."""
    config = enabled(allow_connectors=list(REGISTRY))
    decision = route(slug, config=config)
    assert decision.route is Route.EXPORT
    assert "read-only" in decision.reason


def test_the_model_can_veto_but_cannot_authorise():
    """`proceed=True` opens nothing on its own; `proceed=False` closes."""
    locked = enabled(enabled=False)
    assert route(config=locked, model_recommends=True).route is Route.EXPORT
    assert route(model_recommends=False).route is Route.EXPORT
    assert route(model_recommends=True).route is Route.SUBMIT


# -- the dispatcher --------------------------------------------------------


def packet(route_: Route, slug: str = "greenhouse") -> ApplicationPacket:
    handler = scripted_handler()
    return ApplicationPacket(
        posting=make_posting(source=slug),
        assessment=handler(_call(FitAssessment)),
        draft=handler(_call(ApplicationDraft)),
        route=route_,
        route_reason="test",
    )


def _call(schema):
    return Call(stage="x", model="m", system="", user="", schema=schema, max_tokens=10)


def test_dispatch_refuses_a_submit_route_on_a_read_only_connector(store, tmp_path, monkeypatch):
    """Defence in depth: even if routing were somehow wrong, dispatch stops it."""
    monkeypatch.setenv("GREENHOUSE_API_KEY", "key")
    submitter = Submitter(
        config=enabled(),
        profile=_profile(),
        store=store,
        out_dir=tmp_path / "out",
        run_id="r",
    )
    with pytest.raises(RightsError):
        submitter.dispatch(packet(Route.SUBMIT, slug="linkedin"), connector("linkedin"))


def test_dispatch_refuses_when_the_credential_is_missing(store, tmp_path):
    submitter = Submitter(
        config=enabled(), profile=_profile(), store=store, out_dir=tmp_path / "out", run_id="r"
    )
    with pytest.raises(RightsError, match="GREENHOUSE_API_KEY"):
        submitter.dispatch(packet(Route.SUBMIT), connector("greenhouse"))


def test_dry_run_builds_the_real_request_but_sends_nothing(store, tmp_path, monkeypatch):
    monkeypatch.setenv("GREENHOUSE_API_KEY", "key")
    submitter = Submitter(
        config=enabled(dry_run=True),
        profile=_profile(),
        store=store,
        out_dir=tmp_path / "out",
        run_id="r",
    )
    receipt = submitter.dispatch(packet(Route.SUBMIT), connector("greenhouse"))

    assert receipt.dry_run and not receipt.submitted
    assert receipt.route is Route.SUBMIT

    import json

    written = json.loads(
        next((tmp_path / "out" / "dry-run").glob("*.json")).read_text(encoding="utf-8")
    )
    assert written["method"] == "POST"
    assert "boards-api.greenhouse.io" in written["url"]
    assert written["data"]["email"] == "ada@example.com"
    assert written["data"]["cover_letter_text"].startswith("Dear team")
    assert not store.has_submitted(make_posting().key), "a dry run is not an application"


def test_export_writes_a_reviewable_packet(store, tmp_path):
    submitter = Submitter(
        config=SubmissionConfig(),
        profile=_profile(),
        store=store,
        out_dir=tmp_path / "out",
        run_id="r",
    )
    receipt = submitter.dispatch(packet(Route.EXPORT), connector("linkedin"))
    assert receipt.route is Route.EXPORT and not receipt.submitted

    markdown = next((tmp_path / "out" / "applications").glob("*.md")).read_text(encoding="utf-8")
    assert "## Cover letter" in markdown
    assert "Dear team" in markdown


def _profile():
    from sourcing_agent.config import Profile

    return Profile(name="Ada Lovelace", email="ada@example.com")
