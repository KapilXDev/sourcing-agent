"""Run the full pipeline against fixtures with a stubbed model.

No API key, no network, no spend - but every other part of the system is the
real one: the 13 connectors, dedupe, the gate, the three-stage funnel, budget
accounting at real list prices, and routing. The only substitution is the
backend, which returns canned structured output in place of Claude.

    python scripts/demo_run.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from tests.conftest import scripted_handler  # noqa: E402

from sourcing_agent.agent import SourcingAgent  # noqa: E402
from sourcing_agent.cli import _print_report  # noqa: E402
from sourcing_agent.config import load_settings  # noqa: E402
from sourcing_agent.llm import ScriptedBackend  # noqa: E402


def main() -> None:
    settings = load_settings(ROOT / "profiles" / "example.yaml")
    settings.offline = True
    settings.fixtures_dir = ROOT / "fixtures"
    settings.db_path = ROOT / "demo.db"
    settings.out_dir = ROOT / "out"

    if settings.db_path.exists():
        settings.db_path.unlink()

    agent = SourcingAgent(settings, backend=ScriptedBackend(handler=scripted_handler()))
    _print_report(agent.run())

    print(f"\napplication packets written to {settings.out_dir / 'applications'}")


if __name__ == "__main__":
    main()
