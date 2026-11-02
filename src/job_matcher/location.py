"""Metro bucketing and work-mode inference from location and posting text."""

from __future__ import annotations

from collections.abc import Iterable

from job_matcher.text import find_any, fold_key

# Metro -> (counties, cities). Names are compared after fold_key().
FLORIDA_METROS: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    "orlando": (
        frozenset({"orange county", "seminole county", "osceola county", "lake county"}),
        frozenset({
            "orlando", "kissimmee", "sanford", "winter park", "lake mary", "altamonte springs",
            "oviedo", "clermont", "winter garden", "apopka", "maitland", "celebration",
        }),
    ),
    "tampa": (
        frozenset({"hillsborough county", "pinellas county", "pasco county"}),
        frozenset({
            "tampa", "st petersburg", "saint petersburg", "clearwater", "brandon", "largo",
            "wesley chapel", "riverview", "plant city", "new port richey", "temple terrace",
        }),
    ),
    "miami": (
        frozenset({"miami dade county", "broward county", "palm beach county"}),
        frozenset({
            "miami", "fort lauderdale", "hialeah", "doral", "coral gables", "miami beach",
            "hollywood", "pompano beach", "boca raton", "west palm beach", "miramar",
            "pembroke pines", "plantation", "sunrise", "weston", "delray beach", "jupiter",
        }),
    ),
}

MONTREAL_AREA = frozenset({
    "montreal", "laval", "longueuil", "brossard", "dorval", "pointe claire", "saint laurent",
    "boucherville", "terrebonne", "kirkland", "mont royal", "verdun", "lasalle", "saint leonard",
    "anjou", "repentigny", "blainville", "boisbriand", "mirabel", "sainte therese",
    "dollard des ormeaux", "beaconsfield", "saint lambert", "chateauguay", "vaudreuil dorion",
    "la prairie", "saint hubert", "saint bruno de montarville", "candiac", "westmount",
    "cote saint luc", "mascouche", "saint jerome",
})


def metro_for(country: str, area: Iterable[str], display: str | None = None) -> str | None:
    """Bucket a posting into montreal / orlando / tampa / miami / florida_other, or None."""
    parts = {fold_key(p) for p in area}
    if display:
        parts |= {fold_key(p) for p in display.split(",")}
    parts.discard("")

    if country == "us":
        if "florida" not in parts:
            return None
        for metro, (counties, cities) in FLORIDA_METROS.items():
            if parts & counties or parts & cities:
                return metro
        return "florida_other"

    if country == "ca" and (parts & MONTREAL_AREA or any("montreal" in p for p in parts)):
        return "montreal"
    return None


# Checked in this order: an explicit on-site statement beats a passing "remote" mention.
_ONSITE = (
    "on-site only", "onsite only", "no remote", "not remote", "not a remote position",
    "sur place seulement", "en présentiel seulement", "100 % en présentiel", "100% en présentiel",
    "pas de télétravail", "aucun télétravail",
)
_HYBRID = ("hybrid", "hybride", "télétravail partiel", "partially remote", "partial remote")
_REMOTE = (
    "remote", "fully remote", "work from home", "wfh", "télétravail", "en télétravail",
    "à distance", "100% remote", "100 % télétravail",
)


def infer_work_mode(title: str, description: str) -> str | None:
    """Best-effort work mode from text. None means the snippet doesn't say."""
    text = f"{title}\n{description}"
    if find_any(text, _ONSITE):
        return "onsite"
    if find_any(text, _HYBRID):
        return "hybrid"
    if find_any(text, _REMOTE):
        return "remote"
    return None
