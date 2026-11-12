"""Command-line entry point: `jm`."""

from __future__ import annotations

import json
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated

import httpx
import typer
from pydantic import ValidationError
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from job_matcher import db, labels, pipeline
from job_matcher.config import DEFAULT_CONFIG_PATH, Settings, load_search_config
from job_matcher.cv import (
    CvError,
    CvProfile,
    Vocabulary,
    default_cv_path,
    load_cv,
    load_vocabulary,
)
from job_matcher.discovery import BOARD_HOSTS, CdxClient, CdxError
from job_matcher.pipeline import BoardReader
from job_matcher.profile import DEFAULT_PROFILE_PATH, load_profile
from job_matcher.scoring import KeywordScorer
from job_matcher.sources import SourceAuthError, SourceError, build_source
from job_matcher.sources.boards import READERS, BoardClient

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


CvOption = Annotated[
    Path | None,
    typer.Option("--cv", help="CV file (.pdf, .docx, .md, .txt). Default: private/cv.*"),
]


def _load_cv(cv_path: Path | None) -> tuple[CvProfile, Vocabulary]:
    path = cv_path or default_cv_path()
    if path is None or not path.exists():
        console.print("[red]No CV found.[/red] Put it at private/cv.pdf (or .docx/.md/.txt).")
        raise typer.Exit(2)
    vocabulary = load_vocabulary()
    try:
        return load_cv(path, vocabulary), vocabulary
    except CvError as exc:
        console.print(f"[red]CV error:[/red] {exc}")
        raise typer.Exit(2) from None


@app.command()
def cv(cv_path: CvOption = None) -> None:
    """Show what's extracted from your CV: sections and recognized skills."""
    profile, _ = _load_cv(cv_path)
    console.print(f"Sections: {', '.join(k for k, v in profile.sections.items() if v)}")
    table = Table("Skill", "Found in", title=f"{len(profile.skills)} recognized skills")
    for skill, where in profile.skills.items():
        table.add_row(skill, ", ".join(where))
    console.print(table)
    console.print("Missing a skill? Add it to config/skills.yaml.")


@app.command("score")
def score_jobs(
    cv_path: CvOption = None, profile_path: ProfileOption = DEFAULT_PROFILE_PATH
) -> None:
    """Score every job that passed `jm filter` against your CV (free, local)."""
    cv_profile, vocabulary = _load_cv(cv_path)
    profile = load_profile(profile_path)
    scorer = KeywordScorer(cv_profile, profile, vocabulary)
    settings = Settings.from_env()
    conn = db.connect(settings.db_path)
    try:
        report = pipeline.score(conn, scorer, cv_hash=cv_profile.fingerprint, profile=profile)
    finally:
        conn.close()
    b = report.bands
    console.print(
        f"Scored {report.scored} jobs with the {scorer.name} scorer: "
        f"{b['strong']} strong (70+), {b['possible']} possible (50-69), {b['weak']} weak."
    )
    if not report.scored:
        console.print("Nothing to score; run `jm filter` first.")


LabelsOption = Annotated[Path, typer.Option("--file", "-f", help="Labels CSV.")]


@app.command("label")
def label_export(
    count: Annotated[int, typer.Option(min=2, help="How many jobs to label.")] = 15,
    path: LabelsOption = labels.DEFAULT_LABELS_PATH,
    force: Annotated[
        bool, typer.Option("--force", help="Replace an existing labels file.")
    ] = False,
) -> None:
    """Write a file of scored jobs for you to label (scores hidden, order shuffled)."""
    settings = Settings.from_env()
    conn = db.connect(settings.db_path)
    try:
        rows = db.scored_jobs(conn)
    finally:
        conn.close()
    if not rows:
        console.print("No scored jobs yet; run `jm filter` and `jm score` first.")
        raise typer.Exit(1)
    picked = labels.sample_for_labeling(rows, count)
    try:
        labels.write_label_file(path, picked, overwrite=force)
    except FileExistsError as exc:
        console.print(f"[yellow]{exc}.[/yellow] Use --force to replace it.")
        raise typer.Exit(1) from None
    console.print(
        f"Wrote {len(picked)} jobs to {path} (from {len(rows)} scored, at most 2 per company).\n"
        "Fill in the `label` column: 2 = apply, 1 = maybe, 0 = skip. Add a short `reason`, and\n"
        "put yes in `opened_full_posting` if you clicked through. Then run `jm eval`."
    )


