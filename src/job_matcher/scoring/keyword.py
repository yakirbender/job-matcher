"""Free, local, explainable scoring: skill overlap with the CV plus experience-level fit,
adjusted by the profile's ranking points. No API calls.

Base score (max 90):
  skills  0-60      30 is neutral. Moves toward 60 or 0 with the share of the job's
                    named skills found in the CV (related skills count half), weighted
                    by how many it names, n / (n + 2): one named skill moves it at most
                    10 points, since snippets often name only one or two.
  level   up to 30  entry-level title 30, entry-level wording 22, 0-1 years 26,
                    2 years 20, unstated 15, stretch title ("II") 10
Adjustments: French, metro, remote, clearance, suspicious (profile.example.yaml
`ranking`). Clamped 0-100.
"""

from __future__ import annotations

from job_matcher.cv import CvProfile, Vocabulary, find_skills
from job_matcher.models import Job
from job_matcher.profile import Profile
from job_matcher.scoring.base import ScoreResult
from job_matcher.tagging import Tag
from job_matcher.text import find_any, fold_title

SKILLS_MAX = 60
SKILLS_NEUTRAL = 30
EVIDENCE_PRIOR = 2  # with n named skills, the swing from neutral is scaled by n / (n + 2)
SECTION_NAMES = {"skills": "Skills", "projects": "Projects", "experience": "Experience",
                 "education": "Education", "header": "summary", "languages": "Languages"}


def _tag(tags: list[Tag], name: str) -> Tag | None:
    return next((t for t in tags if t.name == name), None)


def _signed(points: int) -> str:
    return f"+{points}" if points > 0 else str(points)


class KeywordScorer:
    name = "keyword"
    version = "4"  # 2: evidence-weighted skills + related skills; 3: stated mid level; 4: remote

    def __init__(self, cv: CvProfile, profile: Profile, vocabulary: Vocabulary) -> None:
        self.cv = cv
        self.profile = profile
        self.vocabulary = vocabulary

    def score(self, job: Job, tags: list[Tag]) -> ScoreResult:
        result = ScoreResult(score=0)
        self._skills(job, result)
        self._level(job, tags, result)
        self._adjustments(job, tags, result)
        self._related_experience(tags, result)
        result.score = max(0, min(100, sum(result.components.values())))
        return result

    # --- base components ------------------------------------------------------

    def _skills(self, job: Job, result: ScoreResult) -> None:
        wanted = find_skills(f"{job.title}\n{job.description}", self.vocabulary.jobs)
        have = set(self.cv.skills)
        result.matched = sorted(wanted & have)
        related = {s: sorted(set(self.vocabulary.related.get(s, [])) & have)
                   for s in sorted(wanted - have)}
        related = {s: via for s, via in related.items() if via}
        result.missing = sorted(wanted - have - set(related))
        if not wanted:
            result.components["skills"] = SKILLS_NEUTRAL
            result.explanation.append("Skills: the snippet names no specific skills (neutral).")
            return

        n = len(wanted)
        share = (len(result.matched) + 0.5 * len(related)) / n
        swing = (SKILLS_MAX - SKILLS_NEUTRAL) * (2 * share - 1) * n / (n + EVIDENCE_PRIOR)
        result.components["skills"] = round(SKILLS_NEUTRAL + swing)

        parts = []
        if result.matched:
            parts.append("you have " + "; ".join(
                f"{s} ({', '.join(SECTION_NAMES.get(x, x) for x in self.cv.skills[s])})"
                for s in result.matched
            ))
        if related:
            parts.append("related, half credit: " + "; ".join(
                f"{s} (you have {', '.join(via)})" for s, via in related.items()
            ))
        if result.missing:
            parts.append(f"not in your CV: {', '.join(result.missing)}")
        result.explanation.append(f"Skills ({n} named): " + ". ".join(parts) + ".")

    def _level(self, job: Job, tags: list[Tag], result: ScoreResult) -> None:
        entry = _tag(tags, "entry_level")
        experience = _tag(tags, "experience_years")
        years = experience.value if experience else "unknown"
        stretch = find_any(fold_title(job.title), self.profile.seniority.stretch_title)
        stated = _tag(tags, "stated_level")

        if entry and entry.value == "title":
            points, why = 30, f"entry-level title ('{entry.evidence}')"
        elif stretch:
            points, why = 10, f"title says '{stretch}', which usually means 1-3 years; a stretch"
        elif stated and stated.value == "mid":
            points, why = 10, f"posting says mid level ('{stated.evidence}'); a stretch"
        elif years.isdigit() and int(years) <= 1:
            points, why = 26, f"asks for {years}+ years of experience"
        elif entry:
            points, why = 22, f"description mentions '{entry.evidence}'"
        elif years.isdigit():
            points, why = 20, f"asks for {years}+ years of experience"
        else:
            points, why = 15, "experience level not stated in the snippet"
        result.components["level"] = points
        result.explanation.append(f"Level: {why}.")

    # --- adjustments ------------------------------------------------------------

    def _adjustments(self, job: Job, tags: list[Tag], result: ScoreResult) -> None:
        ranking = self.profile.ranking

        french = _tag(tags, "french")
        if french and french.value in ("required", "asset"):
            points = ranking.french_required if french.value == "required" else ranking.french_asset
            if points:
                result.components["french"] = points
                result.explanation.append(
                    f"French {french.value}: you have professional French ({_signed(points)})."
                )

        if job.metro and (points := ranking.metro.get(job.metro, 0)):
            result.components["location"] = points
            result.explanation.append(f"Location: {job.metro} ({_signed(points)}).")

        if job.work_mode == "remote" and ranking.remote:
            result.components["remote"] = ranking.remote
            result.explanation.append(
                f"Remote: open to a larger applicant pool ({_signed(ranking.remote)})."
            )

        clearance = _tag(tags, "clearance")
        if clearance:
            level, _, requirement = clearance.value.partition(":")
            points = getattr(ranking.clearance, requirement, ranking.clearance.required)
            label = {
                "obtainable": "must be able to obtain one",
                "active": "requires an already-active clearance, rare for new grads",
                "required": "clearance mentioned; unclear whether it must already be active",
            }.get(requirement, requirement)
            if _tag(tags, "clearance_polygraph"):
                points += ranking.clearance.polygraph
                label += "; polygraph"
            result.components["clearance"] = points
            result.explanation.append(f"Clearance ({level}): {label} ({_signed(points)}).")

        suspicious = [t for t in tags if t.name == "suspicious"]
        if suspicious and ranking.suspicious:
            result.components["suspicious"] = ranking.suspicious
            kinds = ", ".join(sorted({t.value for t in suspicious}))
            result.explanation.append(
                f"Suspicious ({kinds}): verify before applying ({_signed(ranking.suspicious)})."
            )

    def _related_experience(self, tags: list[Tag], result: ScoreResult) -> None:
        family = _tag(tags, "role_family")
        phrases = self.profile.ranking.relevant_experience.get(family.value if family else "", [])
        if phrases and (found := self.cv.mentions(phrases)):
            result.explanation.append(
                f"Related experience: your CV mentions '{found}' (shown only, not scored)."
            )
