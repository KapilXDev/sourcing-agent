"""Configuration: one YAML profile drives the whole run.

Everything that shapes behaviour - what you want, what the gate rejects, which
model runs each stage, the spend cap, which sources are enabled - lives in a
single validated file. Nothing is tuned by editing code.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator


class WorkAuthorization(BaseModel):
    model_config = ConfigDict(extra="forbid")

    country: str = "US"
    needs_sponsorship: bool = False
    has_clearance: bool = False


class Profile(BaseModel):
    """Who you are and what you will accept."""

    model_config = ConfigDict(extra="forbid")

    name: str
    email: str
    phone: str | None = None
    location: str | None = None
    links: dict[str, str] = Field(default_factory=dict)

    titles: list[str] = Field(default_factory=list)
    """Regex-ish title fragments that qualify a posting, e.g. ``backend engineer``."""

    exclude_titles: list[str] = Field(default_factory=list)
    seniority: list[str] = Field(default_factory=lambda: ["mid", "senior"])

    must_have_any: list[str] = Field(default_factory=list)
    """Skill keywords; the gate requires ``gate.min_keyword_hits`` of these."""

    nice_to_have: list[str] = Field(default_factory=list)
    exclude_keywords: list[str] = Field(default_factory=list)
    exclude_companies: list[str] = Field(default_factory=list)

    locations: list[str] = Field(default_factory=list)
    remote_only: bool = False
    work_authorization: WorkAuthorization = Field(default_factory=WorkAuthorization)
    min_base_salary: int | None = None

    resume_path: Path | None = None
    resume_text: str = ""
    summary: str = ""

    def resolved_resume_text(self, base_dir: Path) -> str:
        """Inline resume text wins; otherwise read ``resume_path``."""
        if self.resume_text.strip():
            return self.resume_text
        if self.resume_path:
            path = self.resume_path
            if not path.is_absolute():
                path = base_dir / path
            if path.exists():
                return path.read_text(encoding="utf-8")
        return ""


class GateConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_age_days: int = 30
    min_description_chars: int = 240
    min_keyword_hits: int = 2
    max_to_funnel: int = 250
    """Hard ceiling on how many postings may reach stage 1, however many pass."""

    skip_seen_days: int = 14
    """A posting already assessed this recently is not re-assessed."""


class StageConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str
    max_tokens: int
    keep_top: int | None = None
    batch_size: int | None = None
    effort: Literal["low", "medium", "high", "xhigh", "max"] | None = None
    thinking: bool = False
    min_score: int | None = None


class FunnelConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    triage: StageConfig = Field(
        default_factory=lambda: StageConfig(
            model="claude-haiku-4-5", max_tokens=4000, batch_size=12
        )
    )
    fit: StageConfig = Field(
        default_factory=lambda: StageConfig(
            model="claude-sonnet-5", max_tokens=4000, keep_top=40, thinking=True
        )
    )
    draft: StageConfig = Field(
        default_factory=lambda: StageConfig(
            model="claude-opus-5",
            max_tokens=8000,
            keep_top=5,
            effort="high",
            thinking=True,
            min_score=70,
        )
    )


class BudgetConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cap_usd: float = Field(default=2.50, gt=0)
    """Hard cap for a single run. Enforced before each call, not after."""

    reserve_headroom: float = Field(default=1.0, ge=1.0, le=2.0)
    """Safety multiplier applied to the pre-flight estimate."""


class SubmissionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    """Master switch. Off means every packet is exported, whatever rights a
    connector holds."""

    dry_run: bool = True
    """On means submit-capable connectors build and validate the request but do
    not transmit it."""

    max_per_run: int = 3
    allow_connectors: list[str] = Field(default_factory=list)
    """Empty means 'no connector is opted in'. Submission is allowlist-only."""


class SourceConfig(BaseModel):
    model_config = ConfigDict(extra="allow")

    enabled: bool = True
    companies: list[str] = Field(default_factory=list)
    """Board tokens for per-company ATS sources (Greenhouse, Lever, ...)."""

    query: str | None = None
    limit: int = 100


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile: Profile
    gate: GateConfig = Field(default_factory=GateConfig)
    funnel: FunnelConfig = Field(default_factory=FunnelConfig)
    budget: BudgetConfig = Field(default_factory=BudgetConfig)
    submission: SubmissionConfig = Field(default_factory=SubmissionConfig)
    sources: dict[str, SourceConfig] = Field(default_factory=dict)

    db_path: Path = Path("sourcing.db")
    out_dir: Path = Path("out")
    offline: bool = False
    fixtures_dir: Path = Path("fixtures")

    base_dir: Path = Field(default=Path("."), exclude=True)

    @model_validator(mode="after")
    def _apply_env(self) -> "Settings":
        if os.environ.get("SOURCING_OFFLINE") == "1":
            object.__setattr__(self, "offline", True)
        return self

    def source(self, slug: str) -> SourceConfig:
        return self.sources.get(slug, SourceConfig(enabled=False))

    def resolve(self, path: Path) -> Path:
        """Runtime paths (db, out, fixtures) are relative to the working
        directory; only ``resume_path`` is relative to the profile file, since
        that is the one a profile author thinks of as living beside it."""
        return path if path.is_absolute() else Path.cwd() / path


def load_settings(path: str | os.PathLike[str]) -> Settings:
    """Load and validate a profile YAML."""
    p = Path(path).resolve()
    if not p.exists():
        raise FileNotFoundError(f"profile not found: {p}")
    data: dict[str, Any] = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    settings = Settings.model_validate(data)
    object.__setattr__(settings, "base_dir", p.parent)
    return settings
