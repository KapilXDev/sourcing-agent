"""The gate is the cheapest stage, so it gets the most tests.

Every rule has a case that fires it and the default posting is one that passes
everything - so a regression that makes the gate too permissive (postings that
should be rejected reaching the model) or too strict (good roles silently
dropped) shows up as a named failure.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from sourcing_agent.config import GateConfig, Profile, WorkAuthorization
from sourcing_agent.gate import (
    DecisionGate,
    detect_seniority,
    parse_salary,
    remote_residue,
)
from tests.conftest import make_posting


@pytest.fixture
def profile() -> Profile:
    return Profile(
        name="Test",
        email="t@example.com",
        titles=["backend engineer", "software engineer", "platform engineer"],
        exclude_titles=["sales", "support engineer"],
        seniority=["mid", "senior", "staff"],
        must_have_any=["python", "go", "kubernetes", "postgres"],
        nice_to_have=["rust"],
        exclude_keywords=["unpaid"],
        exclude_companies=["Scam Corp"],
        locations=["united states", "remote", "austin"],
        remote_only=False,
        work_authorization=WorkAuthorization(country="US", needs_sponsorship=False),
        min_base_salary=150000,
    )


@pytest.fixture
def gate(profile: Profile) -> DecisionGate:
    return DecisionGate(profile, GateConfig())


def rules_fired(decision) -> set[str]:
    return {r.split("(")[0] for r in decision.rejections}


def test_a_good_posting_passes(gate):
    decision = gate.evaluate(make_posting())
    assert decision.passed, decision.rejections
    assert decision.prefilter_score > 50
    assert "python" in decision.matched_keywords


def test_gate_makes_no_network_or_model_calls(gate, monkeypatch):
    """The whole point of the gate. If anything here ever reaches out, this
    test starts failing loudly rather than quietly costing money."""
    import httpx

    def explode(*args, **kwargs):
        raise AssertionError("the gate must not perform I/O")

    monkeypatch.setattr(httpx.Client, "send", explode)
    assert gate.evaluate(make_posting()).passed


def test_already_assessed_is_skipped(profile):
    posting = make_posting()
    gate = DecisionGate(profile, GateConfig(), seen_keys={posting.key})
    assert "dedupe:already_assessed" in rules_fired(gate.evaluate(posting))


def test_stale_posting_rejected(gate, now):
    posting = make_posting(posted_at=now - timedelta(days=90))
    assert "staleness:too_old" in rules_fired(gate.evaluate(posting))


def test_thin_description_rejected(gate):
    assert "content:too_thin" in rules_fired(gate.evaluate(make_posting(description="tiny")))


def test_excluded_company(gate):
    posting = make_posting(company="Scam Corp Holdings")
    assert "company:excluded" in rules_fired(gate.evaluate(posting))


def test_excluded_title(gate):
    posting = make_posting(title="Senior Sales Engineer")
    assert "title:excluded" in rules_fired(gate.evaluate(posting))


def test_unrelated_title(gate):
    posting = make_posting(title="Senior Product Designer")
    assert "title:no_match" in rules_fired(gate.evaluate(posting))


@pytest.mark.parametrize(
    "title,expected",
    [
        ("New Grad Software Engineer", "seniority:overqualified"),
        ("Director of Software Engineering", "seniority:underqualified"),
        ("Principal Software Engineer", "seniority:underqualified"),
    ],
)
def test_seniority_mismatch(gate, title, expected):
    assert expected in rules_fired(gate.evaluate(make_posting(title=title)))


def test_seniority_match_is_not_rejected(gate):
    for title in ("Staff Backend Engineer", "Senior Backend Engineer", "Backend Engineer"):
        assert gate.evaluate(make_posting(title=title)).passed, title


def test_location_ineligible(gate):
    posting = make_posting(location="Reston, VA", remote=False)
    assert "location:ineligible" in rules_fired(gate.evaluate(posting))


def test_remote_only_profile_rejects_onsite(profile):
    profile.remote_only = True
    gate = DecisionGate(profile, GateConfig())
    posting = make_posting(
        remote=False,
        location="Austin, TX",
        description=make_posting().description + " This role is on-site only.",
    )
    assert "remote:onsite_only" in rules_fired(gate.evaluate(posting))


def test_sponsorship_only_matters_when_needed(profile):
    text = make_posting().description + " We are unable to provide sponsorship."
    posting = make_posting(description=text)

    no_sponsorship_needed = DecisionGate(profile, GateConfig())
    assert no_sponsorship_needed.evaluate(posting).passed

    profile.work_authorization = WorkAuthorization(country="US", needs_sponsorship=True)
    needs_sponsorship = DecisionGate(profile, GateConfig())
    assert "authorization:no_sponsorship" in rules_fired(needs_sponsorship.evaluate(posting))


def test_explicit_sponsorship_offer_overrides(profile):
    profile.work_authorization = WorkAuthorization(country="US", needs_sponsorship=True)
    gate = DecisionGate(profile, GateConfig())
    text = (
        make_posting().description
        + " Visa sponsorship is available. We do not offer sponsorship for interns."
    )
    assert gate.evaluate(make_posting(description=text)).passed


def test_clearance_requirement(gate):
    text = make_posting().description + " Requires an active TS/SCI clearance."
    assert "authorization:clearance_required" in rules_fired(
        gate.evaluate(make_posting(description=text))
    )


def test_excluded_keyword(gate):
    text = make_posting().description + " This is an unpaid position."
    assert "keywords:excluded" in rules_fired(gate.evaluate(make_posting(description=text)))


def test_insufficient_keyword_hits(gate):
    text = (
        "We are a COBOL shop maintaining mainframe batch jobs for a regional bank. "
        "The role involves JCL, VSAM datasets and a great deal of careful reading of "
        "programs written before you were born. Patience is the core requirement here."
    )
    decision = gate.evaluate(make_posting(description=text))
    assert "keywords:insufficient" in rules_fired(decision)
    assert set(decision.missing_required) >= {"python", "kubernetes"}


def test_salary_floor(gate):
    text = make_posting().description + " Compensation: $95,000 - $120,000."
    assert "comp:below_floor" in rules_fired(gate.evaluate(make_posting(description=text)))


def test_unstated_salary_is_not_a_rejection(gate):
    assert gate.evaluate(make_posting()).passed


def test_all_failures_are_collected_not_just_the_first(gate, now):
    posting = make_posting(
        title="Senior Sales Engineer",
        company="Scam Corp",
        posted_at=now - timedelta(days=200),
        description="short",
    )
    fired = rules_fired(gate.evaluate(posting))
    assert {"title:excluded", "company:excluded", "staleness:too_old", "content:too_thin"} <= fired


def test_partition_sorts_by_score(gate, now):
    fresh = make_posting(source_id="fresh", posted_at=now - timedelta(days=1))
    old = make_posting(source_id="old", posted_at=now - timedelta(days=25))
    passed, rejected = gate.partition([old, fresh])
    assert not rejected
    assert [p.source_id for p, _ in passed] == ["fresh", "old"]


@pytest.mark.parametrize(
    "text,expected",
    [
        ("$150k-$200k", (150000, 200000)),
        ("$140,000 - $185,000 plus equity", (140000, 185000)),
        ("base salary $180k", (180000, 180000)),
        ("we serve 500,000 users", (None, None)),
        ("raised $50,000,000 in Series B", (None, None)),
        ("no compensation stated", (None, None)),
    ],
)
def test_parse_salary(text, expected):
    assert parse_salary(text) == expected


@pytest.mark.parametrize(
    "title,level",
    [
        ("Staff Engineer", "staff"),
        ("Sr. Backend Developer", "senior"),
        ("New Grad SWE", "junior"),
        ("VP of Engineering", "executive"),
        ("Backend Engineer", "mid"),
        ("Software Engineering Intern", "intern"),
    ],
)
def test_detect_seniority(title, level):
    assert detect_seniority(title) == level


def test_keyword_matching_respects_word_boundaries(profile):
    """`go` must not match `Google`, or every posting matches everything."""
    gate = DecisionGate(profile, GateConfig())
    text = (
        "We are a Google Cloud shop building information architecture for goats. "
        "Our stack is entirely proprietary and we do not discuss it publicly. The "
        "role is mostly stakeholder management and writing documents for review."
    )
    assert "keywords:insufficient" in rules_fired(gate.evaluate(make_posting(description=text)))


# -- remote is an arrangement, not a place ---------------------------------


@pytest.mark.parametrize(
    "location,remote",
    [
        ("Remote - US", True),
        ("Remote - United States", True),
        ("Remote (US)", True),
        ("Remote, US", True),
        ("USA Only", True),
        ("Remote", True),            # no place named -> unrestricted
        ("Worldwide", True),
        ("Anywhere", True),
        ("Remote, Austin", True),
        ("Austin, TX", None),
    ],
)
def test_eligible_locations_pass(gate, location, remote):
    decision = gate.evaluate(make_posting(location=location, remote=remote))
    assert "location:ineligible" not in rules_fired(decision), decision.rejections


@pytest.mark.parametrize(
    "location",
    ["Berlin", "Remote - EU only", "Remote (Germany)", "Fully Remote - Tokyo", "Remote - LATAM"],
)
def test_remote_does_not_launder_an_ineligible_location(gate, location):
    """The bug this rule exists for: treating `remote` as a free pass lets a
    role the candidate cannot legally take reach the paid stages."""
    decision = gate.evaluate(make_posting(location=location, remote=True))
    assert "location:ineligible" in rules_fired(decision)


def test_country_synonyms_match_in_both_directions():
    profile = Profile(name="T", email="t@e.com", titles=[], must_have_any=[], locations=["us"])
    gate = DecisionGate(profile, GateConfig())
    for written in ("Remote - United States", "USA", "Remote (U.S.)", "America"):
        decision = gate.evaluate(make_posting(location=written, remote=True))
        assert "location:ineligible" not in rules_fired(decision), written


def test_listing_only_remote_imposes_no_geography():
    """`locations: [remote]` describes an arrangement, not a place, so it must
    not silently become a geographic filter that nothing can satisfy."""
    profile = Profile(name="T", email="t@e.com", titles=[], must_have_any=[], locations=["remote"])
    gate = DecisionGate(profile, GateConfig())
    for written in ("Berlin", "Tokyo", "Remote - EU"):
        assert gate.evaluate(make_posting(location=written, remote=True)).passed, written


def test_unspecified_location_is_still_rejected_when_not_remote(gate):
    decision = gate.evaluate(make_posting(location=None, remote=False))
    assert "location:ineligible" in rules_fired(decision)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Remote - US", "US"),
        ("Fully Remote (EU)", "EU"),
        ("Remote", ""),
        ("100% Remote — Berlin", "Berlin"),
        ("Austin, TX", "Austin, TX"),
    ],
)
def test_remote_residue(raw, expected):
    assert remote_residue(raw) == expected
