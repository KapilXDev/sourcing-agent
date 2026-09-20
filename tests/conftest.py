from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from sourcing_agent.config import Settings, load_settings
from sourcing_agent.llm import Call, ScriptedBackend
from sourcing_agent.models import (
    ApplicationDraft,
    FitAssessment,
    Posting,
    TriageBatch,
    TriageItem,
)
from sourcing_agent.store import Store

ROOT = Path(__file__).resolve().parent.parent
PROFILE = ROOT / "profiles" / "example.yaml"


@pytest.fixture
def now() -> datetime:
    return datetime.now(timezone.utc)


@pytest.fixture
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    """The shipped example profile, pointed at a temp database and output dir.

    Tests run against the same configuration a user gets, so a profile change
    that breaks the pipeline breaks the suite.
    """
    monkeypatch.chdir(tmp_path)
    loaded = load_settings(PROFILE)
    loaded.offline = True
    loaded.fixtures_dir = ROOT / "fixtures"
    loaded.db_path = tmp_path / "test.db"
    loaded.out_dir = tmp_path / "out"
    return loaded


@pytest.fixture
def store(tmp_path: Path) -> Store:
    with Store(tmp_path / "store.db") as s:
        yield s


def make_posting(**overrides) -> Posting:
    base = dict(
        source="greenhouse",
        source_id="1",
        url="https://example.com/jobs/1",
        title="Senior Backend Engineer",
        company="Acme",
        location="Remote - US",
        remote=True,
        description=(
            "We run distributed services in Python and Go on Kubernetes with a "
            "large Postgres fleet. You will own ingestion end to end, including "
            "on-call. We look for strong distributed systems fundamentals and "
            "comfort with AWS primitives in production environments."
        ),
        posted_at=datetime.now(timezone.utc) - timedelta(days=2),
        apply_handle="acme/1",
    )
    base.update(overrides)
    return Posting(**base)


@pytest.fixture
def posting() -> Posting:
    return make_posting()


def scripted_handler(
    keep: bool = True,
    fit_score: int = 88,
    proceed: bool = True,
    dealbreakers: list[str] | None = None,
):
    """Build a handler that answers every stage plausibly."""

    def handle(call: Call):
        if call.schema is TriageBatch:
            count = call.user.count("### ref ")
            return TriageBatch(
                results=[
                    TriageItem(ref=i, keep=keep, reason="scripted", confidence=0.9)
                    for i in range(count)
                ]
            )
        if call.schema is FitAssessment:
            return FitAssessment(
                fit_score=fit_score,
                seniority="senior",
                remote_policy="remote",
                sponsorship="unclear",
                min_years_experience=5,
                salary_min=170000,
                salary_max=210000,
                must_have_skills=["python", "kubernetes"],
                matched_strengths=["ingestion pipelines"],
                gaps=["no Kafka in production"],
                dealbreakers=dealbreakers or [],
                summary="scripted assessment",
            )
        if call.schema is ApplicationDraft:
            return ApplicationDraft(
                proceed=proceed,
                rationale="scripted",
                cover_letter="Dear team, scripted cover letter.",
                resume_bullets=["Built the ingestion pipeline"],
                screening_answers=[],
                tailoring_notes=["mention Postgres sharding"],
            )
        raise AssertionError(f"unexpected schema {call.schema}")

    return handle


@pytest.fixture
def backend() -> ScriptedBackend:
    return ScriptedBackend(handler=scripted_handler())


@pytest.fixture(autouse=True)
def clean_credentials(monkeypatch: pytest.MonkeyPatch):
    """No test may accidentally pick up a real API key from the environment."""
    for name in (
        "ANTHROPIC_API_KEY",
        "GREENHOUSE_API_KEY",
        "LEVER_API_KEY",
        "ASHBY_API_KEY",
        "WORKABLE_API_KEY",
        "SMARTRECRUITERS_API_KEY",
        "SOURCING_RECORD",
    ):
        monkeypatch.delenv(name, raising=False)
    os.environ["SOURCING_OFFLINE"] = "1"
    yield
    os.environ.pop("SOURCING_OFFLINE", None)
