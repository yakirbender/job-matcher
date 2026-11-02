"""Command-line entry point: `jm`."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Annotated

import typer
from pydantic import ValidationError
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from job_matcher import db, pipeline
from job_matcher.config import DEFAULT_CONFIG_PATH, Settings, load_search_config
from job_matcher.profile import DEFAULT_PROFILE_PATH, load_profile
from job_matcher.sources import SourceAuthError, SourceError, build_source

app = typer.Typer(no_args_is_help=True, help="Fetch, filter, and score software job postings.")
console = Console(emoji=False)  # tag values like "secret:active" aren't emoji codes

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
    title_only: Annotated[
        bool | None,
        typer.Option(
            "--title-only/--full-text",
            help="Match terms in titles only, or anywhere in the posting. Default: config.",
            show_default=False,
        ),
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
        specs = cfg.specs(regions=region, max_pages=max_pages, terms=term, title_only=title_only)
    except (ValidationError, ValueError) as exc:
        console.print(f"[red]Config error:[/red] {exc}")
        raise typer.Exit(2) from None

    if plan:
        table = Table("Region", "Country", "Where", "Term", "Match", "Pages")
        for s in specs:
            where = "(remote)" if s.remote else f"{s.where} ({s.distance_km or '-'} km)"
            match = "title" if s.title_only else "full text"
            table.add_row(s.region, s.country, where, s.term, match, str(s.max_pages))
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


ProfileOption = Annotated[
    Path, typer.Option("--profile", "-p", help="Job-search profile.", exists=True)
]


@app.command("filter")
def filter_jobs(profile_path: ProfileOption = DEFAULT_PROFILE_PATH) -> None:
    """Tag every stored job and apply the profile's hard filters."""
    try:
        profile = load_profile(profile_path)
    except (ValidationError, ValueError) as exc:
        console.print(f"[red]Profile error:[/red] {exc}")
        raise typer.Exit(2) from None

    settings = Settings.from_env()
    conn = db.connect(settings.db_path)
    try:
        report = pipeline.screen(conn, profile)
    finally:
        conn.close()

    console.print(
        f"Screened {report.total} jobs: [green]{report.passed} passed[/green], "
        f"{report.total - report.passed} rejected."
    )
    if report.reasons:
        table = Table("Rejection reason", "Jobs", title="A job can have several reasons")
        for code, count in report.reasons.most_common():
            table.add_row(code, str(count))
        console.print(table)


@app.command("list")
def list_jobs(
    rejected: Annotated[bool, typer.Option("--rejected", help="Show rejected jobs.")] = False,
    reason: Annotated[
        str | None, typer.Option(help="Only rejected jobs with this reason code.")
    ] = None,
    metro: Annotated[str | None, typer.Option(help="Only this metro.")] = None,
    employment: Annotated[
        str | None,
        typer.Option(help="Only full_time, part_time, either, or unknown."),
    ] = None,
    limit: Annotated[int, typer.Option(min=1)] = 50,
) -> None:
    """List screened jobs, newest first, in separate full-time / part-time sections."""
    settings = Settings.from_env()
    conn = db.connect(settings.db_path)
    try:
        rows = db.screened_jobs(
            conn, passed=not (rejected or reason), reason=reason, metro=metro,
            employment=employment, limit=limit,
        )
    finally:
        conn.close()

    show_reasons = rejected or reason is not None
    for value, title in EMPLOYMENT_SECTIONS:
        section = [r for r in rows if r["employment"] == value]
        if section:
            console.print(_jobs_table(section, f"{title} ({len(section)})", show_reasons))
    console.print(f"{len(rows)} jobs shown (limit {limit}).")


EMPLOYMENT_SECTIONS = (
    ("full_time", "Full-time"),
    ("part_time", "Part-time"),
    ("either", "Full- or part-time"),
    ("unknown", "Employment type not stated"),
)


def _jobs_table(rows: list, title: str, show_reasons: bool) -> Table:
    columns = ["ID", "Title", "Company", "Metro", "Mode", "Exp", "Family", "Signals"]
    table = Table(*columns, *(["Reasons"] if show_reasons else []), title=title)
    for r in rows:
        signals = []
        if r["suspicious"]:
            signals.append(f"[bold red]SUSPECT:{r['suspicious']}[/bold red]")
        if r["entry_level"]:
            signals.append(f"entry({r['entry_level']})")
        if r["french"]:
            signals.append(f"fr:{r['french']}")
        if r["clearance"]:
            signals.append(f"clr:{r['clearance']}")
        cells = [
            str(r["id"]), escape(r["title"][:50]), escape((r["company"] or "")[:24]),
            r["metro"] or "-",
            r["work_mode"] or "?", r["experience"] or "?", r["role_family"] or "-",
            " ".join(signals),
        ]
        if show_reasons:
            cells.append(", ".join(x["code"] for x in json.loads(r["reasons_json"])))
        table.add_row(*cells)
    return table


@app.command()
def show(job_id: int) -> None:
    """Everything known about one job: fields, tags with evidence, filter result."""
    settings = Settings.from_env()
    conn = db.connect(settings.db_path)
    try:
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            console.print(f"[red]No job {job_id}.[/red]")
            raise typer.Exit(1)
        tags = db.job_tags(conn, job_id)
        result = conn.execute(
            "SELECT * FROM filter_results WHERE job_id = ?", (job_id,)
        ).fetchone()
    finally:
        conn.close()

    console.print(f"[bold]{row['title']}[/bold] — {row['company'] or 'unknown company'}")
    console.print(
        f"{row['location_display'] or '-'} | metro {row['metro'] or '-'} | "
        f"work mode {row['work_mode'] or 'unknown'} | posted {row['posted_at'] or '-'}"
    )
    console.print(row["url"])
    console.print(f"\n{row['description']}\n")
    if result is None:
        console.print("Not screened yet; run `jm filter`.")
        return
    verdict = "[green]PASSED[/green]" if result["passed"] else "[red]REJECTED[/red]"
    console.print(verdict)
    for item in json.loads(result["reasons_json"]):
        console.print(f"  reason  {item['code']}: {item['detail']}")
    for item in json.loads(result["notes_json"]):
        console.print(f"  note    {item['code']}: {item['detail']}")
    table = Table("Tag", "Value", "Evidence")
    for t in tags:
        table.add_row(t["name"], t["value"], t["evidence"] or "")
    console.print(table)


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
