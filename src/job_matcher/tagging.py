"""Taggers: facts extracted from a posting's title and snippet, in English and French.

Tags record what a posting says, with the matching text as evidence. Filters decide
what to reject from them, and ranking (milestone 3) weighs them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from job_matcher.models import Job
from job_matcher.profile import Profile
from job_matcher.text import find_any, fold, fold_title


@dataclass(frozen=True)
class Tag:
    name: str
    value: str
    evidence: str | None = None


def _evidence(text: str, match: re.Match[str], pad: int = 40) -> str:
    start, end = max(match.start() - pad, 0), min(match.end() + pad, len(text))
    return ("…" if start else "") + text[start:end].strip() + ("…" if end < len(text) else "")


# --- experience -------------------------------------------------------------

_NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
    "nine": 9, "ten": 10, "un": 1, "une": 1, "deux": 2, "trois": 3, "quatre": 4, "cinq": 5,
    "sept": 7, "huit": 8, "neuf": 9, "dix": 10,
}
_NUM = r"(\d{1,2}|" + "|".join(_NUMBER_WORDS) + r")"
_RANGE = rf"{_NUM}\s*\+?\s*(?:(?:-|–|to|or|a)\s*{_NUM}\s*)?\+?"
_UNIT = r"(?:years?|yrs?|ans?|annees?)"
# What follows "N years" when it isn't experience: age, company history, benefits, time.
_NOT_EXPERIENCE = (r"(?!\s+(?:of\s+)?(?:age|old|ago|later|from now|hour|legacy|history"
                   r"|heritage|of service|in business))")
_EXPERIENCE_PATTERNS = [
    # "3-5 years of Java experience", "5+ yrs experience", "3 ans d'experience"
    re.compile(rf"(?<!\w){_RANGE}\s*{_UNIT}(?!\w)[^.;]{{0,40}}?(?<!\w)experiences?(?!\w)"),
    # "7 to 9 years of professional ...", where the snippet may cut off "experience"
    re.compile(
        rf"(?<!\w){_RANGE}\s*{_UNIT}\s+(?:of\s+)?"
        r"(?:professional|relevant|related|industry|work|hands-on|progressive)(?!\w)"
    ),
    # "Experience: 6 years", "Experience Required: 5 years", "experience of 3+ years",
    # "Minimum experience: 4 years", "expérience requise : 3 ans"
    re.compile(
        r"(?<!\w)(?:minimum\s+)?experiences?\s*"
        r"(?:required|requirements?|needed|requise|exigee|minimale|minimum)?\s*"
        r"(?::|-|of|de|minimum de|minimum of|of at least)?"
        rf"\s*(?:at least\s*|minimum\s*)?{_RANGE}\s*{_UNIT}(?!\w)"
    ),
    # "A minimum of 2 years building ...", "at least 3 years in ..."
    re.compile(rf"(?<!\w)(?:minimum of|at least|minimum)\s+{_RANGE}\s*{_UNIT}(?!\w)"
               + _NOT_EXPERIENCE),
    # "5+ years in SRE", "3-5 years writing tested code", "8+ years building systems": a
    # plus sign or a range marks a requirement even without the word "experience". Not
    # after "for", "over", "next", ... ("named a best workplace for 7+ years"), not pay
    # ("$70-85 an hour"), and not "N years to exercise your options" (no plus or range).
    re.compile(
        r"(?<!\bfor )(?<!\bover )(?<!\bpast )(?<!\blast )(?<!\bnext )(?<!\bthan )"
        r"(?<!\bnearly )(?<!\balmost )(?<!\babout )(?<!\bwithin )(?<![\w$])"
        rf"{_NUM}\s*(?:\+|(?:-|–|to)\s*{_NUM}\s*\+?)\s*{_UNIT}(?!\w)" + _NOT_EXPERIENCE
    ),
]


def _to_int(value: str) -> int:
    return int(value) if value.isdigit() else _NUMBER_WORDS[value]


def experience_years(text: str) -> tuple[int, str] | None:
    """The largest minimum number of years the text asks for, with evidence.

    "3-5 years" counts as 3. With several mentions ("5+ years Java, 2 years AWS") the
    largest wins, since a posting is only as junior as its strictest requirement.
    """
    folded = fold(text)
    best: tuple[int, str] | None = None
    for pattern in _EXPERIENCE_PATTERNS:
        for m in pattern.finditer(folded):
            years = _to_int(m.group(1))
            if best is None or years > best[0]:
                best = (years, _evidence(folded, m, pad=20))
    return best


# --- stated level -------------------------------------------------------------

# "Experience Level: Middle / Senior", "Career Level Mid Level", "Seniority level: Entry",
# "Niveau d'expérience : Junior". Captures what follows, up to punctuation or 4 words.
# Bare "niveau" needs a colon: "anglais niveau avancé" is a language requirement.
_STATED_LEVEL = re.compile(
    r"(?<!\w)(?:(?:(?:experience|career|seniority|job|position)\s+level"
    r"|niveau\s+(?:d'experience|de carriere|hierarchique))\s*:?"
    r"|niveau\s*:)"
    r"\s*((?:[a-z-]+(?:\s*/\s*|\s+)?){1,4})"
)
_LEVEL_WORDS = [  # checked lowest first: "Middle / Senior" accepts mid-level people
    ("entry", ("entry", "junior", "jr", "associate", "graduate", "debutant", "debutante")),
    ("mid", ("mid", "middle", "intermediate", "intermediaire")),
    ("senior", ("senior", "expert", "lead", "principal", "advanced", "avance", "experimente")),
]


def stated_level(text: str) -> Tag | None:
    """The level a posting states outright ("Experience Level: Senior"), or None."""
    folded = fold(text)
    for m in _STATED_LEVEL.finditer(folded):
        words = set(re.findall(r"[a-z]+", m.group(1)))
        for level, vocab in _LEVEL_WORDS:
            if words & set(vocab):
                return Tag("stated_level", level, m.group(0).strip())
    return None


# --- clearance and citizenship ------------------------------------------------

_CLEARANCE_CONTEXT = re.compile(
    r"clearance|habilitation|cote de (?:securite|fiabilite)|reliability status|public trust"
    r"|controlled goods|ts\s*/\s*sci|polygraph|top secret|tres secret|attestation de securite"
    r"|enquete de securite"
)
_CLEARANCE_LEVELS = [
    ("ts_sci", re.compile(r"ts\s*/\s*sci|top secret\s*/\s*sci")),
    ("top_secret", re.compile(r"top secret|(?<!\w)ts clearance|tres secret")),
    ("secret", re.compile(r"(?<!\w)secret(?!\w)")),
    ("public_trust", re.compile(r"public trust")),
    ("reliability", re.compile(
        r"reliability status|enhanced reliability|cote de fiabilite|fiabilite approfondie"
    )),
    ("controlled_goods", re.compile(r"controlled goods")),
]
_CLEARANCE_MUST_HAVE = re.compile(
    r"(?:must|required to|need to|should)\s+(?:have|possess|hold|maintain)\s+(?:an?\s+)?"
    r"(?:active|current)(?:[^.;]{0,30}clearance|\s+(?:ts\s*/\s*sci|top secret|secret)(?!\w))"
    r"|(?:active|current|existing)\s+(?:ts\s*/\s*sci|top secret|secret|security|dod)?\s*"
    r"(?:security\s+)?clearance\s+(?:is\s+)?(?:required|needed|mandatory)"
    r"|detenir une cote|cote de (?:securite|fiabilite) valide"
)
_CLEARANCE_ACTIVE = re.compile(
    r"(?:active|current|existing)\s+(?:ts\s*/\s*sci|top secret|secret|security|dod)?\s*"
    r"(?:security\s+)?clearance"
    r"|(?:active|current)\s+(?:ts\s*/\s*sci|top secret|secret)(?!\w)"
)
_CLEARANCE_OBTAINABLE = re.compile(
    r"(?:ability|able|eligible|eligibility)\s+to\s+(?:obtain|be granted|receive)"
    r"|obtain and maintain|eligible for|capable d'obtenir|capacite (?:a|d') obtenir"
    r"|admissible a|pouvoir obtenir|etre en mesure d'obtenir"
)
_POLYGRAPH = re.compile(r"polygraph|(?<!\w)(?:ci|full scope|fs)\s+poly(?!\w)")
_CITIZENSHIP = [
    ("us", re.compile(
        r"(?:us|u\.s\.|united states)\s+citizen(?:ship)?\s+(?:is\s+)?required"
        r"|must be an?\s+(?:us|u\.s\.|united states)\s+citizen"
    )),
    ("ca", re.compile(
        r"canadian citizen(?:ship)?\s+(?:is\s+)?required|must be an? canadian citizen"
        r"|citoyennete canadienne"
    )),
]


def clearance_tags(text: str) -> list[Tag]:
    folded = fold(text)
    tags: list[Tag] = []
    context = _CLEARANCE_CONTEXT.search(folded)
    if context:
        level = next((name for name, p in _CLEARANCE_LEVELS if p.search(folded)), "unspecified")
        if m := _CLEARANCE_MUST_HAVE.search(folded):
            requirement = "active"
        elif m := _CLEARANCE_OBTAINABLE.search(folded):
            requirement = "obtainable"
        elif m := _CLEARANCE_ACTIVE.search(folded):
            requirement = "active"
        else:
            requirement, m = "required", context
        tags.append(Tag("clearance", f"{level}:{requirement}", _evidence(folded, m)))
        if poly := _POLYGRAPH.search(folded):
            tags.append(Tag("clearance_polygraph", "yes", _evidence(folded, poly)))
    for country, pattern in _CITIZENSHIP:
        if m := pattern.search(folded):
            tags.append(Tag("citizenship_required", country, _evidence(folded, m)))
    return tags


# --- French -----------------------------------------------------------------

_FRENCH_WORDS = r"(?:french|francais|bilingual|bilingue|bilinguisme)"
_NOT_REQUIRED = r"(?:(?!required|requis|obligatoire|exige|mandatory|essential)[^.;])"
_FRENCH_ASSET = [
    re.compile(
        rf"{_FRENCH_WORDS}{_NOT_REQUIRED}{{0,40}}"
        r"(?:asset|atout|a plus|nice to have|preferred|un plus|is a bonus)"
    ),
    re.compile(rf"(?:asset|atout)[^.;]{{0,20}}{_FRENCH_WORDS}"),
]
# Explicit enough to beat an "asset" phrase elsewhere in the same posting, e.g.
# "Maîtrise du français; l'espagnol est un atout".
_FRENCH_REQUIRED_STRONG = [
    re.compile(r"francais\s+(?:est\s+)?(?:requis|obligatoire|exige|essentiel)"),
    re.compile(r"maitrise\s+(?:du|de la langue)\s+francaise?"),
    re.compile(r"french\s+(?:is\s+)?(?:required|mandatory|essential)"),
]
_FRENCH_REQUIRED = [
    re.compile(r"(?:excellent|strong|fluent|professional|native)[^.;]{0,20}french"),
    re.compile(r"french[^.;]{0,25}(?:required|mandatory|essential|must)"),
    re.compile(
        r"(?:bilingual|bilingue|bilinguisme)[^.;]{0,40}"
        r"(?:required|requis|obligatoire|exige|essential|must)"
    ),
    re.compile(r"(?:required|requis|must be|doit etre)[^.;]{0,20}(?:bilingual|bilingue)"),
    re.compile(r"bilingual\s*\(?\s*(?:english|french)\s*(?:/|and|&|-)\s*(?:french|english)"),
    re.compile(r"bilingue\s*\(?\s*(?:francais|anglais)\s*(?:/|et|&|-)\s*(?:anglais|francais)"),
    re.compile(r"francais\s+(?:ecrit et parle|parle et ecrit)"),
    re.compile(r"(?:speak|speaks|speaking)\s+french|french[- ]speaking|francophone"),
]
_FR_STOPWORDS = frozenset({
    "le", "la", "les", "des", "et", "pour", "vous", "nous", "une", "du", "est", "avec",
    "dans", "au", "aux", "sur", "notre", "votre", "qui",
})
_EN_STOPWORDS = frozenset({
    "the", "and", "for", "you", "we", "with", "is", "are", "our", "your", "to", "of", "in",
    "on", "will", "this", "that", "who",
})


def posting_language(text: str) -> str:
    words = re.findall(r"\w+", fold(text))
    fr = sum(w in _FR_STOPWORDS for w in words)
    en = sum(w in _EN_STOPWORDS for w in words)
    return "fr" if fr >= 5 and fr > en * 1.2 else "en"


def french_tags(text: str) -> list[Tag]:
    folded = fold(text)
    language = posting_language(text)
    tags = [Tag("posting_language", language)]
    for value, patterns in (
        ("required", _FRENCH_REQUIRED_STRONG),
        ("asset", _FRENCH_ASSET),
        ("required", _FRENCH_REQUIRED),
    ):
        for pattern in patterns:
            if m := pattern.search(folded):
                tags.append(Tag("french", value, _evidence(folded, m)))
                return tags
    if language == "fr":
        tags.append(Tag("french", "required", "posting is written in French"))
    return tags


# --- employment type --------------------------------------------------------

# "Contract with possible conversion to full-time" is a contract now, not full-time.
_CONVERSION = re.compile(
    r"(?:conversion|convert(?:ing)?|transition|potential|possibility)\s+(?:to|into|for)\s+"
    r"(?:a\s+)?(?:full|permanent)[- ]?time|converti[a-z]*\s+en\s+(?:poste\s+)?(?:a\s+)?temps plein"
)
_PART_TIME = re.compile(r"(?<!\w)(?:part[- ]?time|temps partiel|mi-temps)(?!\w)")
_FULL_TIME = re.compile(r"(?<!\w)(?:full[- ]?time|temps plein|temps complet)(?!\w)")
_ADZUNA_EMPLOYMENT = {"full_time": "full_time", "part_time": "part_time"}


def employment_tag(job: Job) -> Tag:
    """full_time / part_time / either / unknown.

    What the posting says beats Adzuna's contract_time field, which is missing for most
    postings and sometimes wrong (seen: "part_time" on a posting that says "Full-Time").
    """
    text = _CONVERSION.sub(" ", fold(f"{job.title}\n{job.description}"))
    part, full = _PART_TIME.search(text), _FULL_TIME.search(text)
    if part and full:
        return Tag("employment", "either", _evidence(text, part, pad=30))
    if m := part or full:
        return Tag("employment", "part_time" if part else "full_time", _evidence(text, m, pad=30))
    if value := _ADZUNA_EMPLOYMENT.get(job.contract_time or ""):
        return Tag("employment", value, "Adzuna contract_time field")
    return Tag("employment", "unknown")


# --- title-based ------------------------------------------------------------


def role_family(title: str, families: dict[str, list[str]]) -> Tag | None:
    folded = fold_title(title)
    for family, terms in families.items():
        if term := find_any(folded, terms):
            return Tag("role_family", family, term)
    return None


def tag_job(job: Job, profile: Profile) -> list[Tag]:
    title = fold_title(job.title)
    text = f"{job.title}\n{job.description}"
    seniority = profile.seniority
    tags: list[Tag] = []

    if family := role_family(job.title, profile.role_families):
        tags.append(family)

    level = stated_level(job.description)
    if level:
        tags.append(level)

    entry = find_any(title, seniority.entry_level_title)
    if entry:
        tags.append(Tag("entry_level", "title", entry))
    elif desc_entry := find_any(job.description, seniority.entry_level_description):
        tags.append(Tag("entry_level", "description", desc_entry))
    elif level and level.value == "entry":
        tags.append(Tag("entry_level", "description", level.evidence))
    if not entry:
        if senior := find_any(title, seniority.exclude_title):
            tags.append(Tag("seniority", "senior", senior))
        elif level and level.value == "senior":
            tags.append(Tag("seniority", "senior", level.evidence))

    if found := experience_years(text):
        tags.append(Tag("experience_years", str(found[0]), found[1]))
    else:
        tags.append(Tag("experience_years", "unknown"))

    tags.extend(clearance_tags(text))
    tags.extend(french_tags(text))
    tags.append(employment_tag(job))
    tags.append(Tag("work_mode", job.work_mode or "unknown"))
    if job.salary_min is None and job.salary_max is None:
        tags.append(Tag("salary", "none"))
    else:
        tags.append(Tag("salary", "estimated" if job.salary_is_predicted else "listed"))
    return tags
