"""Reading a posting's requirements with a local language model.

The model answers in a fixed JSON shape (Extraction, enforced by Ollama). Every claim that
can rule a job out comes with a quote, and a claim whose quote can't be found in the
posting is discarded: a small model sometimes invents a requirement (in testing, one added
a security clearance the posting never mentioned).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, Field

from job_matcher.models import Job
from job_matcher.text import fold_key

# Raise when the instructions or fields change: the model reads those jobs again.
PROMPT_VERSION = "3"  # 2: provable degree substitution; 3: stricter substitute quotes
# Raise when check_quotes() changes: saved answers are checked again, without the model.
CHECK_VERSION = "1"

Level = Literal["entry", "junior", "mid", "senior", "unclear"]
Degree = Literal["none", "bachelor", "master", "phd"]
Authorization = Literal[
    "none_stated", "us_citizen", "us_citizen_or_pr", "no_sponsorship", "canada", "other"
]
Clearance = Literal["none", "must_obtain", "must_hold"]


class Extraction(BaseModel):
    """What the model returns for one posting. Field order is the order it writes them."""

    years_required: int | None = Field(ge=0, le=40)
    years_quote: str
    degree_substitutes_for_years: bool
    degree_substitute_quote: str
    years_with_degree: int | None = Field(ge=0, le=40)
    entry_level_band: bool
    entry_level_quote: str
    level: Level
    must_have_skills: list[str]
    nice_to_have_skills: list[str]
    degree_required: Degree
    work_authorization: Authorization
    work_authorization_quote: str
    clearance: Clearance
    clearance_quote: str


# Fields check_quotes() doesn't verify. They're informational: before a filter or the scorer
# relies on one, give it a quote and a check (a test keeps this list complete).
UNCHECKED_FIELDS = ("level", "must_have_skills", "nice_to_have_skills", "degree_required")
QUOTE_FIELDS = ("years_quote", "degree_substitute_quote", "entry_level_quote",
                "work_authorization_quote", "clearance_quote")


SYSTEM_PROMPT = """\
You read one job posting and extract facts from it for a job seeker. The job seeker has a
bachelor's degree in computer engineering and no professional experience yet.

Answer only from the posting. Never guess: when the posting doesn't say, use null, false,
"unclear", "none" or "none_stated". Every quote must be copied word for word from the
posting (a short phrase is enough); use "" when there is nothing to quote.

- years_required: the fewest years of professional experience the posting requires for
  this role. If it lists several levels ("Engineer I: 0-2 years, Senior: 5+ years"), use
  the lowest. A maximum ("no more than 2 years of experience") is not a requirement: use
  0. null if no number is stated.
