"""Hand-labeled evaluation set: export jobs to label, then compare labels with the pipeline.

Labels: 2 = apply, 1 = maybe, 0 = skip. The export leaves scores out so they can't anchor the
labeler, and shuffles the order so position doesn't reveal them either.
"""

from __future__ import annotations

import csv
import json
import random
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_LABELS_PATH = Path("private/labels.csv")
LABEL_WORDS = {"2": 2, "apply": 2, "1": 1, "maybe": 1, "0": 0, "skip": 0}
LABEL_NAMES = {2: "apply", 1: "maybe", 0: "skip"}
COLUMNS = [
    "id", "title", "company", "location", "work_mode", "employment", "posted", "link",
    "snippet", "label", "reason", "opened_full_posting",
]


def score_band(score: int) -> int:
    """The scorer's band on the label scale: 70+ strong = 2, 50-69 possible = 1, else 0."""
    return 2 if score >= 70 else 1 if score >= 50 else 0


def _cap_per_company(rows: list[sqlite3.Row], per_company: int) -> list[sqlite3.Row]:
    """Keep at most `per_company` jobs per company, its lowest- and highest-scoring first,
    so one prolific poster (20 near-identical postings) doesn't fill the sample."""
    by_company: dict[str, list[sqlite3.Row]] = {}
    for r in rows:
        by_company.setdefault((r["company"] or f"#{r['id']}").casefold(), []).append(r)
    kept: list[sqlite3.Row] = []
    for jobs in by_company.values():
        jobs.sort(key=lambda r: (r["score"], r["id"]))
        ends = [jobs[0], jobs[-1]] + jobs[1:-1]
        kept.extend(list(dict.fromkeys(ends))[:per_company])
    return kept


def sample_for_labeling(
    rows: list[sqlite3.Row], count: int, *, per_company: int = 2, seed: int = 7
) -> list[sqlite3.Row]:
    """Pick jobs spread evenly across the score range, then shuffle them.

    Taking only the top would never test whether bad jobs rank low.
    """
    if count < 2:
        raise ValueError("label at least 2 jobs")
    ordered = sorted(_cap_per_company(rows, per_company), key=lambda r: (r["score"], r["id"]))
    if len(ordered) <= count:
        picked = ordered
    else:
        step = (len(ordered) - 1) / (count - 1)
        indexes = sorted({round(i * step) for i in range(count)})
        picked = [ordered[i] for i in indexes]
    picked = list(picked)
    random.Random(seed).shuffle(picked)
    return picked


def write_label_file(path: Path, rows: list[sqlite3.Row], *, overwrite: bool = False) -> None:
    """Write the file to label. Won't replace an existing file, which may hold labels,
    unless `overwrite` is set."""
    if path.exists() and not overwrite:
        raise FileExistsError(f"{path} already exists; it may contain your labels")
    path.parent.mkdir(parents=True, exist_ok=True)
    # utf-8-sig so Excel shows French accents correctly.
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS)
        writer.writeheader()
        for r in rows:
            writer.writerow({
                "id": r["id"],
                "title": r["title"],
                "company": r["company"] or "",
                "location": r["location_display"] or "",
                "work_mode": r["work_mode"] or "unknown",
                "employment": r["employment"],
                "posted": (r["posted_at"] or "")[:10],
                "link": r["url"],
                "snippet": r["description"],
                "label": "", "reason": "", "opened_full_posting": "",
            })


@dataclass(frozen=True)
class Label:
    job_id: int
    value: int  # 0, 1, 2
    reason: str
    opened_full_posting: bool


@dataclass
class LabelFile:
    labels: list[Label] = field(default_factory=list)
    unlabeled: int = 0
    invalid: list[str] = field(default_factory=list)  # "row 4: 'yes' isn't 0/1/2"


def read_labels(path: Path) -> LabelFile:
    result = LabelFile()
    with path.open(encoding="utf-8-sig", newline="") as f:
        for line_no, row in enumerate(csv.DictReader(f), start=2):
            raw = (row.get("label") or "").strip().lower()
            if not raw:
                result.unlabeled += 1
                continue
            if raw not in LABEL_WORDS:
                result.invalid.append(f"row {line_no}: {raw!r} isn't 2/1/0 (apply/maybe/skip)")
                continue
            opened = (row.get("opened_full_posting") or "").strip().lower()
            result.labels.append(Label(
                job_id=int(row["id"]),
                value=LABEL_WORDS[raw],
                reason=(row.get("reason") or "").strip(),
                opened_full_posting=opened in ("y", "yes", "1", "true", "x"),
            ))
    return result


