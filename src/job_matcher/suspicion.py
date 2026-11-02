"""Signals that a posting may be mass-produced or a scam.

These mark postings for a closer look; they don't prove anything. Tested on stored data:
a naive "same opening text" check flags big legitimate employers (company boilerplate),
so the template check below requires the mail-merge pattern instead.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable

from job_matcher.models import Job
from job_matcher.tagging import Tag
from job_matcher.text import fold, fold_key

_SCAM_PHRASES = re.compile(
    r"upfront fee|registration fee|training fee|starter kit fee|pay for (?:your )?training"
    r"|send (?:us )?money|wire transfer|gift cards?|cashier'?s check|check deposit"
    r"|telegram|whatsapp|signal app|text to apply|interview (?:via|over) (?:chat|text)"
    r"|no interview|unlimited earning|be your own boss|personal bank account"
    r"|frais d'inscription|frais de formation|carte[s]? cadeau|virement bancaire"
)
_TEMPLATE_WORDS = 15  # words compared after the title


def scam_phrase_tag(text: str) -> Tag | None:
    folded = fold(text)
    if m := _SCAM_PHRASES.search(folded):
        start, end = max(m.start() - 40, 0), m.end() + 40
        return Tag("suspicious", "scam_phrase", folded[start:end].strip())
    return None


def _words(text: str) -> list[str]:
    return re.findall(r"\w+", fold(text))


def mass_template_tags(jobs: Iterable[tuple[int, Job]], min_titles: int) -> dict[int, Tag]:
    """Jobs whose description is the job title followed by text the same company reuses
    word for word across at least `min_titles` different titles ("SAP Developer – Remote
    Acme is a…", "Rust Developer – Remote Acme is a…").
    """
    jobs = list(jobs)
    groups: dict[tuple[str, str], list[tuple[int, Job]]] = defaultdict(list)
    for job_id, job in jobs:
        title, desc = _words(job.title), _words(job.description)
        if not job.company or not title or desc[: len(title)] != title:
            continue
        after = " ".join(desc[len(title) : len(title) + _TEMPLATE_WORDS])
        groups[(fold_key(job.company), after)].append((job_id, job))

    tags: dict[int, Tag] = {}
    for (company_key, after), members in groups.items():
        titles = {fold_key(job.title) for _, job in members}
        if len(titles) < min_titles:
            continue
        company = members[0][1].company
        evidence = (f"{company} posts the same text with only the title changed, "
                    f"across {len(titles)} different titles")
        for job_id, _ in members:
            tags[job_id] = Tag("suspicious", "mass_template", evidence)

        # The same company's other postings that reuse the template text near the start,
        # e.g. a variant that opens with the company name instead of the title. Only
        # applies to a company already caught mail-merging, so boilerplate elsewhere is safe.
        core = " ".join(after.split()[2:12])
        for job_id, job in jobs:
            if job_id in tags or not job.company or fold_key(job.company) != company_key:
                continue
            if core and core in " ".join(_words(job.description)[:40]):
                tags[job_id] = Tag("suspicious", "mass_template", evidence)
    return tags
