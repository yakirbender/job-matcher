"""Hard filters: decide from a job and its tags whether it's worth scoring at all.

Every rejection carries a reason code and detail, so `jm show` can explain it. Soft
concerns that shouldn't reject a job (unknown experience, estimated salary) are notes.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from job_matcher.models import Job
from job_matcher.profile import Profile
from job_matcher.tagging import Tag
from job_matcher.text import find_any, fold_title


@dataclass(frozen=True)
class Reason:
    code: str
    detail: str


@dataclass
class FilterResult:
    reasons: list[Reason] = field(default_factory=list)
    notes: list[Reason] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.reasons


def _tag(tags: list[Tag], name: str) -> Tag | None:
    return next((t for t in tags if t.name == name), None)


def evaluate(
    job: Job, tags: list[Tag], profile: Profile, duplicate_of: int | None = None
) -> FilterResult:
    result = FilterResult()
    reject, note = result.reasons.append, result.notes.append
    title = fold_title(job.title)

    if duplicate_of is not None:
        reject(Reason("duplicate", f"likely repost of job {duplicate_of}"))

    if _tag(tags, "role_family") is None:
        reject(Reason("not_software", "title matches no role family"))

    if senior := _tag(tags, "seniority"):
        reject(Reason("seniority", f"title has '{senior.evidence}'"))

    if word := find_any(title, profile.exclude.title):
        reject(Reason("internship", f"title has '{word}'"))

    if word := find_any(f"{job.title}\n{job.description}", profile.exclude.text):
        reject(Reason("excluded_keyword", f"posting has '{word}'"))

    if job.company and (name := find_any(job.company, profile.exclude.companies)):
        reject(Reason("company", f"company '{name}' is excluded"))

    experience = _tag(tags, "experience_years")
    limit = profile.seniority.max_years_required
    if experience is None or experience.value == "unknown":
        note(Reason("experience_unknown", "snippet doesn't state required experience"))
    elif limit is not None and int(experience.value) > limit:
        reject(Reason("experience", f"asks for {experience.value}+ years: {experience.evidence}"))

    work_mode = job.work_mode or "unknown"
    if job.metro not in profile.locations.metros:
        if work_mode in ("hybrid", "onsite"):
            where = job.location_display or "an unlisted location"
            reject(Reason("location", f"{work_mode} in {where}"))
        elif work_mode == "unknown":
            note(Reason("remote_unconfirmed", "outside target metros; snippet doesn't say remote"))

    minimums = profile.salary.min or {}
    floor = minimums.get(job.salary_currency or "")
    top = job.salary_max or job.salary_min
    if floor is not None and top is not None:
        if job.salary_is_predicted:
            note(Reason("salary_estimated", "salary is Adzuna's estimate, not the employer's"))
        elif top < floor:
            reject(Reason("salary", f"listed {top:,.0f} {job.salary_currency} < {floor:,.0f}"))

    for flag in (t for t in tags if t.name == "suspicious"):
        reason = Reason("suspicious", f"{flag.value}: {flag.evidence}")
        (reject if profile.suspicious.mode == "exclude" else note)(reason)

    clearance = _tag(tags, "clearance")
    excluding = profile.clearance.mode == "exclude_active"
    if clearance and excluding and clearance.value.endswith(":active"):
        reject(Reason("clearance", f"requires an active clearance ({clearance.value})"))

    return result