# --- evaluation -----------------------------------------------------------------


def _ranks(values: list[float]) -> list[float]:
    """1-based ranks, ties get the average rank."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        for k in range(i, j + 1):
            ranks[order[k]] = (i + j) / 2 + 1
        i = j + 1
    return ranks


def spearman(xs: list[float], ys: list[float]) -> float | None:
    """Rank correlation, -1 to 1. None when either side has no variation."""
    if len(xs) < 2:
        return None
    rx, ry = _ranks(xs), _ranks(ys)
    mx, my = sum(rx) / len(rx), sum(ry) / len(ry)
    cov = sum((a - mx) * (b - my) for a, b in zip(rx, ry, strict=True))
    vx = sum((a - mx) ** 2 for a in rx)
    vy = sum((b - my) ** 2 for b in ry)
    if vx == 0 or vy == 0:
        return None
    return cov / (vx * vy) ** 0.5


@dataclass
class Verdict:
    job_id: int
    title: str
    label: int
    reason: str
    score: int | None  # None: rejected by the filters
    explanation: list[str]

    @property
    def band(self) -> int:
        return 0 if self.score is None else score_band(self.score)

    @property
    def gap(self) -> int:
        return abs(self.band - self.label)


FILTERED = "filtered"  # confusion-grid column for jobs the filters rejected


@dataclass
class EvalReport:
    compared: int = 0
    filtered_out: int = 0  # labeled jobs the filters rejected; counted as the scorer's "skip"
    missing: list[int] = field(default_factory=list)  # labeled, but neither scored nor screened
    band_agreement: float | None = None  # share where the pipeline's verdict equals the label
    spearman: float | None = None
    mean_score_by_label: dict[int, float] = field(default_factory=dict)  # scored jobs only
    confusion: Counter[tuple[int, int | str]] = field(default_factory=Counter)  # (label, column)
    disagreements: list[Verdict] = field(default_factory=list)  # biggest gap first


def evaluate(
    labels: list[Label],
    scored: dict[int, sqlite3.Row],
    rejected: dict[int, tuple[str, str]] | None = None,
) -> EvalReport:
    """Compare labels with the whole pipeline: the score for jobs that pass the filters,
    and "skip" for jobs the filters reject (`rejected`: job id -> (title, reason codes)).

    Leaving rejected jobs out would make correct rejections vanish from the comparison.
    """
    rejected = rejected or {}
    report = EvalReport()
    verdicts: list[Verdict] = []  # one per compared job
    for label in labels:
        if (row := scored.get(label.job_id)) is not None:
            verdicts.append(Verdict(
                job_id=label.job_id, title=row["title"], label=label.value, reason=label.reason,
                score=row["score"], explanation=json.loads(row["explanation_json"]),
            ))
        elif label.job_id in rejected:
            title, codes = rejected[label.job_id]
            verdicts.append(Verdict(
                job_id=label.job_id, title=title, label=label.value, reason=label.reason,
                score=None, explanation=[f"Rejected by filters: {codes}"],
            ))
            report.filtered_out += 1
        else:
            report.missing.append(label.job_id)
    report.compared = len(verdicts)
    if not verdicts:
        return report

    for v in verdicts:
        report.confusion[(v.label, FILTERED if v.score is None else v.band)] += 1
        if v.band != v.label:
            report.disagreements.append(v)
    report.band_agreement = sum(v.band == v.label for v in verdicts) / len(verdicts)
    # A rejected job ranks below every scored one.
    report.spearman = spearman(
        [v.label for v in verdicts], [-1 if v.score is None else v.score for v in verdicts]
    )
    by_label: dict[int, list[int]] = {}
    for v in verdicts:
        if v.score is not None:
            by_label.setdefault(v.label, []).append(v.score)
    report.mean_score_by_label = {k: sum(s) / len(s) for k, s in sorted(by_label.items())}
    report.disagreements.sort(key=lambda d: (-d.gap, -abs((d.score or 0) - 50)))
    return report
