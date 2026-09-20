"""Command line interface."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table

from .agent import SourcingAgent
from .capabilities import Route
from .config import load_settings
from .connectors import REGISTRY
from .connectors.registry import submit_capable_slugs
from .gate import DecisionGate
from .ledger import PRICING
from .llm import LLMUnavailable
from .models import RunReport
from .store import Store

app = typer.Typer(
    add_completion=False,
    help="Autonomous job sourcing over 13 sources, with a hard spend cap.",
)
console = Console()

ProfileOpt = typer.Option("profiles/example.yaml", "--profile", "-p", help="Profile YAML.")
SourceOpt = typer.Option(None, "--source", "-s", help="Limit to these sources (repeatable).")


# --------------------------------------------------------------------------


@app.command()
def sources() -> None:
    """List the 13 connectors and their submission rights."""
    table = Table(title="Connectors", show_lines=False)
    table.add_column("slug", style="cyan", no_wrap=True)
    table.add_column("kind")
    table.add_column("submit", justify="center")
    table.add_column("credential")
    table.add_column("basis", overflow="fold")

    for slug in sorted(REGISTRY, key=lambda s: (not REGISTRY[s].can_submit(), s)):
        cls = REGISTRY[slug]
        allowed = cls.can_submit()
        table.add_row(
            slug,
            cls.kind,
            "[green]yes[/green]" if allowed else "[dim]no[/dim]",
            cls.rights.requires_credential or "[dim]-[/dim]",
            cls.rights.basis,
        )

    console.print(table)
    submitting = sum(1 for c in REGISTRY.values() if c.can_submit())
    console.print(
        f"\n{len(REGISTRY)} sources, {submitting} with submission rights. "
        "Rights are a property of the connector class - configuration can "
        "narrow them, never widen them."
    )


@app.command()
def models() -> None:
    """Show the price table the spend cap is enforced against."""
    table = Table(title="Pricing (USD per million tokens)")
    table.add_column("model", style="cyan")
    table.add_column("input", justify="right")
    table.add_column("output", justify="right")
    table.add_column("cache write", justify="right")
    table.add_column("cache read", justify="right")
    for name, price in PRICING.items():
        table.add_row(
            name,
            f"${price.input_per_mtok:.2f}",
            f"${price.output_per_mtok:.2f}",
            f"${price.cache_write_per_mtok:.2f}",
            f"${price.cache_read_per_mtok:.2f}",
        )
    console.print(table)


@app.command()
def discover(
    profile: Path = ProfileOpt,
    source: Optional[list[str]] = SourceOpt,
    limit: int = typer.Option(25, help="Rows to display."),
) -> None:
    """Crawl the enabled sources and store what comes back. No model calls."""
    settings = load_settings(profile)
    agent = SourcingAgent(settings)
    errors: list[str] = []

    with console.status("crawling sources..."):
        postings = agent.discover(source, errors)

    unique = agent.store.dedupe(postings, submit_capable_slugs())
    stored, duplicates = agent.store.upsert_postings(unique)

    by_source = Counter(p.source for p in postings)
    table = Table(title=f"{len(postings)} postings, {len(unique)} unique")
    table.add_column("source", style="cyan")
    table.add_column("found", justify="right")
    for slug, count in by_source.most_common():
        table.add_row(slug, str(count))
    console.print(table)

    preview = Table(title="Sample")
    preview.add_column("source", style="cyan", no_wrap=True)
    preview.add_column("title", overflow="ellipsis", max_width=44)
    preview.add_column("company", overflow="ellipsis", max_width=22)
    preview.add_column("location", overflow="ellipsis", max_width=22)
    for posting in unique[:limit]:
        preview.add_row(posting.source, posting.title, posting.company, posting.location or "-")
    console.print(preview)

    console.print(f"\nstored {stored}, cross-source duplicates collapsed: {duplicates}")
    _print_errors(errors)


@app.command()
def gate(
    profile: Path = ProfileOpt,
    source: Optional[list[str]] = SourceOpt,
    show: int = typer.Option(15, help="Rows per table."),
    stored: bool = typer.Option(False, "--stored", help="Use the local corpus, skip crawling."),
) -> None:
    """Run the deterministic gate and show exactly why postings were dropped.

    Costs nothing. This is the command to iterate a profile against.
    """
    settings = load_settings(profile)
    agent = SourcingAgent(settings)
    errors: list[str] = []

    if stored:
        postings = agent.store.all_postings()
    else:
        with console.status("crawling sources..."):
            postings = agent.store.dedupe(
                agent.discover(source, errors), submit_capable_slugs()
            )
        agent.store.upsert_postings(postings)

    passed, rejected = agent.gate_postings(postings, errors)

    kept = Table(title=f"passed the gate: {len(passed)}")
    kept.add_column("score", justify="right", style="green")
    kept.add_column("source", style="cyan", no_wrap=True)
    kept.add_column("title", overflow="ellipsis", max_width=42)
    kept.add_column("company", overflow="ellipsis", max_width=20)
    kept.add_column("matched", overflow="ellipsis", max_width=30)
    for posting, decision in passed[:show]:
        kept.add_row(
            str(decision.prefilter_score),
            posting.source,
            posting.title,
            posting.company,
            ", ".join(decision.matched_keywords),
        )
    console.print(kept)

    reasons = Counter(r.split("(")[0] for _, d in rejected for r in d.rejections)
    dropped = Table(title=f"rejected: {len(rejected)} (zero tokens spent)")
    dropped.add_column("rule", style="yellow")
    dropped.add_column("count", justify="right")
    for rule, count in reasons.most_common():
        dropped.add_row(rule, str(count))
    console.print(dropped)
    _print_errors(errors)


@app.command()
def run(
    profile: Path = ProfileOpt,
    source: Optional[list[str]] = SourceOpt,
    budget: Optional[float] = typer.Option(None, help="Override the run's spend cap (USD)."),
    no_funnel: bool = typer.Option(False, "--no-funnel", help="Stop after the gate."),
    submit: bool = typer.Option(
        False,
        "--submit",
        help="Opt into real submission. Requires submission.enabled and an allowlist.",
    ),
    live: bool = typer.Option(
        False, "--live", help="Turn off dry-run mode. Applications are actually sent."
    ),
) -> None:
    """Run the full pipeline: discover, dedupe, gate, funnel, route."""
    settings = load_settings(profile)
    if budget is not None:
        settings.budget.cap_usd = budget
    if submit:
        settings.submission.enabled = True
    if live:
        settings.submission.dry_run = False

    if settings.submission.enabled and not settings.submission.dry_run:
        allowed = ", ".join(settings.submission.allow_connectors) or "none"
        console.print(
            Panel(
                f"Live submission is on.\nAllowlisted connectors: {allowed}\n"
                f"Limit: {settings.submission.max_per_run} per run.",
                title="[red]applications will be transmitted[/red]",
                border_style="red",
            )
        )
        typer.confirm("Continue?", abort=True)

    agent = SourcingAgent(settings)
    try:
        with console.status("running..."):
            report = agent.run(source, skip_funnel=no_funnel)
    except LLMUnavailable as exc:
        # Escape: the message contains "[llm]", which rich would eat as markup.
        console.print(f"[red]{escape(str(exc))}[/red]")
        console.print("Run with --no-funnel to exercise discovery and the gate only.")
        raise typer.Exit(code=2)

    _print_report(report)


@app.command()
def budget(
    profile: Path = ProfileOpt,
    runs: int = typer.Option(10, help="How many recent runs to show."),
) -> None:
    """Show what recent runs cost, and where the money went."""
    settings = load_settings(profile)
    store = Store(settings.resolve(settings.db_path))

    table = Table(title="Recent runs")
    table.add_column("run", style="cyan", no_wrap=True)
    table.add_column("started")
    table.add_column("cap", justify="right")
    table.add_column("spent", justify="right")
    table.add_column("stages", overflow="fold")

    for row in store.last_runs(runs):
        stages = store.spend_by_stage(row["run_id"])
        table.add_row(
            row["run_id"],
            (row["started_at"] or "")[:19],
            f"${row['budget_usd']:.2f}",
            f"${row['spend_usd']:.4f}",
            "  ".join(f"{k}=${v:.4f}" for k, v in sorted(stages.items())) or "-",
        )
    console.print(table)


@app.command()
def explain(
    key: str = typer.Argument(..., help="Posting key, e.g. greenhouse:4012345"),
    profile: Path = ProfileOpt,
) -> None:
    """Explain the gate's verdict on one stored posting, rule by rule."""
    settings = load_settings(profile)
    store = Store(settings.resolve(settings.db_path))
    posting = store.get_posting(key)
    if posting is None:
        console.print(f"[red]no posting stored with key {key}[/red]")
        raise typer.Exit(code=1)

    decision_gate = DecisionGate(settings.profile, settings.gate)
    decision = decision_gate.evaluate(posting)

    console.print(
        Panel(
            f"{posting.title}\n{posting.company} - {posting.location or 'unspecified'}\n"
            f"{posting.url}",
            title=key,
        )
    )
    table = Table(show_header=True)
    table.add_column("rule")
    table.add_column("verdict")
    fired = {r.split("(")[0]: r for r in decision.rejections}
    for rule in decision_gate.rules:
        hit = fired.get(rule.id)
        table.add_row(
            rule.id,
            f"[red]{hit}[/red]" if hit else "[green]ok[/green]",
        )
    console.print(table)
    console.print(
        f"\nresult: {'[green]passed[/green]' if decision.passed else '[red]rejected[/red]'}"
        f"  score={decision.prefilter_score}"
    )


