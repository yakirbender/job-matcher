"""Command-line entry point: `jm`."""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Annotated

import typer
from pydantic import ValidationError
from rich.console import Console
from rich.table import Table

from job_matcher import db, pipeline
from job_matcher.config import DEFAULT_CONFIG_PATH, Settings, load_search_config
from job_matcher.sources import SourceAuthError, SourceError, build_source

app = typer.Typer(no_args_is_help=True, help="Fetch, filter, and score software job postings.")
console = Console()

ConfigOption = Annotated[
    Path, typer.Option("--config", "-c", help="Search configuration file.", exists=True)
]


@app.command()
def fetch(
    region: Annotated[
        list[str] | None,
        typer.Option("--region", "-r", help="Only these regions (repeatable). Default: all."),
    ] = None,
    term: Annotated[
        list[str] | None,
        typer.Option("--term", "-t", help="Only these exact terms from the config (repeatable)."),
    ] = None,
    config: ConfigOption = DEFAULT_CONFIG_PATH,
    max_pages: Annotated[
        int | None, typer.Option(min=1, help="Override pages per search (50 results each).")
    ] = None,
    limit: Annotated[
        int | None, typer.Option(min=1, help="Stop after this many API requests in this run.")
    ] = None,
    all_pages: Annotated[
        bool, typer.Option("--all-pages", help="Don't stop a search when a page has nothing new.")
    ] = False,
    plan: Annotated[
        bool, typer.Option("--plan", help="List the searches and request budget; call nothing.")
    ] = False,
) -> None:
    """Pull new postings from the configured source into the database."""
    try:
        cfg = load_search_config(config)
        specs = cfg.specs(regions=region, max_pages=max_pages, terms=term)
    except (ValidationError, ValueError) as exc:
        console.print(f"[red]Config error:[/red] {exc}")
        raise typer.Exit(2) from None

    if plan:
        table = Table("Region", "Country", "Where", "Term", "Pages")
        for s in specs:
            where = "(remote)" if s.remote else f"{s.where} ({s.distance_km or '-'} km)"
            table.add_row(s.region, s.country, where, s.term, str(s.max_pages))
        console.print(table)
        most = sum(s.max_pages for s in specs)
        if limit is not None:
            most = min(most, limit)
        monthly = f", {cfg.limits.per_month} per 30 days" if cfg.limits.per_month else ""
        console.print(
            f"{len(specs)} searches, at most {most} requests "
            f"(limits: {cfg.limits.per_day} per 24 h{monthly})."
        )
        return

    settings = Settings.from_env()
    conn = db.connect(settings.db_path)
    try:
        source = build_source(cfg.source, settings, cfg)
        with console.status(f"Running {len(specs)} searches against {cfg.source}..."):
            report = pipeline.fetch(
                conn,
                source,
                specs,
                daily_limit=cfg.limits.per_day,
                monthly_limit=cfg.limits.per_month,
                run_limit=limit,
                early_stop=not all_pages,
            )
    except SourceAuthError as exc:
        console.print(f"[red]Authentication failed:[/red] {exc}")
        raise typer.Exit(1) from None
    except SourceError as exc:
        console.print(f"[red]Source error:[/red] {exc}")
        raise typer.Exit(1) from None
    finally:
        conn.close()

    console.print(
        f"Run {report.run_id}: {report.searches_run}/{report.searches_total} searches, "
        f"{report.requests} requests. "
        f"[green]{report.new} new[/green], {report.updated} updated, "
        f"{report.unchanged} unchanged, {report.likely_duplicates} likely duplicates."
    )
    if report.new_by_region:
        console.print(
            "New by region: "
            + ", ".join(f"{r} {n}" for r, n in Counter(report.new_by_region).most_common())
        )
    if report.stopped_reason:
        console.print(f"[yellow]Stopped early:[/yellow] {report.stopped_reason}")
    for err in report.errors:
        console.print(f"[yellow]Error:[/yellow] {err}")


@app.command()
def stats() -> None:
    """Summarize what's in the database."""
    settings = Settings.from_env()
    conn = db.connect(settings.db_path)
    try:
        s = db.stats(conn)
    finally:
        conn.close()

    console.print(f"{s['total']} jobs, {s['likely_duplicates']} flagged as likely duplicates.")
    for title, key in (
        ("Metro", "by_metro"),
        ("Work mode", "by_work_mode"),
        ("Search region", "by_region"),
    ):
        table = Table(title, "Jobs")
        for name, count in s[key]:
            table.add_row(str(name), str(count))
        console.print(table)

    runs = Table("Run", "Started (UTC)", "Status", "Requests", "New", "Updated", "Unchanged",
                 title="Recent runs")
    for row in s["recent_runs"]:
        runs.add_row(*(str(v) for v in row))
    console.print(runs)


if __name__ == "__main__":
    app()
