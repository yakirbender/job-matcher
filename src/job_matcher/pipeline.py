"""Pipeline stages: fetch postings, screen and score them, and discover job boards."""

from __future__ import annotations

import sqlite3
import time
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from job_matcher import db
from job_matcher.discovery import (
    BOARD_HOSTS,
    KNOWN_BOARDS,
    BoardRef,
    CdxClient,
    CdxError,
    Crawl,
    pending_patterns,
    read_until_stable,
)
from job_matcher.filters import evaluate
from job_matcher.location import infer_work_mode, metro_for
from job_matcher.models import Job, SearchSpec
from job_matcher.profile import Profile
from job_matcher.scoring.base import Scorer
from job_matcher.sources.base import JobSource, SourceAuthError, SourceError, SourceQuotaError
from job_matcher.sources.boards import BoardError, BoardGone, stated_work_mode
from job_matcher.suspicion import mass_template_tags, scam_phrase_tag
from job_matcher.tagging import role_family, tag_job


@dataclass
class FetchReport:
    run_id: int
    requests: int = 0
    new: int = 0
    updated: int = 0
    unchanged: int = 0
    likely_duplicates: int = 0
    searches_run: int = 0
    searches_total: int = 0
    stopped_reason: str | None = None
    errors: list[str] = field(default_factory=list)
    new_by_region: dict[str, int] = field(default_factory=lambda: defaultdict(int))


def enrich(job: Job) -> Job:
    """Fill in fields derived the same way for every source."""
    job.metro = metro_for(job.country, job.location_area, job.location_display)
    job.work_mode = stated_work_mode(job) or infer_work_mode(job.title, job.description)
    return job


def fetch(
    conn: sqlite3.Connection,
    source: JobSource,
    specs: list[SearchSpec],
    *,
    daily_limit: int,
    monthly_limit: int | None = None,
    run_limit: int | None = None,
    early_stop: bool = True,
) -> FetchReport:
    """Run each search, upsert every job, and record which search found it.

    Results are sorted newest first, so with `early_stop` a page that brings nothing
    new ends that search: the older pages behind it were stored on earlier runs.
    The run stops at whichever request budget (day, month, this run) is tightest.
    """
    budgets = {"daily limit": daily_limit - db.requests_in_last(conn, source.name, days=1)}
    if monthly_limit is not None:
        budgets["monthly limit"] = monthly_limit - db.requests_in_last(conn, source.name, days=30)
    if run_limit is not None:
        budgets["--limit for this run"] = run_limit
    binding, remaining = min(budgets.items(), key=lambda item: item[1])

    run_id = db.start_run(conn, source.name)
    report = FetchReport(run_id=run_id, searches_total=len(specs))
    source.max_requests = source.requests_made + max(remaining, 0)
    status = "ok"

    try:
        for spec in specs:
            try:
                for page in source.search(spec):
                    now = db.utcnow()
                    found_new = False
                    for job in page:
                        result = db.upsert_job(conn, enrich(job), now)
                        db.record_hit(conn, result.job_id, run_id, spec, now)
                        setattr(report, result.status, getattr(report, result.status) + 1)
                        if result.status == "new":
                            found_new = True
                            report.new_by_region[spec.region] += 1
                            if result.duplicate_of is not None:
                                report.likely_duplicates += 1
                    conn.commit()
                    if early_stop and not found_new:
                        break
            except SourceQuotaError:
                report.stopped_reason = f"reached the {binding}"
                status = "partial"
                break
            except SourceAuthError:
                status = "failed"
                raise
            except SourceError as exc:
                report.errors.append(f"{spec.region} / {spec.term}: {exc}")
                status = "partial"
            report.searches_run += 1
    except BaseException:
        conn.rollback()
        if status == "ok":
            status = "failed"
        raise
    finally:
        report.requests = source.requests_made
        db.finish_run(
            conn,
            run_id,
            status=status,
            requests=report.requests,
            new=report.new,
            updated=report.updated,
            unchanged=report.unchanged,
            errors=report.errors,
            stopped_reason=report.stopped_reason,
        )
    return report


