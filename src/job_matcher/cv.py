"""Read a CV (PDF, Word, Markdown, or text) and extract its sections and skills."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from job_matcher.text import find_any, fold

DEFAULT_SKILLS_PATH = Path("config/skills.yaml")
CV_CANDIDATES = [Path("private") / f"cv.{ext}" for ext in ("pdf", "docx", "md", "txt")]

# Heading keyword -> section. A heading is a short line containing one of these words.
_SECTION_KEYWORDS = {
    "skills": ("skill", "competence", "technolog", "outils", "tools"),
    "education": ("education", "formation", "etudes", "academic"),
    "projects": ("project", "projet", "portfolio"),
    "experience": ("experience", "employment", "emploi", "work history"),
    "languages": ("langues", "spoken languages"),
}
_BULLET_ONLY = re.compile(r"^\s*[●•▪◦‣\-*–]\s*$")


class CvError(Exception):
    pass


Spellings = dict[str, list[str]]  # skill -> every spelling that counts


@dataclass(frozen=True)
class Vocabulary:
    jobs: Spellings  # for posting text
    cv: Spellings  # for your CV: also matches ambiguous bare names like "C"
    related: dict[str, list[str]] = field(default_factory=dict)  # job skill -> half-credit skills


def load_vocabulary(path: Path = DEFAULT_SKILLS_PATH) -> Vocabulary:
    with path.open(encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    cv_only = {str(n) for n in raw.pop("cv_only_bare_names", None) or []}
    related = {str(k): [str(s) for s in v or []]
               for k, v in (raw.pop("related_skills", None) or {}).items()}
    jobs: Spellings = {}
    cv: Spellings = {}
    for name, aliases in raw.items():
        name, others = str(name), [str(a) for a in aliases or []]
        cv[name] = [name, *others]
        jobs[name] = others if name in cv_only else [name, *others]
    unknown = sorted({s for k, v in related.items() for s in (k, *v)} - set(cv))
    if unknown:
        raise ValueError(f"related_skills names skills that aren't defined: {unknown}")
    return Vocabulary(jobs=jobs, cv=cv, related=related)


def find_skills(text: str, spellings: Spellings) -> set[str]:
    return {name for name, options in spellings.items() if options and find_any(text, options)}


def default_cv_path() -> Path | None:
    return next((p for p in CV_CANDIDATES if p.exists()), None)


def read_cv_text(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        import pdfplumber

        with pdfplumber.open(path) as pdf:
            text = "\n".join(page.extract_text() or "" for page in pdf.pages)
    elif suffix == ".docx":
        import docx

        document = docx.Document(str(path))
        parts = [p.text for p in document.paragraphs]
        for table in document.tables:
            parts.extend(cell.text for row in table.rows for cell in row.cells)
        text = "\n".join(parts)
    elif suffix in (".md", ".txt"):
        text = path.read_text(encoding="utf-8")
    else:
        raise CvError(f"unsupported CV format {suffix!r}; use .pdf, .docx, .md, or .txt")
    if not text.strip():
        raise CvError(
            f"no text found in {path}. If it's a scanned PDF (an image), export it from the "
            "original document instead; OCR isn't supported."
        )
    return _join_bullets(text)


def _join_bullets(text: str) -> str:
    """PDF extraction can put a bullet glyph on its own line; attach it to the next line."""
    out: list[str] = []
    pending = False
    for line in text.splitlines():
        if _BULLET_ONLY.match(line):
            pending = True
            continue
        out.append(f"- {line.strip()}" if pending else line.rstrip())
        pending = False
    return "\n".join(out)


def _section_for(line: str) -> str | None:
    folded = fold(line).strip(" :")
    if not folded or len(folded.split()) > 5 or folded.startswith(("-", "*")):
        return None
    for section, keywords in _SECTION_KEYWORDS.items():
        if any(k in folded for k in keywords):
            return section
    return None


@dataclass
class CvProfile:
    text: str
    sections: dict[str, str] = field(default_factory=dict)
    skills: dict[str, list[str]] = field(default_factory=dict)  # skill -> sections it's in

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(self.text.encode()).hexdigest()[:16]

    def mentions(self, phrases: list[str]) -> str | None:
        return find_any(self.text, phrases)


def parse_cv(text: str, vocabulary: Vocabulary) -> CvProfile:
    sections: dict[str, list[str]] = {"header": []}
    current = "header"
    for line in text.splitlines():
        if (section := _section_for(line)) is not None:
            current = section
            sections.setdefault(current, [])
            continue
        sections.setdefault(current, []).append(line)
    joined = {name: "\n".join(lines).strip() for name, lines in sections.items()}

    skills: dict[str, list[str]] = {}
    for name, body in joined.items():
        for skill in find_skills(body, vocabulary.cv):
            skills.setdefault(skill, []).append(name)
    return CvProfile(text=text, sections=joined, skills=dict(sorted(skills.items())))


def load_cv(path: Path, vocabulary: Vocabulary) -> CvProfile:
    return parse_cv(read_cv_text(path), vocabulary)
