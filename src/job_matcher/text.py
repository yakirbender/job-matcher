"""Language-neutral text normalization shared by English and French matching."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from functools import lru_cache

_NON_WORD = re.compile(r"[^\w]+")
_APOSTROPHES = str.maketrans({"’": "'", "‘": "'", "ʼ": "'"})

# French inclusive endings after a separator: "developpeur.euse", "developpeur·euse",
# "developpeur · euse", "concepteur(trice)", "developpeur/euse", "principal.e".
# Dots and slashes must touch the word, so sentence breaks in prose aren't affected.
_INCLUSIVE = re.compile(
    r"(\w)(?:\s*[·•]\s*|[./]|\s*\(\s*)(e|es|se|euse|euses|rice|trice|ice|ne|le)\)?(?!\w)"
)


def fold(text: str) -> str:
    """Case-fold, strip accents, unify apostrophes, and collapse whitespace.

    "Développeur  Sénior" -> "developpeur senior"
    """
    decomposed = unicodedata.normalize("NFKD", text.translate(_APOSTROPHES))
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return " ".join(stripped.casefold().split())


def fold_title(title: str) -> str:
    """fold() plus French inclusive endings removed: "Développeur.euse" -> "developpeur"."""
    return _INCLUSIVE.sub(r"\1", fold(title))


def fold_key(text: str) -> str:
    """Like fold(), but punctuation becomes spaces: "Miami-Dade County" -> "miami dade county"."""
    return " ".join(_NON_WORD.sub(" ", fold(text)).split())


@lru_cache(maxsize=2048)
def _phrase_pattern(phrase: str) -> re.Pattern[str]:
    return re.compile(rf"(?<!\w){re.escape(fold(phrase))}(?!\w)")


def find_any(text: str, phrases: Iterable[str]) -> str | None:
    """Return the first phrase that appears in text as whole words, ignoring case and accents."""
    folded = fold(text)
    for phrase in phrases:
        if _phrase_pattern(phrase).search(folded):
            return phrase
    return None