@dataclass
class DiscoveryReport:
    finished: int = 0  # queries completed in this run
    new_boards: int = 0
    failed: list[str] = field(default_factory=list)  # still failing when the run ended
    stopped_reason: str | None = None


# Called with the crawl, the pattern, its status, and a detail (boards found or the error).
QueryCallback = Callable[[str, str, str, str], None]


def discover_crawl(
    conn: sqlite3.Connection,
    client: CdxClient,
    crawl: str,
    cdx_api: str,
    hosts: list[str],
    *,
    max_depth: int = 2,
    max_reads: int = 4,
    max_attempts: int = 3,
    stop_after_failures: int = 5,
    retry_pause: float = 300.0,
    limit: int | None = None,
    sleep: Callable[[float], None] = time.sleep,
    on_query: QueryCallback | None = None,
) -> DiscoveryReport:
    """Read one crawl's index for job boards, saving each query as it finishes.

    Finished queries are skipped, so rerunning resumes. A failed query is recorded and
    retried after the others, up to `max_attempts` times per run. Too many failures in a
    row stops the run, since pressing on risks a block from the index. `on_query` hears
    about each query as it ends.
    """
    report = DiscoveryReport()
    boards_before = db.board_count(conn)
    attempts: Counter[str] = Counter()
    failures_in_a_row = 0

    def to_do() -> list[str]:
        rows = db.discovery_queries(conn, crawl)
        finished = {p: r["status"] == "confirmed" for p, r in rows.items()
                    if r["status"] != "failed"}
        return [pattern for host in hosts
                for pattern in pending_patterns(host, finished, max_depth)
                if attempts[pattern] < max_attempts]

    while (batch := to_do()) and report.stopped_reason is None:
        any_failed = False
        for pattern in batch:
            if limit is not None and report.finished >= limit:
                report.stopped_reason = "reached the query limit"
                break
            attempts[pattern] += 1
            try:
                result = read_until_stable(client, cdx_api, pattern, max_reads=max_reads)
            except CdxError as exc:
                db.save_query_failure(conn, crawl, pattern, str(exc))
                if on_query:
                    on_query(crawl, pattern, "failed", str(exc))
                any_failed = True
                failures_in_a_row += 1
                if failures_in_a_row >= stop_after_failures:
                    report.stopped_reason = f"{failures_in_a_row} failed queries in a row"
                    break
                continue
            failures_in_a_row = 0
            db.save_query_result(conn, crawl, result)
            report.finished += 1
            if on_query:
                status = "confirmed" if result.confirmed else "unconfirmed"
                on_query(crawl, pattern, status, f"{len(result.boards)} boards")
        if any_failed and report.stopped_reason is None and to_do():
            sleep(retry_pause)

    report.new_boards = db.board_count(conn) - boards_before
    report.failed = sorted(p for p, r in db.discovery_queries(conn, crawl).items()
                           if r["status"] == "failed")
    return report


def discover(
    conn: sqlite3.Connection,
    client: CdxClient,
    crawls: list[Crawl],
    hosts: list[str],
    *,
    limit: int | None = None,
    **options,
) -> list[tuple[Crawl, DiscoveryReport]]:
    """Read each crawl in turn. `limit` counts queries across all of them, and a crawl
    that stops early ends the run: the query limit is used up, or the index is failing.
    Other options go to discover_crawl."""
    results: list[tuple[Crawl, DiscoveryReport]] = []
    for crawl in crawls:
        remaining = None if limit is None else limit - sum(r.finished for _, r in results)
        report = discover_crawl(conn, client, crawl.id, crawl.cdx_api, hosts,
                                limit=remaining, **options)
        results.append((crawl, report))
        if report.stopped_reason:
            break
    return results


