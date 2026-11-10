"""Reading free-text job locations from company job boards."""

import pytest

from job_matcher.places import Place, parse_place, parse_places


@pytest.mark.parametrize(
    ("text", "country", "area", "remote"),
    [
        ("Orlando, FL", "us", ("US", "Florida", "Orlando"), False),
        ("San Francisco, CA", "us", ("US", "California", "San Francisco"), False),
        ("New York City, NY", "us", ("US", "New York", "New York City"), False),
        ("Washington, DC", "us", ("US", "District of Columbia", "Washington"), False),
        ("Washington, D.C.", "us", ("US", "District of Columbia", "Washington"), False),
        ("Boston, U.S.A.", "us", ("US", "Boston"), False),
        ("Dublin, IE", "ireland", ("Ireland", "Dublin"), False),
        ("Berlin, DE", "us", ("US", "Delaware", "Berlin"), False),  # DE stays Delaware
        ("Austin, Texas", "us", ("US", "Texas", "Austin"), False),
        ("The Woodlands, TX, United States", "us", ("US", "Texas", "The Woodlands"), False),
        ("Ontario, CAN", "ca", ("Canada", "Ontario"), False),
        ("Ontario, CA", "ca", ("Canada", "Ontario"), False),  # CA after a province: Canada
        ("Toronto, ON, Canada", "ca", ("Canada", "Ontario", "Toronto"), False),
        ("Montréal, QC", "ca", ("Canada", "Quebec", "Montréal"), False),
        ("Gatineau, Québec", "ca", ("Canada", "Quebec", "Gatineau"), False),
        ("Toronto", "ca", ("Canada", "Ontario", "Toronto"), False),  # a target city alone
        ("Remote-Friendly, United States", "us", ("US",), True),
        ("Remote-Friendly US (Travel Required)", "us", ("US",), True),
        ("United States | Remote", "us", ("US",), True),
        ("Remote - Canada", "ca", ("Canada",), True),
        ("US-Remote", "us", ("US",), True),
        ("Remote in US", "us", ("US",), True),
        ("Remote Based - US", "us", ("US",), True),
        ("Remote within Canada", "ca", ("Canada",), True),
        ("Indianapolis, IN", "us", ("US", "Indiana", "Indianapolis"), False),
        ("Remote (Orlando, FL)", "us", ("US", "Florida", "Orlando"), True),
        ("Télétravail, Québec", "ca", ("Canada", "Quebec"), True),
        ("Remote", None, (), True),
        ("London, UK", "united kingdom", ("United Kingdom", "London"), False),
        ("Wiesbaden, Germany", "germany", ("Germany", "Wiesbaden"), False),
        ("Tokyo, Japan", "japan", ("Japan", "Tokyo"), False),
        ("Springfield", None, ("Springfield",), False),  # nothing known: left as text
        ("", None, (), False),
    ],
)
def test_parse_place(text, country, area, remote):
    place = parse_place(text)
    assert (place.country, place.area, place.remote) == (country, area, remote)
    assert place.text == text


def test_several_places_are_split_on_bars_and_semicolons():
    places = parse_places("London, UK; Ontario, CAN; Remote-Friendly, United States")
    assert [p.country for p in places] == ["united kingdom", "ca", "us"]
    assert [p.remote for p in places] == [False, False, True]
    assert [p.text for p in parse_places("San Francisco, CA | Seattle, WA")] == [
        "San Francisco, CA", "Seattle, WA"]


def test_a_bare_remote_piece_belongs_to_the_place_beside_it():
    # Ashby writes "United States | Remote" for one remote place, not two places.
    assert parse_places("United States | Remote") == [
        Place("us", ("US",), True, "United States | Remote")]


def test_empty_pieces_are_dropped():
    assert parse_places(" | Austin, TX ; ") == [parse_place("Austin, TX")]
