"""SQLite storage: schema migrations, job upserts, run bookkeeping."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Iterator
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from job_matcher.models import Job, SearchSpec

if TYPE_CHECKING:
    from job_matcher.filters import FilterResult
    from job_matcher.scoring.base import ScoreResult
    from job_matcher.tagging import Tag

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
    """
    ALTER TABLE runs ADD COLUMN stopped_reason TEXT;

    -- Facts extracted from each posting (milestone 2). Rebuilt by every `jm filter`.
    CREATE TABLE tags (
        job_id   INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
        name     TEXT NOT NULL,
        value    TEXT NOT NULL,
        evidence TEXT,
        PRIMARY KEY (job_id, name, value)
    );
    CREATE INDEX idx_tags_name_value ON tags(name, value);

    CREATE TABLE filter_results (
        job_id       INTEGER PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,
        passed       INTEGER NOT NULL,
        reasons_json TEXT NOT NULL,
        notes_json   TEXT NOT NULL,
        profile_hash TEXT NOT NULL,
        content_hash TEXT NOT NULL,
        evaluated_at TEXT NOT NULL
    );
    """,
    """
    -- CV match scores for jobs that passed the filters (milestone 3). One row per scorer,
    -- rebuilt by every `jm score`.
    CREATE TABLE scores (
        job_id           INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
        scorer           TEXT NOT NULL,
        scorer_version   TEXT NOT NULL,
        cv_hash          TEXT NOT NULL,
        profile_hash     TEXT NOT NULL,
        score            INTEGER NOT NULL,
        components_json  TEXT NOT NULL,
        explanation_json TEXT NOT NULL,
        matched_json     TEXT NOT NULL,
        missing_json     TEXT NOT NULL,
        scored_at        TEXT NOT NULL,
        PRIMARY KEY (job_id, scorer)
    );
    CREATE INDEX idx_scores_score ON scores(scorer, score);
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
    stopped_reason: str | None = None,
) -> None:
    with conn:
        conn.execute(
            """
            UPDATE runs SET finished_at = ?, status = ?, requests = ?, jobs_new = ?,
                jobs_updated = ?, jobs_unchanged = ?, errors_json = ?, stopped_reason = ?
            WHERE id = ?
            """,
            (utcnow(), status, requests, new, updated, unchanged,
             json.dumps(errors, ensure_ascii=False), stopped_reason, run_id),
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


def row_to_job(row: sqlite3.Row) -> Job:
    return Job(
        source=row["source"],
        source_id=row["source_id"],
        title=row["title"],
        url=row["url"],
        country=row["country"],
        description=row["description"],
        company=row["company"],
        location_display=row["location_display"],
        location_area=json.loads(row["location_area_json"]),
        latitude=row["latitude"],
        longitude=row["longitude"],
        salary_min=row["salary_min"],
        salary_max=row["salary_max"],
        salary_currency=row["salary_currency"],
        salary_is_predicted=bool(row["salary_is_predicted"]),
        contract_type=row["contract_type"],
        contract_time=row["contract_time"],
        category=row["category"],
        posted_at=row["posted_at"],
        raw=json.loads(row["raw_json"]),
        metro=row["metro"],
        work_mode=row["work_mode"],
    )


def update_derived(
    conn: sqlite3.Connection, job_id: int, *, metro: str | None, work_mode: str | None
) -> None:
    conn.execute(
        "UPDATE jobs SET metro = ?, work_mode = ? WHERE id = ?", (metro, work_mode, job_id)
    )


def iter_jobs(conn: sqlite3.Connection) -> Iterator[tuple[sqlite3.Row, Job]]:
    for row in conn.execute("SELECT * FROM jobs ORDER BY id").fetchall():
        yield row, row_to_job(row)


# --- screening (tags + filters) --------------------------------------------


def save_screening(
    conn: sqlite3.Connection,
    job_id: int,
    tags: Iterable[Tag],
    result: FilterResult,
    *,
    profile_hash: str,
    content_hash: str,
    now: str | None = None,
) -> None:
    """Replace a job's tags and filter result. The caller commits."""
    conn.execute("DELETE FROM tags WHERE job_id = ?", (job_id,))
    conn.executemany(
        "INSERT OR IGNORE INTO tags (job_id, name, value, evidence) VALUES (?, ?, ?, ?)",
        [(job_id, t.name, t.value, t.evidence) for t in tags],
    )
    conn.execute(
        """
        INSERT OR REPLACE INTO filter_results
            (job_id, passed, reasons_json, notes_json, profile_hash, content_hash, evaluated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            job_id,
            int(result.passed),
            json.dumps([asdict(r) for r in result.reasons], ensure_ascii=False),
            json.dumps([asdict(r) for r in result.notes], ensure_ascii=False),
            profile_hash,
            content_hash,
            now or utcnow(),
        ),
    )


def replace_scores(
    conn: sqlite3.Connection,
    scorer: str,
    scorer_version: str,
    rows: Iterable[tuple[int, ScoreResult]],
    *,
    cv_hash: str,
    profile_hash: str,
    now: str | None = None,
) -> None:
    """Replace every stored score from this scorer. The caller commits."""
    now = now or utcnow()
    conn.execute("DELETE FROM scores WHERE scorer = ?", (scorer,))
    conn.executemany(
        """
        INSERT INTO scores (job_id, scorer, scorer_version, cv_hash, profile_hash, score,
            components_json, explanation_json, matched_json, missing_json, scored_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (job_id, scorer, scorer_version, cv_hash, profile_hash, r.score,
             json.dumps(r.components), json.dumps(r.explanation, ensure_ascii=False),
             json.dumps(r.matched), json.dumps(r.missing), now)
            for job_id, r in rows
        ],
    )


def scored_jobs(conn: sqlite3.Connection, scorer: str = "keyword") -> list[sqlite3.Row]:
    """Jobs that pass the filters and have a score from this scorer, with what labeling
    and evaluation need."""
    return conn.execute(
        """
        SELECT j.id, j.title, j.company, j.location_display, j.work_mode, j.posted_at,
               j.url, j.description, s.score, s.explanation_json,
               COALESCE((SELECT value FROM tags t
                         WHERE t.job_id = j.id AND t.name = 'employment'), 'unknown')
                   AS employment
        FROM jobs j
        JOIN filter_results f ON f.job_id = j.id AND f.passed = 1
        JOIN scores s ON s.job_id = j.id AND s.scorer = ?
        ORDER BY j.id
        """,
        (scorer,),
    ).fetchall()


def rejected_jobs(conn: sqlite3.Connection) -> dict[int, tuple[str, str]]:
    """Rejected job id -> (title, reason codes such as "experience, seniority")."""
    rows = conn.execute(
        """SELECT j.id, j.title, f.reasons_json FROM jobs j
           JOIN filter_results f ON f.job_id = j.id WHERE f.passed = 0"""
    ).fetchall()
    return {
        job_id: (title, ", ".join(sorted({r["code"] for r in json.loads(reasons)})))
        for job_id, title, reasons in rows
    }


def drop_scores_for_rejected(conn: sqlite3.Connection) -> int:
    """Remove scores of jobs that no longer pass the filters, so none shows a stale score."""
    cur = conn.execute(
        "DELETE FROM scores WHERE job_id IN (SELECT job_id FROM filter_results WHERE passed = 0)"
    )
    return cur.rowcount


def job_score(conn: sqlite3.Connection, job_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM scores WHERE job_id = ? ORDER BY scored_at DESC LIMIT 1", (job_id,)
    ).fetchone()


def passed_jobs(conn: sqlite3.Connection) -> Iterator[tuple[int, Job, list[Tag]]]:
    from job_matcher.tagging import Tag

    rows = conn.execute(
        """SELECT j.* FROM jobs j JOIN filter_results f ON f.job_id = j.id
           WHERE f.passed = 1 ORDER BY j.id"""
    ).fetchall()
    for row in rows:
        tags = [Tag(t["name"], t["value"], t["evidence"]) for t in job_tags(conn, row["id"])]
        yield row["id"], row_to_job(row), tags


def job_tags(conn: sqlite3.Connection, job_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT name, value, evidence FROM tags WHERE job_id = ? ORDER BY name", (job_id,)
    ).fetchall()


def screened_jobs(
    conn: sqlite3.Connection,
    *,
    passed: bool,
    reason: str | None = None,
    metro: str | None = None,
    employment: str | None = None,
    limit: int = 50,
) -> list[sqlite3.Row]:
    """Screened jobs, newest first, with their key tags flattened into columns."""
    sql = """
        SELECT j.id, j.title, j.company, j.metro, j.work_mode, j.posted_at,
               f.reasons_json, f.notes_json,
               (SELECT value FROM tags t WHERE t.job_id = j.id AND t.name = 'experience_years')
                   AS experience,
               (SELECT value FROM tags t WHERE t.job_id = j.id AND t.name = 'french') AS french,
               (SELECT value FROM tags t WHERE t.job_id = j.id AND t.name = 'clearance')
                   AS clearance,
               (SELECT value FROM tags t WHERE t.job_id = j.id AND t.name = 'role_family')
                   AS role_family,
               (SELECT value FROM tags t WHERE t.job_id = j.id AND t.name = 'entry_level')
                   AS entry_level,
               (SELECT group_concat(value, '+') FROM tags t
                WHERE t.job_id = j.id AND t.name = 'suspicious') AS suspicious,
               COALESCE((SELECT value FROM tags t
                         WHERE t.job_id = j.id AND t.name = 'employment'), 'unknown')
                   AS employment,
               (SELECT MAX(score) FROM scores s WHERE s.job_id = j.id) AS score
        FROM jobs j JOIN filter_results f ON f.job_id = j.id
        WHERE f.passed = ?
    """
    params: list[object] = [int(passed)]
    if employment:
        sql += " AND COALESCE((SELECT value FROM tags t WHERE t.job_id = j.id"
        sql += " AND t.name = 'employment'), 'unknown') = ?"
        params.append(employment)
    if reason:
        sql += " AND EXISTS (SELECT 1 FROM json_each(f.reasons_json) r"
        sql += " WHERE json_extract(r.value, '$.code') = ?)"
        params.append(reason)
    if metro:
        sql += " AND j.metro = ?"
        params.append(metro)
    sql += " ORDER BY score DESC NULLS LAST, j.posted_at DESC LIMIT ?"
    params.append(limit)
    return conn.execute(sql, params).fetchall()


def screening_summary(conn: sqlite3.Connection) -> dict[str, object]:
    passed = conn.execute("SELECT COUNT(*) FROM filter_results WHERE passed = 1").fetchone()[0]
    total = conn.execute("SELECT COUNT(*) FROM filter_results").fetchone()[0]
    reasons = conn.execute(
        """
        SELECT json_extract(r.value, '$.code'), COUNT(DISTINCT f.job_id)
        FROM filter_results f, json_each(f.reasons_json) r
        GROUP BY 1 ORDER BY 2 DESC
        """
    ).fetchall()
    return {"total": total, "passed": passed, "reasons": [tuple(r) for r in reasons]}


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
