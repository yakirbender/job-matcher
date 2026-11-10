"""Reading free-text job locations, as company job boards write them.

Adzuna gives a structured country > state > county > city list. Job boards give text:
"Orlando, FL", "Ontario, CAN", "Remote-Friendly, United States", or several places in
one string ("London, UK; Ontario, CAN"). This turns each place into a country code and an
area list shaped like Adzuna's, so metro_for() works on both.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from job_matcher.location import MONTREAL_AREA, OTTAWA_AREA, TORONTO_AREA
from job_matcher.text import fold_key

US_STATES = {
    "al": "Alabama", "ak": "Alaska", "az": "Arizona", "ar": "Arkansas", "ca": "California",
    "co": "Colorado", "ct": "Connecticut", "de": "Delaware", "dc": "District of Columbia",
    "fl": "Florida", "ga": "Georgia", "hi": "Hawaii", "id": "Idaho", "il": "Illinois",
    "in": "Indiana", "ia": "Iowa", "ks": "Kansas", "ky": "Kentucky", "la": "Louisiana",
    "me": "Maine", "md": "Maryland", "ma": "Massachusetts", "mi": "Michigan",
    "mn": "Minnesota", "ms": "Mississippi", "mo": "Missouri", "mt": "Montana",
    "ne": "Nebraska", "nv": "Nevada", "nh": "New Hampshire", "nj": "New Jersey",
    "nm": "New Mexico", "ny": "New York", "nc": "North Carolina", "nd": "North Dakota",
    "oh": "Ohio", "ok": "Oklahoma", "or": "Oregon", "pa": "Pennsylvania",
    "ri": "Rhode Island", "sc": "South Carolina", "sd": "South Dakota", "tn": "Tennessee",
    "tx": "Texas", "ut": "Utah", "vt": "Vermont", "va": "Virginia", "wa": "Washington",
    "wv": "West Virginia", "wi": "Wisconsin", "wy": "Wyoming",
}

CA_PROVINCES = {
    "ab": "Alberta", "bc": "British Columbia", "mb": "Manitoba", "nb": "New Brunswick",
    "nl": "Newfoundland and Labrador", "ns": "Nova Scotia", "nt": "Northwest Territories",
    "nu": "Nunavut", "on": "Ontario", "pe": "Prince Edward Island", "qc": "Quebec",
    "sk": "Saskatchewan", "yt": "Yukon",
}

# Folded name or abbreviation -> (code, label). "us" and "ca" are the target countries;
# other countries are known only so a job there isn't mistaken for a US or Canadian one.
COUNTRIES = {
    "us": ("us", "US"), "usa": ("us", "US"), "united states": ("us", "US"),
    "united states of america": ("us", "US"),
    "canada": ("ca", "Canada"), "can": ("ca", "Canada"),
}
for _name in ("united kingdom", "germany", "france", "ireland", "india", "japan",
              "australia", "netherlands", "spain", "portugal", "italy", "poland", "israel",
              "singapore", "brazil", "mexico", "switzerland", "sweden", "denmark", "norway",
              "finland", "austria", "belgium", "argentina", "colombia", "philippines",
              "south korea", "china", "new zealand", "romania", "ukraine", "czech republic"):
    COUNTRIES[_name] = (_name, _name.title())
COUNTRIES["uk"] = COUNTRIES["england"] = ("united kingdom", "United Kingdom")

# Two-letter country codes ("Dublin, IE"). Codes that are also a state or province ("DE"
# for Germany or Delaware, "IN" for India or Indiana) are left out: they stay states.
_ISO_CODES = {
    "gb": "united kingdom", "ie": "ireland", "ch": "switzerland", "fr": "france",
    "jp": "japan", "au": "australia", "nl": "netherlands", "es": "spain", "pt": "portugal",
    "it": "italy", "pl": "poland", "sg": "singapore", "br": "brazil", "mx": "mexico",
    "se": "sweden", "dk": "denmark", "fi": "finland", "at": "austria", "be": "belgium",
    "nz": "new zealand", "kr": "south korea", "cn": "china", "ph": "philippines",
    "ro": "romania", "ua": "ukraine", "cz": "czech republic",
}

for _code, _name in _ISO_CODES.items():
    if _code not in US_STATES and _code not in CA_PROVINCES:
        COUNTRIES[_code] = COUNTRIES[_name]

_STATE_NAMES = {fold_key(name): name for name in US_STATES.values()}
_PROVINCE_NAMES = {fold_key(name): name for name in CA_PROVINCES.values()}

# A target Canadian city named alone is enough to place it.
_CANADIAN_CITIES = (
    [(city, "Quebec") for city in MONTREAL_AREA]
    + [(city, "Quebec" if city in {"gatineau", "aylmer", "hull"} else "Ontario")
       for city in OTTAWA_AREA]
    + [(city, "Ontario") for city in TORONTO_AREA]
)
CANADIAN_CITIES = dict(_CANADIAN_CITIES)

_REMOTE = re.compile(r"\b(?:remote|teletravail|work from home|wfh|anywhere)\b")
# Words that only qualify "remote" ("Remote-Friendly", "Remote First").
_REMOTE_FILLER = re.compile(r"\b(?:friendly|first|only|based|eligible|ok|in|within|from)\b")
# Pieces that describe the job, not a place.
_NOT_A_PLACE = {"travel required", "hybrid", "on site", "onsite", "in office", "flexible"}
_PIECE_SPLIT = re.compile(r"\s*[|;]\s*")
_PART_SPLIT = re.compile(r"\s*(?:,|\(|\)|\s[-–]\s)\s*")


@dataclass(frozen=True)
class Place:
    country: str | None  # "us", "ca", another country's name, or None when unknown
    area: tuple[str, ...]  # e.g. ("US", "Florida", "Orlando"), like Adzuna's location_area
    remote: bool
    text: str  # the original text


def _key(part: str) -> str:
    """fold_key, with dotted abbreviations closed up: "D.C." and "U.S.A." become "dc", "usa"."""
    key = fold_key(part)
    words = key.split()
    return "".join(words) if len(words) > 1 and all(len(w) == 1 for w in words) else key


def _is_region(part: str) -> bool:
    """A state or province, not counting "CA", which may mean Canada ("Ontario, CA")."""
    key = _key(part)
    if key == "ca":
        return False
    return any(key in table for table in (US_STATES, _STATE_NAMES, CA_PROVINCES, _PROVINCE_NAMES))


def parse_place(text: str) -> Place:
    """Read one place: "Orlando, FL", "Remote - Canada", "London, UK", ..."""
    remote = False
    country: tuple[str, str] | None = None
    region: str | None = None
    province_seen = False
    unknown: list[str] = []

    parts = _PART_SPLIT.split(text)
    for index, part in enumerate(parts):
        key = _key(part)
        if _REMOTE.search(key):
            remote = True
            key = " ".join(_REMOTE_FILLER.sub(" ", _REMOTE.sub(" ", key)).split())
            part = key
        if not key or key in _NOT_A_PLACE:
            continue
        if index == 0 and any(_is_region(p) for p in parts[1:]):
            unknown.append(part.strip())  # "Washington, DC", "New York, NY": first is the city
            continue
        if key == "ca":  # Canada after a province ("Ontario, CA"), otherwise California
            if province_seen:
                country = COUNTRIES["canada"]
            else:
                region = region or "California"
        elif key in COUNTRIES:
            country = COUNTRIES[key]
        elif key in CA_PROVINCES or key in _PROVINCE_NAMES:
            region = CA_PROVINCES.get(key) or _PROVINCE_NAMES[key]
            province_seen = True
        elif key in US_STATES or key in _STATE_NAMES:
            region = US_STATES.get(key) or _STATE_NAMES[key]
        else:
            unknown.append(part.strip())

    if country is None and region is not None:
        country = ("ca", "Canada") if province_seen else ("us", "US")
    if country is None and unknown and fold_key(unknown[0]) in CANADIAN_CITIES:
        country, region = ("ca", "Canada"), CANADIAN_CITIES[fold_key(unknown[0])]

    if country is None:
        return Place(None, tuple(unknown), remote, text)
    city = unknown[:1]
    area = (country[1], *([region] if region and country[0] in ("us", "ca") else []), *city)
    return Place(country[0], area, remote, text)


def parse_places(text: str) -> list[Place]:
    """Read every place in a string that may list several ("A | B", "A; B").

    A piece that only says "remote" belongs to the place beside it: Ashby writes
    "United States | Remote" for one remote place.
    """
    places = [parse_place(p) for p in _PIECE_SPLIT.split(text) if p.strip()]
    merged: list[Place] = []
    waiting: Place | None = None  # a bare "Remote" that came before any real place
    for place in places:
        if place.remote and place.country is None and not place.area and len(places) > 1:
            if merged:
                last = merged[-1]
                merged[-1] = Place(last.country, last.area, True, f"{last.text} | {place.text}")
            else:
                waiting = place
            continue
        if waiting:
            place = Place(place.country, place.area, True, f"{waiting.text} | {place.text}")
            waiting = None
        merged.append(place)
    if waiting:  # nothing but bare "Remote" pieces
        merged.append(waiting)
    return merged
