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

TEXAS_METROS: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    "austin": (
        frozenset({"travis county", "williamson county", "hays county"}),
        frozenset({
            "austin", "round rock", "cedar park", "georgetown", "pflugerville", "san marcos",
            "leander", "kyle", "lakeway",
        }),
    ),
    "dallas": (
        frozenset({"dallas county", "tarrant county", "collin county", "denton county"}),
        frozenset({
            "dallas", "fort worth", "plano", "irving", "arlington", "frisco", "mckinney",
            "richardson", "garland", "denton", "addison", "carrollton", "allen",
            "grand prairie", "lewisville", "southlake", "coppell", "flower mound",
        }),
    ),
    "houston": (
        frozenset({"harris county", "fort bend county", "montgomery county"}),
        frozenset({
            "houston", "sugar land", "the woodlands", "katy", "pearland", "pasadena", "spring",
            "cypress", "conroe", "league city", "baytown", "missouri city",
        }),
    ),
    "san_antonio": (
        frozenset({"bexar county", "comal county"}),
        frozenset({"san antonio", "new braunfels", "boerne", "schertz", "converse"}),
    ),
}

# State -> (its metros, the bucket for the rest of the state).
US_STATE_METROS = {
    "florida": (FLORIDA_METROS, "florida_other"),
    "texas": (TEXAS_METROS, "texas_other"),
}

OTTAWA_AREA = frozenset({
    "ottawa", "gatineau", "kanata", "nepean", "orleans", "barrhaven", "gloucester",
    "stittsville", "aylmer", "hull",
})

TORONTO_AREA = frozenset({
    "toronto", "mississauga", "brampton", "markham", "vaughan", "richmond hill", "oakville",
    "scarborough", "north york", "etobicoke", "pickering", "ajax", "burlington", "milton",
    "oshawa", "whitby", "newmarket", "aurora", "concord", "thornhill", "woodbridge",
})

MONTREAL_AREA = frozenset({
    "montreal", "laval", "longueuil", "brossard", "dorval", "pointe claire", "saint laurent",
    "boucherville", "terrebonne", "kirkland", "mont royal", "verdun", "lasalle", "saint leonard",
    "anjou", "repentigny", "blainville", "boisbriand", "mirabel", "sainte therese",
    "dollard des ormeaux", "beaconsfield", "saint lambert", "chateauguay", "vaudreuil dorion",
    "la prairie", "saint hubert", "saint bruno de montarville", "candiac", "westmount",
    "cote saint luc", "mascouche", "saint jerome",
})


def metro_for(country: str, area: Iterable[str], display: str | None = None) -> str | None:
    """Bucket a posting into a target metro (montreal, ottawa, toronto; a Florida or Texas
    metro, or florida_other / texas_other for the rest of the state), or None."""
    parts = {fold_key(p) for p in area}
    if display:
        parts |= {fold_key(p) for p in display.split(",")}
    parts.discard("")

    if country == "us":
        for state, (metros, rest_of_state) in US_STATE_METROS.items():
            if state not in parts:
                continue
            for metro, (counties, cities) in metros.items():
                if parts & counties or parts & cities:
                    return metro
            return rest_of_state
        return None

    if country == "ca":
        if parts & MONTREAL_AREA or any("montreal" in p for p in parts):
            return "montreal"
        if parts & OTTAWA_AREA:
            return "ottawa"
        if parts & TORONTO_AREA:
            return "toronto"
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
# Plain mentions, checked last: "Hybrid: 2 days onsite" is hybrid and "remote with
# occasional on-site visits" is remote, but a bare "(Onsite)" means on-site.
_ONSITE_PLAIN = (
    "onsite", "on-site", "on site", "in-office", "in office", "sur place", "en présentiel",
    "présentiel",
)


def infer_work_mode(title: str, description: str) -> str | None:
    """Best-effort work mode from text. None means the snippet doesn't say."""
    text = f"{title}\n{description}"
    for mode, phrases in (
        ("onsite", _ONSITE),
        ("hybrid", _HYBRID),
        ("remote", _REMOTE),
        ("onsite", _ONSITE_PLAIN),
    ):
        if find_any(text, phrases):
            return mode
    return None