@app.command("eval")
def label_eval(path: LabelsOption = labels.DEFAULT_LABELS_PATH) -> None:
    """Compare your labels with the pipeline: agreement, rank correlation, disagreements.

    Jobs the filters rejected count as the pipeline saying "skip"."""
    if not path.exists():
        console.print(f"No labels file at {path}; run `jm label` first.")
        raise typer.Exit(1)
    parsed = labels.read_labels(path)
    for problem in parsed.invalid:
        console.print(f"[yellow]Skipped[/yellow] {problem}")
    if not parsed.labels:
        console.print(f"No labels filled in yet ({parsed.unlabeled} rows still blank).")
        raise typer.Exit(1)

    settings = Settings.from_env()
    conn = db.connect(settings.db_path)
    try:
        scored = {r["id"]: r for r in db.scored_jobs(conn)}
        rejected = db.rejected_jobs(conn)
    finally:
        conn.close()
    report = labels.evaluate(parsed.labels, scored, rejected)

    console.print(
        f"Compared {report.compared} labeled jobs"
        + (f", {report.filtered_out} of them rejected by the filters (counted as skip)"
           if report.filtered_out else "")
        + (f" ({parsed.unlabeled} still blank)" if parsed.unlabeled else "")
        + (f"; {len(report.missing)} not found: {report.missing}" if report.missing else "")
        + "."
    )
    if not report.compared:
        return
    rho = "n/a (no variation)" if report.spearman is None else f"{report.spearman:+.2f}"
    console.print(
        f"Band agreement: {report.band_agreement:.0%}   "
        f"Rank correlation (Spearman): {rho}   (1 = same order, 0 = unrelated)"
    )
    means = ", ".join(f"{labels.LABEL_NAMES[k]} {v:.0f}"
                      for k, v in sorted(report.mean_score_by_label.items(), reverse=True))
    console.print(f"Mean score by your label (scored jobs): {means}  (should fall apply -> skip)")

    columns = (2, 1, 0, labels.FILTERED)
    grid = Table("Your label", "Scorer: strong 70+", "possible 50-69", "weak <50",
                 "filtered out", title="Your label vs the pipeline's verdict")
    for lab in (2, 1, 0):
        grid.add_row(labels.LABEL_NAMES[lab],
                     *(str(report.confusion.get((lab, col), 0)) for col in columns))
    console.print(grid)

    if report.disagreements:
        table = Table("ID", "Title", "You", "Score", "Your reason", "Pipeline's first lines",
                      title="Disagreements, biggest first")
        for d in report.disagreements:
            explanation = escape(" | ".join(d.explanation[:2]))
            table.add_row(str(d.job_id), escape(d.title[:40]), labels.LABEL_NAMES[d.label],
                          "filtered" if d.score is None else str(d.score),
                          escape(d.reason[:40]), explanation)
        console.print(table)
    if scored and max(r["score"] for r in scored.values()) < 70:
        console.print(
            "[yellow]Note:[/yellow] no scored job reaches 70, so every 'apply' counts as a band "
            "disagreement. Until the thresholds or weights change, the rank correlation and "
            "mean score by label are the fairer measures."
        )


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
    links: Annotated[
        bool, typer.Option("--links", help="One job per line with its full link, to open.")
    ] = False,
    stale_days: Annotated[
        float, typer.Option(help="Hide jobs not seen at their source for this many days "
                            "(boards are re-read every 7).")
    ] = 10,
    include_closed: Annotated[
        bool, typer.Option("--include-closed", help="Also show closed and stale jobs.")
    ] = False,
) -> None:
    """List screened jobs, best score first, in separate full-time / part-time sections."""
    settings = Settings.from_env()
    conn = db.connect(settings.db_path)
    try:
        passed = not (rejected or reason)
        open_since = None if include_closed else (
            datetime.now(UTC) - timedelta(days=stale_days)).isoformat(timespec="seconds")
        rows = db.screened_jobs(
            conn, passed=passed, reason=reason, metro=metro,
            employment=employment, open_since=open_since, limit=limit,
        )
        hidden = db.count_hidden(conn, passed=passed, open_since=open_since) if open_since else 0
    finally:
        conn.close()

    show_reasons = rejected or reason is not None
    for value, title in EMPLOYMENT_SECTIONS:
        section = [r for r in rows if r["employment"] == value]
        if section and links:
            _print_links(section, f"{title} ({len(section)})")
        elif section:
            console.print(_jobs_table(section, f"{title} ({len(section)})", show_reasons))
    console.print(f"{len(rows)} jobs shown (limit {limit}).")
    if hidden:
        console.print(f"{hidden} closed or stale jobs hidden (not seen at their source in "
                      f"{stale_days:g} days); --include-closed shows them.")