def check_known_boards(
    conn: sqlite3.Connection, known: tuple[BoardRef, ...] | list[BoardRef] = KNOWN_BOARDS
) -> dict[BoardRef, str]:
    """For each known board: "found", "missing", or "not checked yet".

    A board is missing only when, on every host of its system, a confirmed query (in any
    crawl) covers its name and still didn't find it. Until then it's not checked yet.
    """
    confirmed = db.confirmed_prefixes(conn)
    saved = db.saved_boards(conn)
    result = {}
    for board in known:
        if board in saved:
            result[board] = "found"
            continue
        hosts = [host for host, ats in BOARD_HOSTS.items() if ats == board.ats]
        covered = all(
            any(board.token.startswith(prefix) for prefix in confirmed.get(host, ()))
            for host in hosts
        )
        result[board] = "missing" if covered else "not checked yet"
    return result


def board_job_wanted(job: Job, profile: Profile) -> bool:
    """Whether a job from a company board is worth storing: a software-related title (any
    role family) in a target metro, or remote in the US, Canada, or an unstated country.

    Boards list every job a company has, worldwide, so this runs before anything is saved
    (and, on Greenhouse, before the description is downloaded). The full filters still run
    later, in screen().
    """
    if role_family(job.title, profile.role_families) is None:
        return False
    enrich(job)
    if job.metro in profile.locations.metros:
        return True
    return job.work_mode == "remote" and job.country in ("us", "ca", "")


class BoardReader(Protocol):
    ats: str
    client: Any  # has requests_made

    def list_jobs(self, token: str) -> list[Job]: ...
    def complete(self, job: Job) -> Job: ...


@dataclass
class BoardFetchReport:
    boards: int = 0  # boards read in this run
    gone: int = 0
    failed: list[str] = field(default_factory=list)  # "ats:token"
    jobs_listed: int = 0
    jobs_kept: int = 0
    new: int = 0
    updated: int = 0
    unchanged: int = 0
    requests: int = 0
    stopped_reason: str | None = None


# Called after each board with "ats:token", its status, and a detail.
BoardCallback = Callable[[str, str, str], None]


