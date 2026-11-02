"""SQLite storage: schema migrations, job upserts, run bookkeeping."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from job_matcher.models import Job, SearchSpec

# Each entry upgrades the schema by one version (tracked in PRAGMA user_version).
# Never edit a shipped migration; append a new one.
MIGRATIONS: list[str] = [
    """
    CREATE TABLE runs (
        id             INTEGER PRIMARY KEY,
        source         TEXT NOT NULL,
        started_at     TEXT NOT NULL,
        finished_at    TEXT,
        status         TEXT NOT NULL DEFAULT 'running',
        requests       INTEGER NOT NULL DEFAULT 0,
        jobs_new       INTEGER NOT NULL DEFAULT 0,
        jobs_updated   INTEGER NOT NULL DEFAULT 0,
        jobs_unchanged INTEGER NOT NULL DEFAULT 0,
        errors_json    TEXT NOT NULL DEFAULT '[]'
    );

    CREATE TABLE jobs (
        id                  INTEGER PRIMARY KEY,
        source              TEXT NOT NULL,
        source_id           TEXT NOT NULL,
        title               TEXT NOT NULL,
        company             TEXT,
        description         TEXT NOT NULL,
        url                 TEXT NOT NULL,
        country             TEXT NOT NULL,
        location_display    TEXT,
        location_area_json  TEXT NOT NULL DEFAULT '[]',
        latitude            REAL,
        longitude           REAL,
        metro               TEXT,
        work_mode           TEXT,
        salary_min          REAL,
        salary_max          REAL,
        salary_currency     TEXT,
        salary_is_predicted INTEGER NOT NULL DEFAULT 0,
        contract_type       TEXT,
        contract_time       TEXT,
        category            TEXT,
        posted_at           TEXT,
        content_hash        TEXT NOT NULL,
        dedupe_key          TEXT NOT NULL,
        duplicate_of        INTEGER REFERENCES jobs(id),
        raw_json            TEXT NOT NULL,
        first_seen          TEXT NOT NULL,
        last_seen           TEXT NOT NULL,
        updated_at          TEXT NOT NULL,
        UNIQUE (source, source_id)
    );
    CREATE INDEX idx_jobs_dedupe_key ON jobs(dedupe_key);
    CREATE INDEX idx_jobs_metro ON jobs(metro);

    -- Which searches found each job; the same posting often matches several terms.
    CREATE TABLE job_hits (
        job_id     INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
        run_id     INTEGER NOT NULL REFERENCES runs(id),
        region     TEXT NOT NULL,
        term_group TEXT NOT NULL,
        term       TEXT NOT NULL,
        seen_at    TEXT NOT NULL,
        PRIMARY KEY (job_id, run_id, region, term)
    );
    CREATE INDEX idx_job_hits_region ON job_hits(region);
    """,
]

# Columns written from a Job on every insert or content update.
_JOB_COLUMNS = (
    "title", "company", "description", "url", "country", "location_display",
    "location_area_json", "latitude", "longitude", "metro", "work_mode", "salary_min",
    "salary_max", "salary_currency", "salary_is_predicted", "contract_type", "contract_time",
    "category", "posted_at", "content_hash", "dedupe_key", "raw_json",
)


def utcnow() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def connect(path: Path | str) -> sqlite3.Connection:
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if str(path) != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
    migrate(conn)
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    for number, script in enumerate(MIGRATIONS[version:], start=version + 1):
        with conn:
            conn.executescript(script)
            conn.execute(f"PRAGMA user_version = {number}")


# --- runs -----------------------------------------------------------------


def start_run(conn: sqlite3.Connection, source: str) -> int:
    with conn:
        cur = conn.execute(
            "INSERT INTO runs (source, started_at) VALUES (?, ?)", (source, utcnow())
        )
    return cur.lastrowid


def finish_run(
    conn: sqlite3.Connection,
    run_id: int,
    *,
    status: str,
    requests: int,
    new: int,
    updated: int,
    unchanged: int,
    errors: list[str],
) -> None:
    with conn:
        conn.execute(
            """
            UPDATE runs SET finished_at = ?, status = ?, requests = ?, jobs_new = ?,
                jobs_updated = ?, jobs_unchanged = ?, errors_json = ?
            WHERE id = ?
            """,
            (utcnow(), status, requests, new, updated, unchanged,
             json.dumps(errors, ensure_ascii=False), run_id),
        )


def requests_in_last(conn: sqlite3.Connection, source: str, *, days: int) -> int:
    """API requests used in the last `days` days (rolling), for staying under a quota."""
    since = (datetime.now(UTC) - timedelta(days=days)).isoformat(timespec="seconds")
    row = conn.execute(
        "SELECT COALESCE(SUM(requests), 0) FROM runs WHERE source = ? AND started_at >= ?",
        (source, since),
    ).fetchone()
    return row[0]


# --- jobs -----------------------------------------------------------------


@dataclass(frozen=True)
class UpsertResult:
    job_id: int
    status: Literal["new", "updated", "unchanged"]
    duplicate_of: int | None = None


def _job_row(job: Job) -> dict[str, object]:
    return {
        "title": job.title,
        "company": job.company,
        "description": job.description,
        "url": job.url,
        "country": job.country,
        "location_display": job.location_display,
        "location_area_json": json.dumps(job.location_area, ensure_ascii=False),
        "latitude": job.latitude,
        "longitude": job.longitude,
        "metro": job.metro,
        "work_mode": job.work_mode,
        "salary_min": job.salary_min,
        "salary_max": job.salary_max,
        "salary_currency": job.salary_currency,
        "salary_is_predicted": int(job.salary_is_predicted),
        "contract_type": job.contract_type,
        "contract_time": job.contract_time,
        "category": job.category,
        "posted_at": job.posted_at,
        "content_hash": job.content_hash(),
        "dedupe_key": job.dedupe_key(),
        "raw_json": json.dumps(job.raw, ensure_ascii=False, sort_keys=True),
    }


def upsert_job(conn: sqlite3.Connection, job: Job, now: str | None = None) -> UpsertResult:
    """Insert or refresh a job. The caller commits."""
    now = now or utcnow()
    row = _job_row(job)
    existing = conn.execute(
        "SELECT id, content_hash, duplicate_of FROM jobs WHERE source = ? AND source_id = ?",
        (job.source, job.source_id),
    ).fetchone()

    if existing is None:
        # Without a company the key is just title + city, which would lump unrelated jobs together.
        dup = conn.execute(
            "SELECT COALESCE(duplicate_of, id) FROM jobs WHERE dedupe_key = ? ORDER BY id LIMIT 1",
            (row["dedupe_key"],),
        ).fetchone() if job.company else None
        duplicate_of = dup[0] if dup else None
        columns = ("source", "source_id", *_JOB_COLUMNS,
                   "duplicate_of", "first_seen", "last_seen", "updated_at")
        values = {**row, "source": job.source, "source_id": job.source_id,
                  "duplicate_of": duplicate_of, "first_seen": now, "last_seen": now,
                  "updated_at": now}
        cur = conn.execute(
            f"INSERT INTO jobs ({', '.join(columns)}) "
            f"VALUES ({', '.join(':' + c for c in columns)})",
            values,
        )
        return UpsertResult(cur.lastrowid, "new", duplicate_of)

    job_id = existing["id"]
    if existing["content_hash"] == row["content_hash"]:
        # Derived fields are refreshed too, so improved inference applies to old rows.
        conn.execute(
            "UPDATE jobs SET last_seen = ?, metro = ?, work_mode = ? WHERE id = ?",
            (now, job.metro, job.work_mode, job_id),
        )
        return UpsertResult(job_id, "unchanged", existing["duplicate_of"])

    assignments = ", ".join(f"{c} = :{c}" for c in _JOB_COLUMNS)
    conn.execute(
        f"UPDATE jobs SET {assignments}, last_seen = :now, updated_at = :now WHERE id = :id",
        {**row, "now": now, "id": job_id},
    )
    return UpsertResult(job_id, "updated", existing["duplicate_of"])


def record_hit(
    conn: sqlite3.Connection, job_id: int, run_id: int, spec: SearchSpec, now: str | None = None
) -> None:
    conn.execute(
        """
        INSERT OR IGNORE INTO job_hits (job_id, run_id, region, term_group, term, seen_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (job_id, run_id, spec.region, spec.term_group, spec.term, now or utcnow()),
    )


# --- reporting ------------------------------------------------------------


def stats(conn: sqlite3.Connection) -> dict[str, object]:
    def rows(sql: str) -> list[tuple]:
        return [tuple(r) for r in conn.execute(sql).fetchall()]

    return {
        "total": conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0],
        "likely_duplicates": conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE duplicate_of IS NOT NULL"
        ).fetchone()[0],
        "by_metro": rows(
            "SELECT COALESCE(metro, '(none)'), COUNT(*) FROM jobs GROUP BY 1 ORDER BY 2 DESC"
        ),
        "by_work_mode": rows(
            "SELECT COALESCE(work_mode, '(unknown)'), COUNT(*) FROM jobs GROUP BY 1 ORDER BY 2 DESC"
        ),
        "by_region": rows(
            "SELECT region, COUNT(DISTINCT job_id) FROM job_hits GROUP BY 1 ORDER BY 2 DESC"
        ),
        "recent_runs": rows(
            """
            SELECT id, started_at, status, requests, jobs_new, jobs_updated, jobs_unchanged
            FROM runs ORDER BY id DESC LIMIT 5
            """
        ),
    }