EMPLOYMENT_SECTIONS = (
    ("full_time", "Full-time"),
    ("part_time", "Part-time"),
    ("either", "Full- or part-time"),
    ("unknown", "Employment type not stated"),
)


def _print_links(rows: list, title: str) -> None:
    """Each job on one line, its link on the next. Kept out of a table, whose columns would
    cut or wrap the link and break Ctrl+click."""
    console.print()
    console.print(f"[bold]{title}[/bold]")
    for r in rows:
        score = "-" if r["score"] is None else str(r["score"])
        where = " · ".join(x for x in (r["metro"], r["work_mode"]) if x)
        console.print(f"{score:>3}  #{r['id']}  {escape(r['title'])} — "
                      f"{escape(r['company'] or 'unknown company')}  [dim]{where}[/dim]")
        console.print(f"     [link={r['url']}]{escape(r['url'])}[/link]", soft_wrap=True)


def _jobs_table(rows: list, title: str, show_reasons: bool) -> Table:
    columns = ["Score", "ID", "Title", "Company", "Metro", "Mode", "Exp", "Family", "Signals"]
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
            "-" if r["score"] is None else str(r["score"]),
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
        scored = db.job_score(conn, job_id)
    finally:
        conn.close()

    console.print(f"[bold]{row['title']}[/bold] — {row['company'] or 'unknown company'}")
    console.print(
        f"{row['location_display'] or '-'} | metro {row['metro'] or '-'} | "
        f"work mode {row['work_mode'] or 'unknown'} | posted {row['posted_at'] or '-'}"
    )
    console.print(row["url"])
    if row["closed_at"]:
        console.print(f"[yellow]Closed:[/yellow] gone from its board since {row['closed_at']}.")
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
    if scored is not None:
        parts = ", ".join(f"{k} {v:+d}" if k not in ("skills", "level") else f"{k} {v}"
                          for k, v in json.loads(scored["components_json"]).items())
        console.print(f"\n[bold]Score {scored['score']}[/bold] ({scored['scorer']}: {parts})")
        for line in json.loads(scored["explanation_json"]):
            console.print(f"  {escape(line)}")
    table = Table("Tag", "Value", "Evidence")
    for t in tags:
        table.add_row(t["name"], t["value"], escape(t["evidence"] or ""))
    console.print(table)


def _index_client() -> CdxClient:
    """The real index client. A function so tests can swap in a fake."""
    http = httpx.Client(
        headers={"User-Agent": "job-matcher/0.1 (personal job search)"}, timeout=120.0
    )
    return CdxClient(http)