def fetch_boards(
    conn: sqlite3.Connection,
    readers: dict[str, BoardReader],
    profile: Profile,
    *,
    refresh_days: float = 7,
    limit: int | None = None,
    stop_after_failures: int = 5,
    on_board: BoardCallback | None = None,
) -> BoardFetchReport:
    """Read each discovered board due a fetch, and save the jobs worth keeping.

    Boards never fetched come first, then the stalest. Progress is saved per board, so a
    stopped run resumes. A board that's gone (404) is never tried again; one that failed
    is tried on the next run.
    """
    report = BoardFetchReport()
    cutoff = (datetime.now(UTC) - timedelta(days=refresh_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    due = db.boards_to_fetch(conn, sorted(readers), cutoff)
    # Readers may share one client: count each client once.
    clients = list({id(reader.client): reader.client for reader in readers.values()}.values())
    requests_before = [client.requests_made for client in clients]
    run_id = db.start_run(conn, "boards")
    failures_in_a_row = 0
    status = "ok"
    errors: list[str] = []

    try:
        for ats, token in due:
            if limit is not None and report.boards >= limit:
                report.stopped_reason = "reached the board limit"
                break
            reader = readers[ats]
            name = f"{ats}:{token}"
            report.boards += 1
            try:
                listed = reader.list_jobs(token)
                kept = []
                for job in (j for j in listed if board_job_wanted(j, profile)):
                    try:
                        kept.append(enrich(reader.complete(job)))
                    except BoardGone:
                        continue  # closed between the listing and now
            except BoardGone:
                db.save_board_fetch(conn, ats, token, "gone")
                conn.commit()
                report.gone += 1
                if on_board:
                    on_board(name, "gone", "no such board")
                continue
            except BoardError as exc:
                db.save_board_fetch(conn, ats, token, "error", error=str(exc))
                conn.commit()
                report.failed.append(name)
                errors.append(f"{name}: {exc}")
                status = "partial"
                if on_board:
                    on_board(name, "error", str(exc))
                failures_in_a_row += 1
                if failures_in_a_row >= stop_after_failures:
                    report.stopped_reason = f"{failures_in_a_row} failed boards in a row"
                    break
                continue

            failures_in_a_row = 0
            now = db.utcnow()
            new = 0
            for job in kept:
                result = db.upsert_job(conn, job, now)
                setattr(report, result.status, getattr(report, result.status) + 1)
                new += result.status == "new"
            db.save_board_fetch(conn, ats, token, "ok", listed=len(listed), kept=len(kept),
                                now=now)
            conn.commit()
            report.jobs_listed += len(listed)
            report.jobs_kept += len(kept)
            if on_board:
                on_board(name, "ok", f"{len(listed)} listed, {len(kept)} kept, {new} new")
    except BaseException:
        conn.rollback()
        status = "failed"
        raise
    finally:
        report.requests = sum(client.requests_made - before
                              for client, before in zip(clients, requests_before, strict=True))
        db.finish_run(conn, run_id, status=status, requests=report.requests, new=report.new,
                      updated=report.updated, unchanged=report.unchanged, errors=errors,
                      stopped_reason=report.stopped_reason)
    return report


@dataclass
class ScoreReport:
    scored: int = 0
    bands: Counter[str] = field(default_factory=Counter)  # strong / possible / weak


def band(score: int) -> str:
    return "strong" if score >= 70 else "possible" if score >= 50 else "weak"


def score(
    conn: sqlite3.Connection, scorer: Scorer, *, cv_hash: str, profile: Profile
) -> ScoreReport:
    """Score every job that passed the filters. Replaces this scorer's earlier scores, so
    jobs that no longer pass lose theirs."""
    report = ScoreReport()
    results = [(job_id, scorer.score(job, tags)) for job_id, job, tags in db.passed_jobs(conn)]
    try:
        db.replace_scores(
            conn, scorer.name, scorer.version, results,
            cv_hash=cv_hash, profile_hash=profile.fingerprint(),
        )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    report.scored = len(results)
    report.bands.update(band(r.score) for _, r in results)
    return report


@dataclass
class ScreenReport:
    total: int = 0
    passed: int = 0
    reasons: Counter[str] = field(default_factory=Counter)


def screen(conn: sqlite3.Connection, profile: Profile) -> ScreenReport:
    """Tag and filter every stored job against the profile.

    It's all local text matching, so every job is re-screened each time; a profile
    change applies to old jobs too.
    """
    report = ScreenReport()
    profile_hash = profile.fingerprint()
    now = db.utcnow()
    jobs = list(db.iter_jobs(conn))
    # Template detection compares postings with each other, so it runs over all of them.
    templates = mass_template_tags(
        ((row["id"], job) for row, job in jobs), profile.suspicious.min_template_titles
    )
    try:
        for row, job in jobs:
            enrich(job)  # so improved metro/work-mode rules reach jobs fetched earlier
            if (job.metro, job.work_mode) != (row["metro"], row["work_mode"]):
                db.update_derived(conn, row["id"], metro=job.metro, work_mode=job.work_mode)
            tags = tag_job(job, profile)
            if template := templates.get(row["id"]):
                tags.append(template)
            if scam := scam_phrase_tag(f"{job.title}\n{job.description}"):
                tags.append(scam)
            result = evaluate(job, tags, profile, duplicate_of=row["duplicate_of"])
            db.save_screening(
                conn, row["id"], tags, result,
                profile_hash=profile_hash, content_hash=row["content_hash"], now=now,
            )
            report.total += 1
            report.passed += result.passed
            report.reasons.update({r.code for r in result.reasons})
        db.drop_scores_for_rejected(conn)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return report
