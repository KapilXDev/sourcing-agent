"""Every connector, against its source's real response shape.

The fixtures are synthetic but their *shapes* are not - Greenhouse's escaped
HTML, Lever's split description blocks, Workday's "Posted 4 Days Ago", Hacker
News' pipe-delimited header line. These tests are what catch a normalisation
bug before it becomes a posting the gate silently misjudges.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sourcing_agent.config import SourceConfig, load_settings
from sourcing_agent.connectors import REGISTRY
from sourcing_agent.connectors.http import Fetcher, FixtureMissing, looks_remote, strip_html
from sourcing_agent.models import Posting

ROOT = Path(__file__).resolve().parent.parent
PROFILE = ROOT / "profiles" / "example.yaml"


@pytest.fixture
def fetcher() -> Fetcher:
    return Fetcher(fixtures_dir=ROOT / "fixtures", offline=True)


@pytest.fixture
def source_configs() -> dict[str, SourceConfig]:
    return load_settings(PROFILE).sources


def discover(slug: str, fetcher: Fetcher, configs: dict[str, SourceConfig]) -> list[Posting]:
    connector = REGISTRY[slug](configs[slug], fetcher)
    return list(connector.discover())


@pytest.mark.parametrize("slug", sorted(REGISTRY))
def test_every_connector_discovers_offline(slug, fetcher, source_configs):
    postings = discover(slug, fetcher, source_configs)
    assert postings, f"{slug} returned nothing from its fixture"
    for posting in postings:
        assert isinstance(posting, Posting)
        assert posting.source == slug
        assert posting.source_id and posting.title and posting.url
        assert posting.key.startswith(f"{slug}:")


@pytest.mark.parametrize("slug", sorted(REGISTRY))
def test_keys_are_unique_within_a_source(slug, fetcher, source_configs):
    postings = discover(slug, fetcher, source_configs)
    keys = [p.key for p in postings]
    assert len(keys) == len(set(keys))


def test_greenhouse_unescapes_double_escaped_html(fetcher, source_configs):
    postings = discover("greenhouse", fetcher, source_configs)
    body = postings[0].description
    assert "<p>" not in body and "&lt;" not in body
    assert "ingestion platform" in body


def test_greenhouse_reads_compensation_from_metadata(fetcher, source_configs):
    postings = {p.source_id: p for p in discover("greenhouse", fetcher, source_configs)}
    assert postings["4010001"].compensation_raw == "$180,000 - $225,000"


def test_greenhouse_carries_an_apply_handle(fetcher, source_configs):
    posting = discover("greenhouse", fetcher, source_configs)[0]
    assert posting.apply_handle == "demo-labs/4010001"


def test_lever_joins_its_split_description_blocks(fetcher, source_configs):
    posting = discover("lever", fetcher, source_configs)[0]
    assert "ingestion platform" in posting.description
    assert "distributed systems" in posting.description, "the `lists` block was dropped"


def test_ashby_uses_the_explicit_remote_flag(fetcher, source_configs):
    by_id = {p.title: p for p in discover("ashby", fetcher, source_configs)}
    assert by_id["Senior Backend Engineer"].remote is True
    assert by_id["Staff Infrastructure Engineer"].remote is False


def test_workable_merges_description_and_requirements(fetcher, source_configs):
    posting = discover("workable", fetcher, source_configs)[0]
    assert "ingestion platform" in posting.description
    assert "distributed systems" in posting.description


def test_smartrecruiters_list_has_no_body_until_detail_is_fetched(fetcher, source_configs):
    connector = REGISTRY["smartrecruiters"](source_configs["smartrecruiters"], fetcher)
    postings = list(connector.discover())
    assert all(p.description == "" for p in postings)

    hydrated = connector.fetch_detail(postings[0])
    assert "ingestion platform" in hydrated.description
    assert hydrated.key == postings[0].key


def test_workday_parses_relative_posting_dates(fetcher, source_configs):
    postings = discover("workday", fetcher, source_configs)
    ages = [p.age_days() for p in postings]
    assert ages[0] is not None and 3.5 < ages[0] < 4.5
    assert ages[1] is not None and 11.5 < ages[1] < 12.5


def test_workday_detail_fetch_fills_the_body(fetcher, source_configs):
    connector = REGISTRY["workday"](source_configs["workday"], fetcher)
    postings = list(connector.discover())
    hydrated = connector.fetch_detail(postings[0])
    assert "ingestion platform" in hydrated.description


def test_remoteok_skips_the_legal_notice_row(fetcher, source_configs):
    postings = discover("remoteok", fetcher, source_configs)
    assert all(p.title for p in postings)
    assert not any("legal" in p.raw for p in postings)


def test_hackernews_parses_the_pipe_convention(fetcher, source_configs):
    postings = discover("hackernews", fetcher, source_configs)
    assert len(postings) == 1, "the non-posting comment should have been dropped"
    posting = postings[0]
    assert posting.company == "Thread Systems"
    assert posting.title == "Senior Backend Engineer"
    assert posting.remote is True


def test_linkedin_parses_guest_cards(fetcher, source_configs):
    postings = discover("linkedin", fetcher, source_configs)
    assert {p.company for p in postings} == {"Demo Labs", "Northwind Systems"}
    assert all(p.url.startswith("https://www.linkedin.com/jobs/view/") for p in postings)


def test_wellfound_extracts_next_data(fetcher, source_configs):
    posting = discover("wellfound", fetcher, source_configs)[0]
    assert posting.company == "Seedling Inc"
    assert posting.compensation_raw == "$160,000 - $200,000"


def test_offline_mode_never_touches_the_network(fetcher, source_configs, monkeypatch):
    import httpx

    def explode(*args, **kwargs):
        raise AssertionError("offline mode must not make requests")

    monkeypatch.setattr(httpx.Client, "send", explode)
    for slug in REGISTRY:
        assert discover(slug, fetcher, source_configs)


def test_a_missing_fixture_fails_loudly(source_configs):
    empty = Fetcher(fixtures_dir=Path("does-not-exist"), offline=True)
    with pytest.raises(FixtureMissing):
        discover("greenhouse", empty, source_configs)


# -- html helpers ----------------------------------------------------------


def test_strip_html_preserves_list_structure():
    text = strip_html("<ul><li>First item</li><li>Second item</li></ul>")
    assert "- First item" in text
    assert "- Second item" in text


def test_strip_html_decodes_entities():
    assert strip_html("<p>R&amp;D &mdash; 24&#37; faster</p>") == "R&D - 24% faster"


@pytest.mark.parametrize(
    "values,expected",
    [
        (("Remote - US",), True),
        (("Austin, TX (Hybrid)",), False),
        (("Austin, TX",), None),
        ((None, ""), None),
    ],
)
def test_looks_remote_is_tri_state(values, expected):
    assert looks_remote(*values) is expected