@app.command()
def discover(
    crawls: Annotated[
        int, typer.Option(min=1, help="How many of the newest crawls to read.")
    ] = 1,
    ats: Annotated[
        list[str] | None,
        typer.Option(help="Only these systems (repeatable): greenhouse, ashby. Default: both."),
    ] = None,
    limit: Annotated[
        int | None, typer.Option(min=1, help="Stop after this many finished queries.")
    ] = None,
    retry_pause: Annotated[
        float, typer.Option(min=0, help="Seconds to wait before retrying failed queries.")
    ] = 300.0,
) -> None:
    """Find Greenhouse and Ashby job boards in Common Crawl's index. Free, but slow: a
    full run can take hours. Progress is saved after every query, so a stopped run
    resumes where it left off."""
    systems = set(BOARD_HOSTS.values())
    unknown = sorted(set(ats or ()) - systems)
    if unknown:
        console.print(f"[red]Unknown system:[/red] {', '.join(unknown)} "
                      f"(choose from {', '.join(sorted(systems))})")
        raise typer.Exit(2)
    hosts = [host for host, system in BOARD_HOSTS.items() if not ats or system in ats]

    def show_query(crawl: str, pattern: str, status: str, detail: str) -> None:
        colour = {"confirmed": "green", "unconfirmed": "yellow", "failed": "red"}[status]
        console.print(f"{crawl}  {pattern:<32} [{colour}]{status}[/{colour}]  {escape(detail)}")

    client = _index_client()
    conn = db.connect(Settings.from_env().db_path)
    try:
        boards_before = db.board_count(conn)
        chosen = client.crawls(crawls)
        console.print(f"Reading {', '.join(c.id for c in chosen)} for {', '.join(hosts)}.")
        results = pipeline.discover(conn, client, chosen, hosts, limit=limit,
                                    retry_pause=retry_pause, on_query=show_query)
    except CdxError as exc:
        console.print(f"[red]Index error:[/red] {escape(str(exc))}")
        raise typer.Exit(1) from None
    except KeyboardInterrupt:
        console.print("[yellow]Interrupted.[/yellow] Finished queries are saved; "
                      "run the command again to resume.")
        raise typer.Exit(130) from None
    else:
        known = pipeline.check_known_boards(conn)
        total = db.board_count(conn)
    finally:
        conn.close()

    table = Table("Crawl", "Finished", "Still failing", "Stopped", title="Discovery")
    for crawl, report in results:
        table.add_row(crawl.id, str(report.finished), str(len(report.failed)),
                      report.stopped_reason or "-")
    console.print(table)
    console.print(f"{total} boards saved, {total - boards_before} new in this run.")
    for board, outcome in known.items():
        colour = {"found": "green", "missing": "red"}.get(outcome, "yellow")
        console.print(f"Known board {board.ats}:{board.token}  [{colour}]{outcome}[/{colour}]")
    if "missing" in known.values():
        console.print("[red]A known board is missing:[/red] discovery is losing boards.")
        raise typer.Exit(1)


def _board_readers() -> dict[str, BoardReader]:
    """The real board readers, sharing one polite client. A function so tests can swap it."""
    http = httpx.Client(
        headers={"User-Agent": "job-matcher/0.1 (personal job search)"}, timeout=60.0
    )
    client = BoardClient(http)
    return {ats: reader(client) for ats, reader in READERS.items()}


@app.command("fetch-boards")
def fetch_boards(
    ats: Annotated[
        list[str] | None,
        typer.Option(help="Only these systems (repeatable): greenhouse, ashby. Default: both."),
    ] = None,
    limit: Annotated[
        int | None, typer.Option(min=1, help="Stop after reading this many boards.")
    ] = None,
    refresh_days: Annotated[
        float, typer.Option(min=0, help="Read a board again once this many days have passed.")
    ] = 7.0,
    profile_path: ProfileOption = DEFAULT_PROFILE_PATH,
) -> None:
    """Read the discovered company boards and keep software jobs in the target places.
    Free, no key. Progress is saved after every board, so a stopped run resumes."""
    readers = _board_readers()
    unknown = sorted(set(ats or ()) - set(readers))
    if unknown:
        console.print(f"[red]Unknown system:[/red] {', '.join(unknown)} "
                      f"(choose from {', '.join(sorted(readers))})")
        raise typer.Exit(2)
    if ats:
        readers = {name: reader for name, reader in readers.items() if name in ats}
    profile = load_profile(profile_path)

    def show_board(name: str, status: str, detail: str) -> None:
        colour = {"ok": "green", "gone": "yellow", "error": "red"}[status]
        console.print(f"{name:<40} [{colour}]{status}[/{colour}]  {escape(detail)}")

    conn = db.connect(Settings.from_env().db_path)
    try:
        report = pipeline.fetch_boards(conn, readers, profile, refresh_days=refresh_days,
                                       limit=limit, on_board=show_board)
    except KeyboardInterrupt:
        console.print("[yellow]Interrupted.[/yellow] Finished boards are saved; "
                      "run the command again to resume.")
        raise typer.Exit(130) from None
    finally:
        conn.close()

    console.print(
        f"{report.boards} boards read ({report.gone} gone, {len(report.failed)} failed), "
        f"{report.requests} requests. {report.jobs_listed} jobs listed, "
        f"{report.jobs_kept} kept: [green]{report.new} new[/green], {report.updated} updated, "
        f"{report.unchanged} unchanged; {report.closed} stored jobs closed."
    )
    if report.stopped_reason:
        console.print(f"[yellow]Stopped early:[/yellow] {report.stopped_reason}")
    if report.new or report.updated:
        console.print("Run [bold]jm filter[/bold] and [bold]jm score[/bold] to rank them.")


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
