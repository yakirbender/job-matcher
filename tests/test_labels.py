import csv
import sqlite3

import pytest

from job_matcher import labels


def rows(spec: list[tuple[int, str, int]]) -> list[sqlite3.Row]:
    """In-memory rows shaped like db.scored_jobs(): (id, company, score)."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """CREATE TABLE t (id, title, company, location_display, work_mode, posted_at, url,
           description, score, explanation_json, employment)"""
    )
    conn.executemany(
        "INSERT INTO t VALUES (?, ?, ?, 'Orlando, FL', 'remote', '2026-11-01T00:00:00Z', "
        "'https://example.test', 'Snippet with Développeur', ?, '[\"Skills: x\", \"Level: y\"]', "
        "'full_time')",
        [(i, f"Job {i}", company, score) for i, company, score in spec],
    )
    return conn.execute("SELECT * FROM t ORDER BY id").fetchall()


def test_sample_spreads_across_scores_and_caps_each_company():
    spec = [(i, "Spammy Inc", 20 + i) for i in range(20)]  # 20 postings, one company
    spec += [(100 + i, f"Company {i}", 30 + 3 * i) for i in range(12)]
    picked = labels.sample_for_labeling(rows(spec), 10)
    assert len(picked) == 10
    assert sum(r["company"] == "Spammy Inc" for r in picked) <= 2
    scores = sorted(r["score"] for r in picked)
    assert scores[0] <= 30 and scores[-1] >= 60  # covers the low and high ends


def test_sample_is_shuffled_but_reproducible():
    data = rows([(i, f"C{i}", i * 5) for i in range(20)])
    first = [r["id"] for r in labels.sample_for_labeling(data, 8)]
    again = [r["id"] for r in labels.sample_for_labeling(data, 8)]
    assert first == again
    assert first != sorted(first, key=lambda i: data[i]["score"])  # not in score order


def test_sample_needs_at_least_two():
    with pytest.raises(ValueError):
        labels.sample_for_labeling(rows([(1, "A", 50)]), 1)


def test_export_hides_scores_and_never_overwrites_by_default(tmp_path):
    path = tmp_path / "labels.csv"
    labels.write_label_file(path, rows([(1, "A", 66), (2, "B", 30)]))
    text = path.read_text(encoding="utf-8-sig")
    header = text.splitlines()[0].split(",")
    assert header == labels.COLUMNS
    assert "66" not in text and "score" not in header
    assert "Développeur" in text
    with pytest.raises(FileExistsError):
        labels.write_label_file(path, rows([(3, "C", 10)]))
    labels.write_label_file(path, rows([(3, "C", 10)]), overwrite=True)


def write_labels(path, entries):
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=labels.COLUMNS)
        writer.writeheader()
        for job_id, label, reason, opened in entries:
            writer.writerow({"id": job_id, "label": label, "reason": reason,
                             "opened_full_posting": opened})


def test_read_labels_accepts_numbers_and_words(tmp_path):
    path = tmp_path / "labels.csv"
    write_labels(path, [(1, "2", "great fit", "yes"), (2, "Maybe", "", ""), (3, "skip", "", "x"),
                        (4, "", "", ""), (5, "yes", "", "")])
    parsed = labels.read_labels(path)
    assert [(lab.job_id, lab.value, lab.opened_full_posting) for lab in parsed.labels] == [
        (1, 2, True), (2, 1, False), (3, 0, True)
    ]
    assert parsed.unlabeled == 1
    assert parsed.invalid == ["row 6: 'yes' isn't 2/1/0 (apply/maybe/skip)"]


def test_spearman():
    assert labels.spearman([1, 2, 3], [10, 20, 30]) == pytest.approx(1.0)
    assert labels.spearman([1, 2, 3], [30, 20, 10]) == pytest.approx(-1.0)
    assert labels.spearman([2, 2, 0, 0], [80, 70, 30, 20]) == pytest.approx(0.894, abs=0.01)
    assert labels.spearman([1, 1, 1], [10, 20, 30]) is None  # all the same label


def test_evaluate_reports_agreement_and_biggest_disagreements_first():
    scored = {r["id"]: r for r in rows([(1, "A", 75), (2, "B", 55), (3, "C", 20),
                                        (4, "D", 80), (5, "E", 45)])}
    report = labels.evaluate([
        labels.Label(1, 2, "fit", False),       # strong, apply: agree
        labels.Label(2, 1, "stretch", False),   # possible, maybe: agree
        labels.Label(3, 0, "spam", False),      # weak, skip: agree
        labels.Label(4, 0, "defense", False),   # strong, but you'd skip: gap 2
        labels.Label(5, 1, "", False),          # weak, maybe: gap 1
        labels.Label(99, 2, "", False),         # neither scored nor screened
    ], scored)
    assert report.compared == 5
    assert report.missing == [99]
    assert report.band_agreement == pytest.approx(0.6)
    assert [d.job_id for d in report.disagreements] == [4, 5]
    assert report.disagreements[0].reason == "defense"
    assert report.confusion[(0, 2)] == 1
    assert report.mean_score_by_label == {0: 50.0, 1: 50.0, 2: 75.0}


def test_filter_rejections_count_as_skip():
    # The real case: after the experience fix, two jobs you labeled skip were rejected by the
    # filters. They should count as agreements, not drop out of the comparison.
    scored = {r["id"]: r for r in rows([(1, "A", 66), (2, "B", 30)])}
    rejected = {159: ("React Developer", "experience"), 158: ("Rust Developer", "seniority"),
                7: ("Test Engineer", "location")}
    report = labels.evaluate([
        labels.Label(1, 1, "", False),       # possible, maybe: agree
        labels.Label(2, 0, "", False),       # weak, skip: agree
        labels.Label(159, 0, "5+ years", False),
        labels.Label(158, 0, "too senior", False),
        labels.Label(7, 2, "great", False),  # filtered out, but you'd apply: a filter mistake
    ], scored, rejected)
    assert report.compared == 5
    assert report.filtered_out == 3
    assert report.missing == []
    assert report.band_agreement == pytest.approx(0.8)
    assert report.confusion[(0, labels.FILTERED)] == 2
    assert report.confusion[(2, labels.FILTERED)] == 1
    assert report.mean_score_by_label == {0: 30.0, 1: 66.0}  # scored jobs only

    [miss] = report.disagreements
    assert (miss.job_id, miss.title, miss.score, miss.gap) == (7, "Test Engineer", None, 2)
    assert miss.explanation == ["Rejected by filters: location"]
    # Rejected jobs rank below every scored one: skip(-1), skip(-1), skip(30), maybe(66), apply(-1)
    assert report.spearman is not None and report.spearman < 0.5
