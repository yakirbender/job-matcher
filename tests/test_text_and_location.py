import pytest

from job_matcher.location import infer_work_mode, metro_for
from job_matcher.text import find_any, fold, fold_key


def test_fold_strips_accents_case_and_spacing():
    assert fold("  Développeur   SÉNIOR ") == "developpeur senior"
    assert fold("100 % télétravail") == "100 % teletravail"


def test_fold_key_turns_punctuation_into_spaces():
    assert fold_key("Miami-Dade County") == "miami dade county"
    assert fold_key("St. Petersburg") == "st petersburg"


def test_find_any_matches_whole_words_regardless_of_accents():
    assert find_any("Poste en TELETRAVAIL", ["télétravail"]) == "télétravail"
    assert find_any("We are remote-first", ["remote"]) == "remote"
    assert find_any("remoteness is not a word we use", ["remote"]) is None


@pytest.mark.parametrize(
    ("country", "area", "display", "expected"),
    [
        ("us", ["US", "Florida", "Orange County", "Orlando"], None, "orlando"),
        ("us", ["US", "Florida", "Seminole County"], None, "orlando"),
        ("us", ["US", "Florida", "Pinellas County", "St. Petersburg"], None, "tampa"),
        ("us", ["US", "Florida", "Miami-Dade County", "Doral"], None, "miami"),
        ("us", ["US", "Florida"], "Boca Raton, FL", "miami"),
        ("us", ["US", "Florida", "Alachua County", "Gainesville"], None, "florida_other"),
        ("us", ["US", "California", "Orange County", "Irvine"], None, None),
        ("ca", ["Canada", "Québec", "Montréal"], None, "montreal"),
        ("ca", ["Canada", "Québec", "Montréal-Nord"], None, "montreal"),
        ("ca", ["Canada", "Québec", "Laval"], None, "montreal"),
        ("ca", ["Canada", "Ontario", "Toronto"], None, None),
    ],
)
def test_metro_for(country, area, display, expected):
    assert metro_for(country, area, display) == expected


@pytest.mark.parametrize(
    ("title", "description", "expected"),
    [
        ("Developer (Remote)", "", "remote"),
        ("Développeur", "Poste en télétravail à 100 %", "remote"),
        ("Developer", "Hybrid schedule, 2 days remote", "hybrid"),
        ("Développeur", "Mode hybride", "hybrid"),
        ("Developer", "This is not remote. On-site only.", "onsite"),
        ("Développeur", "Pas de télétravail possible", "onsite"),
        ("Embedded Security Software Engineer I (Onsite)", "", "onsite"),
        ("Développeur", "Travail sur place à Laval", "onsite"),
        ("Developer", "Hybrid: 1–2 days onsite per week", "hybrid"),
        ("Developer", "Remote with occasional on-site visits", "remote"),
        ("Developer", "Build APIs in Python.", None),
    ],
)
def test_infer_work_mode(title, description, expected):
    assert infer_work_mode(title, description) == expected