- years_quote: the phrase that states the years.
- degree_substitutes_for_years: true only if the posting explicitly accepts a degree
  instead of the experience ("Bachelor's degree OR 2+ years of experience", "or
  equivalent"). A degree required in addition to the years is not a substitute. Most
  postings have none: then false.
- degree_substitute_quote: the phrase offering the degree instead of the years, or "".
- years_with_degree: the years someone with a bachelor's degree in computer engineering
  needs. Computer science, engineering and "a related field" count as matching degrees.
  0 when the degree substitutes for all the years; otherwise the same as years_required.
- entry_level_band: true if the posting hires at an entry, junior, new-grad or "I" level,
  even next to senior levels. entry_level_quote: the phrase that shows it.
- level: the level of the role as the posting describes it.
- must_have_skills / nice_to_have_skills: technical skills only (languages, frameworks,
  tools, platforms), as short names like "Python" or "Kubernetes". Not degrees, years,
  clearances or personal qualities. Required ones in must_have_skills, "preferred",
  "bonus" or "a plus" ones in nice_to_have_skills.
- degree_required: the lowest degree the posting requires ("none" if a degree is only
  preferred or not mentioned).
- work_authorization: only what the posting says about citizenship, work permits, visas
  or sponsorship: us_citizen, us_citizen_or_pr (permanent residents also accepted),
  no_sponsorship (any work permit, but no visa sponsorship), canada (Canadian work
  authorization), other, or none_stated. Never infer it from the job's location or the
  company's country. work_authorization_quote: the phrase.
- clearance: must_hold (an active clearance is required), must_obtain (must be able to
  get one), or none. clearance_quote: the phrase.
"""


def user_message(job: Job) -> str:
    """The posting as the model sees it: title, company, location, then the description."""
    return (
        f"Title: {job.title}\n"
        f"Company: {job.company or 'not stated'}\n"
        f"Location: {job.location_display or 'not stated'}\n\n"
        f"{job.description}"
    )


def response_schema() -> dict[str, Any]:
    """The JSON schema Ollama holds the model to."""
    return Extraction.model_json_schema()


# --- checking quotes ------------------------------------------------------------------

_ELLIPSIS = re.compile(r"\.\.\.|…")
# A quote must also be about its field: a real phrase that says something else ("This role
# is based in Montreal" for a work permit) proves nothing. Matched on fold_key() text.
_FIELD_WORDS = {
    "years": re.compile(r"\b(?:years?|yrs?|ans?|annees?)\b"),
    "degree_substitute": re.compile(r"\b(?:or|equivalent|in lieu|instead|ou|equivalente?)\b"),
    "work_authorization": re.compile(
        r"citizen|authori[sz]|eligib|permit|visa|sponsor|permanent resident|green card"
        r"|citoyen|permis de travail|residence permanente|resident permanent"),
    "clearance": re.compile(r"clearance|secret|public trust|polygraph|cote de securite"
                            r"|habilitation"),
}
_MIN_PIECE_WORDS = 2  # a one-word "quote" proves nothing


def quote_found(quote: str, posting: str) -> bool:
    """Whether a quote appears in the posting, ignoring case, accents and punctuation.

    A quote the model shortened with "..." counts when each of its pieces is found.
    """
    text = f" {fold_key(posting)} "
    pieces = [fold_key(p) for p in _ELLIPSIS.split(quote)]
    pieces = [p for p in pieces if len(p.split()) >= _MIN_PIECE_WORDS]
    return bool(pieces) and all(f" {p} " in text for p in pieces)


@dataclass
class Checked:
    """An extraction after its quotes were checked against the posting."""

    fields: dict[str, Any]
    unverified: list[str] = field(default_factory=list)  # claims dropped for a missing quote


_DEGREE_WORDS = re.compile(r"\b(?:degree|bachelor'?s?|b ?s|b ?sc|b ?eng|diploma|baccalaureat"
                           r"|diplome)\b")


def _quote_fits(quote: str, posting: str, topic: str | None) -> bool:
    """The quote is in the posting and, for a topic, uses a word that belongs to it.

    A degree-for-years substitute must name all three: a degree, years, and an alternative
    ("Bachelor's degree OR 2+ years"). "Degree in CS or equivalent experience" names no
    years: there, experience replaces the degree, not the other way round.
    """
    if not quote_found(quote, posting):
        return False
    if topic is None:
        return True
    key = fold_key(quote)
    if topic == "degree_substitute":
        return all(p.search(key) for p in (_FIELD_WORDS["degree_substitute"], _DEGREE_WORDS,
                                           _FIELD_WORDS["years"]))
    return bool(_FIELD_WORDS[topic].search(key))


def check_quotes(extraction: Extraction, posting: str) -> Checked:
    """Keep each claim only if its quote is in the posting and is about that claim; reset
    the rest to "not stated".

    Only claims that can rule a job out need a quote. Saying "not stated" needs none.
    """
    fields = extraction.model_dump()
    unverified: list[str] = []

    def drop_unless_quoted(names: list[str], quote_field: str, empty: dict[str, Any],
                           topic: str | None) -> None:
        claimed = any(fields[name] != empty[name] for name in names)
        if claimed and not _quote_fits(fields[quote_field], posting, topic):
            unverified.extend(name for name in names if fields[name] != empty[name])
            fields.update(empty)
            fields[quote_field] = ""

    drop_unless_quoted(["years_required"], "years_quote", {"years_required": None}, "years")
    drop_unless_quoted(["degree_substitutes_for_years"], "degree_substitute_quote",
                       {"degree_substitutes_for_years": False}, "degree_substitute")
    # Fewer years with a degree only when the posting offers the degree instead, and a
    # degree only ever lowers them: never above years_required, and 0 when not given.
    required, with_degree = fields["years_required"], fields["years_with_degree"]
    if not fields["degree_substitutes_for_years"] or required is None:
        fields["years_with_degree"] = required
    else:
        fields["years_with_degree"] = 0 if with_degree is None else min(with_degree, required)
    drop_unless_quoted(["entry_level_band"], "entry_level_quote", {"entry_level_band": False},
                       None)
    drop_unless_quoted(["work_authorization"], "work_authorization_quote",
                       {"work_authorization": "none_stated"}, "work_authorization")
    drop_unless_quoted(["clearance"], "clearance_quote", {"clearance": "none"}, "clearance")
    return Checked(fields, unverified)
