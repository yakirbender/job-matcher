"""Pipeline stages: fetch postings from a source, then screen them against the profile."""

from __future__ import annotations

import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from job_matcher import db
from job_matcher.filters import evaluate
from job_matcher.location import infer_work_mode, metro_for
from job_matcher.models import Job, SearchSpec
from job_matcher.profile import Profile
from job_matcher.sources.base import JobSource, SourceAuthError, SourceError, SourceQuotaError
from job_matcher.suspicion import mass_template_tags, scam_phrase_tag
from job_matcher.tagging import tag_job


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
    job.work_mode = infer_work_mode(job.title, job.description)
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
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return report