@app.command("init-db")
def init_db(profile: Path = ProfileOpt) -> None:
    """Create the SQLite schema."""
    settings = load_settings(profile)
    path = settings.resolve(settings.db_path)
    Store(path)
    console.print(f"initialised {path}")


# --------------------------------------------------------------------------


def _print_errors(errors: list[str]) -> None:
    if not errors:
        return
    console.print("\n[yellow]source errors[/yellow]")
    for error in errors:
        console.print(f"  - {error}")


def _print_report(report: RunReport) -> None:
    funnel = Table(title=f"run {report.run_id}")
    funnel.add_column("stage", style="cyan")
    funnel.add_column("model")
    funnel.add_column("in", justify="right")
    funnel.add_column("out", justify="right")
    funnel.add_column("cost", justify="right")
    funnel.add_column("skipped", justify="right")

    funnel.add_row("discover", "-", "-", str(report.discovered), "$0.0000", "-")
    funnel.add_row(
        "dedupe", "-", str(report.discovered), str(report.discovered - report.deduped), "$0.0000", "-"
    )
    funnel.add_row(
        "gate",
        "[dim]none[/dim]",
        str(report.gate_passed + report.gate_rejected),
        str(report.gate_passed),
        "$0.0000",
        "-",
    )
    for stage in report.stages:
        funnel.add_row(
            stage.name,
            stage.model or "-",
            str(stage.considered),
            str(stage.advanced),
            f"${stage.cost_usd:.4f}",
            str(stage.skipped_for_budget) if stage.skipped_for_budget else "-",
        )
    console.print(funnel)

    console.print(
        f"\nspend: [bold]${report.spend_usd:.4f}[/bold] of ${report.budget_usd:.2f} cap"
        + ("  [yellow](cap reached - funnel truncated)[/yellow]" if report.budget_exhausted else "")
    )

    if report.receipts:
        receipts = Table(title="routing")
        receipts.add_column("posting", style="cyan", no_wrap=True)
        receipts.add_column("route")
        receipts.add_column("detail", overflow="fold")
        for receipt in report.receipts:
            colour = "green" if receipt.route is Route.SUBMIT else "blue"
            label = receipt.route.value + (" (dry run)" if receipt.dry_run else "")
            receipts.add_row(receipt.posting_key, f"[{colour}]{label}[/{colour}]", receipt.detail)
        console.print(receipts)

    _print_errors(report.errors)


@app.command()
def report(
    profile: Path = ProfileOpt,
    run_id: Optional[str] = typer.Option(None, help="Defaults to the most recent run."),
) -> None:
    """Re-print the report for a past run."""
    settings = load_settings(profile)
    store = Store(settings.resolve(settings.db_path))
    rows = store.last_runs(50)
    match = next((r for r in rows if run_id is None or r["run_id"] == run_id), None)
    if match is None or not match["report_json"]:
        console.print("[red]no stored report for that run[/red]")
        raise typer.Exit(code=1)
    _print_report(RunReport.model_validate(json.loads(match["report_json"])))


if __name__ == "__main__":  # pragma: no cover
    app()
