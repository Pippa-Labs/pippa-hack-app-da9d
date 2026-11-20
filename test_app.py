"""Tests for London Wander. The build runs these, and will not publish if any fail.

Everything goes through `LocalRunner`, the same code the runtime uses, so a
result that does not match manifest.yaml fails here exactly as it would live.
The network is replaced with canned responses shaped like the real ones
(captured from Nominatim, Overpass and Wikidata in November 2026).
"""

from __future__ import annotations

import json
import urllib.error
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

import pytest
from pippa_agentapp_sdk import (
    LocalRunner,
    OutputShapeViolation,
    RateLimitedError,
    ServiceUnavailableError,
)

import app as app_module
from app import (
    NO_HOURS,
    App,
    InterestMissing,
    InterestUnknown,
    LocationMissing,
    LocationNotFound,
    match_interest,
    radius_from_inputs,
    tidy_wikidata,
)

NOW = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)

# Covent Garden, as Nominatim returns it.
GEOCODE = [{
    "lat": "51.5117", "lon": "-0.1240", "name": "Covent Garden",
    "display_name": "Covent Garden, City of Westminster, Greater London, England",
}]

COURTAULD = {
    "type": "way", "id": 1, "center": {"lat": 51.5112, "lon": -0.1173},
    "tags": {"name": "The Courtauld Gallery", "tourism": "gallery",
             "opening_hours": "Mo-Su 10:00-18:00", "wikidata": "Q12110695"},
}
MURAL = {
    "type": "node", "id": 2, "lat": 51.5120, "lon": -0.1250,
    "tags": {"name": "Seven Dials Mural", "tourism": "artwork",
             "artwork_type": "mural", "artist_name": "A. Painter"},
}
GALLERY_WITH_ODD_HOURS = {
    "type": "node", "id": 3, "lat": 51.5140, "lon": -0.1210,
    "tags": {"name": "Tiny Gallery", "tourism": "gallery",
             "opening_hours": "Tu-Sa 12:00-17:00 | by appointment"},
}
PLAQUE = {
    "type": "node", "id": 4, "lat": 51.5118, "lon": -0.1241,
    "tags": {"name": "Someone Lived Here", "historic": "memorial",
             "memorial": "plaque"},
}
STATUE = {
    "type": "node", "id": 5, "lat": 51.5080, "lon": -0.1281,
    "tags": {"name": "Statue of Charles I", "historic": "memorial",
             "memorial": "statue", "wikidata": "Q5080418", "start_date": "1633"},
}

WIKIDATA = {"entities": {
    "Q12110695": {"descriptions": {"en": {
        "value": "art museum of the Courtauld Institute of Art, located in London"}}},
}}


class FakeNet:
    """Stands in for `App._fetch`. Each service can be told to fail."""

    def __init__(self, *, geocode=GEOCODE, overpass=None, overpass_fail=0,
                 nominatim_places=None, wikidata=WIKIDATA):
        self.geocode = geocode
        self.overpass = overpass if overpass is not None else [COURTAULD, MURAL]
        self.overpass_fail = overpass_fail      # how many Overpass servers fail
        self.nominatim_places = nominatim_places
        self.wikidata = wikidata
        self.urls: list[str] = []

    def __call__(self, url, timeout=6.0, data=None):
        assert timeout > 0
        self.urls.append(url)
        if "overpass" in url:
            if self.overpass_fail:
                self.overpass_fail -= 1
                raise TimeoutError("timed out")
            assert b"data=" in data
            return json.dumps({"elements": self.overpass}).encode()
        if "nominatim" in url:
            query = parse_qs(urlparse(url).query)
            if "extratags" in query:
                result = self.nominatim_places
            else:
                result = self.geocode
            if isinstance(result, Exception):
                raise result
            return json.dumps(result).encode()
        if "wikidata" in url:
            if isinstance(self.wikidata, Exception):
                raise self.wikidata
            return json.dumps(self.wikidata).encode()
        raise AssertionError(f"unexpected url {url}")


def make_app(net: FakeNet) -> App:
    app = App()
    app._fetch = net
    app._sleep = lambda seconds: None
    app._clock = lambda: 0.0
    return app


def call(inputs: dict, net: FakeNet | None = None, *, explicit: bool = False):
    return LocalRunner(make_app(net or FakeNet())).call(
        "places.walk", inputs, granted_secrets={}, now=NOW, request_id="test-1",
        explicit=explicit,
    ).result


def rows(table: str) -> list[list[str]]:
    """The body rows of the markdown table, split into cells."""
    lines = table.splitlines()[2:]
    return [[c.strip() for c in line.strip("|").split(" | ")] for line in lines]


# ── The manifest ────────────────────────────────────────────────────────────


def test_the_manifest_is_valid():
    app = App()
    assert app.manifest.app_id == "hack.app_da9d"
    assert [o.id for o in app.manifest.outcomes] == ["places.walk"]


def test_the_summary_never_says_by():
    """The badge splits at the first "by", so the summary must not have one."""
    import re
    assert not re.search(r"\bby\b", App().manifest.summary, re.IGNORECASE)


# ── The answer ──────────────────────────────────────────────────────────────


def test_it_returns_a_table_of_matching_places():
    result = call({"location": "Covent Garden", "interest": "art"})
    assert result.data["location_found"] == "Covent Garden"
    assert result.data["interest"] == "art"
    assert result.data["radius_m"] == 1000
    assert result.data["count"] == 2

    table = result.data["table"]
    assert table.splitlines()[0] == "| Name | What it is | Distance | Opening hours |"
    body = rows(table)
    # Closest first: the mural is about 90 m away, the Courtauld about 470 m.
    assert [r[0] for r in body] == ["Seven Dials Mural", "The Courtauld Gallery"]
    assert "Covent Garden" in result.narrative


def test_opening_hours_are_shown_exactly_as_written():
    body = rows(call({"location": "Covent Garden", "interest": "art"}).data["table"])
    courtauld = next(r for r in body if r[0] == "The Courtauld Gallery")
    assert courtauld[3] == "Mo-Su 10:00-18:00"


def test_a_place_without_hours_says_hours_not_listed():
    body = rows(call({"location": "Covent Garden", "interest": "art"}).data["table"])
    mural = next(r for r in body if r[0] == "Seven Dials Mural")
    assert mural[3] == NO_HOURS == "hours not listed"


def test_a_pipe_in_the_hours_does_not_break_the_table():
    net = FakeNet(overpass=[GALLERY_WITH_ODD_HOURS])
    table = call({"location": "Covent Garden", "interest": "art"}, net).data["table"]
    assert "Tu-Sa 12:00-17:00 \\| by appointment" in table
    assert len(table.splitlines()) == 3


def test_descriptions_come_from_wikidata_then_the_tags():
    body = rows(call({"location": "Covent Garden", "interest": "art"}).data["table"])
    by_name = {r[0]: r[1] for r in body}
    assert by_name["The Courtauld Gallery"] == "Art museum of the Courtauld Institute of Art"
    assert by_name["Seven Dials Mural"] == "Mural by A. Painter"


def test_if_wikidata_is_down_the_tags_still_describe_it():
    net = FakeNet(wikidata=TimeoutError("slow"))
    body = rows(call({"location": "Covent Garden", "interest": "art"}, net).data["table"])
    assert {r[0]: r[1] for r in body}["The Courtauld Gallery"] == "Art gallery"


def test_distance_shows_metres_and_a_walking_time():
    body = rows(call({"location": "Covent Garden", "interest": "art"}).data["table"])
    courtauld = next(r for r in body if r[0] == "The Courtauld Gallery")
    assert courtauld[2] == "470 m (~8 min walk)"


def test_blue_plaques_are_left_out_of_history():
    net = FakeNet(overpass=[PLAQUE, STATUE])
    result = call({"location": "Covent Garden", "interest": "history"}, net)
    names = [r[0] for r in rows(result.data["table"])]
    assert names == ["Statue of Charles I"]
    assert rows(result.data["table"])[0][1] == "Memorial, from 1633"


def test_the_same_place_twice_is_listed_once():
    twin = {**COURTAULD, "type": "node", "id": 9}
    net = FakeNet(overpass=[COURTAULD, twin])
    assert call({"location": "Covent Garden", "interest": "art"}, net).data["count"] == 1


def test_nothing_found_is_said_plainly():
    net = FakeNet(overpass=[])
    result = call({"location": "Covent Garden", "interest": "markets"}, net)
    assert result.data["count"] == 0
    assert result.data["table"] == ""
    assert "no markets" in result.narrative


def test_no_more_than_eight_rows():
    many = [{"type": "node", "id": i, "lat": 51.5117 + i / 10000, "lon": -0.1240,
             "tags": {"name": f"Garden {i}", "leisure": "garden"}} for i in range(20)]
    net = FakeNet(overpass=many)
    assert call({"location": "Covent Garden", "interest": "gardens"}, net).data["count"] == 8


# ── When Overpass is at capacity ────────────────────────────────────────────


def test_the_second_overpass_server_is_tried():
    net = FakeNet(overpass_fail=1)
    assert call({"location": "Covent Garden", "interest": "art"}, net).data["count"] == 2
    assert sum("overpass" in u for u in net.urls) == 2


def test_nominatim_is_the_fallback_when_overpass_is_down():
    net = FakeNet(overpass_fail=2, nominatim_places=[{
        "lat": "51.5089", "lon": "-0.1283", "name": "National Gallery",
        "category": "tourism", "type": "gallery",
        "extratags": {"opening_hours": "Sa-Th 10:00-18:00; Fr 10:00-21:00"},
    }, {
        "lat": "51.6000", "lon": "-0.1283", "name": "Too Far Gallery",
        "category": "tourism", "type": "gallery", "extratags": {},
    }])
    result = call({"location": "Covent Garden", "interest": "art"}, net)
    body = rows(result.data["table"])
    assert [r[0] for r in body] == ["National Gallery"]
    assert body[0][3] == "Sa-Th 10:00-18:00; Fr 10:00-21:00"


def test_everything_down_is_a_retryable_error():
    net = FakeNet(overpass_fail=2, nominatim_places=TimeoutError("slow"))
    with pytest.raises(ServiceUnavailableError):
        call({"location": "Covent Garden", "interest": "art"}, net)


# ── The questions it refuses, and what it says ──────────────────────────────


def test_a_place_outside_london_is_named_in_the_message():
    net = FakeNet(geocode=[])
    with pytest.raises(LocationNotFound) as failure:
        call({"location": "Narnia", "interest": "art"}, net)
    assert "Narnia" in failure.value.user_message


def test_no_location_asks_for_one():
    with pytest.raises(LocationMissing):
        call({"location": "", "interest": "art"})
    with pytest.raises(LocationMissing):
        call({"interest": "art"})


def test_no_interest_asks_for_one():
    with pytest.raises(InterestMissing):
        call({"location": "Covent Garden"})


def test_an_unknown_interest_lists_the_ones_it_knows():
    with pytest.raises(InterestUnknown) as failure:
        call({"location": "Covent Garden", "interest": "skateboarding"})
    assert "skateboarding" in failure.value.user_message
    assert "gardens" in failure.value.user_message


def test_nominatim_rate_limit_is_retryable():
    error = urllib.error.HTTPError("u", 429, "Too Many Requests", hdrs={}, fp=None)
    with pytest.raises(RateLimitedError):
        call({"location": "Covent Garden", "interest": "art"}, FakeNet(geocode=error))


def test_nominatim_timeout_is_service_unavailable():
    with pytest.raises(ServiceUnavailableError):
        call({"location": "Covent Garden", "interest": "art"},
             FakeNet(geocode=TimeoutError("slow")))


def test_picked_by_name_it_answers_and_refuses_the_same():
    inputs = {"location": "Covent Garden", "interest": "art"}
    assert call(inputs, explicit=True).data == call(inputs).data
    with pytest.raises(LocationMissing):
        call({"interest": "art"}, explicit=True)


# ── The pieces ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("text, label", [
    ("history", "history"), ("historic buildings", "history"),
    ("gardens", "gardens"), ("botanical gardens", "gardens"), ("parks", "gardens"),
    ("art", "art"), ("street art", "art"), ("art museums", "art"),
    ("History museums", "history"), ("museums", "museums"),
    ("churches", "churches"), ("live music", "theatre and music"),
    ("markets", "markets"), ("wildlife", "nature"),
])
def test_interests_are_recognised(text, label):
    assert match_interest(text).label == label


def test_partial_words_do_not_count():
    assert match_interest("parkour") is None
    assert match_interest("smart") is None


@pytest.mark.parametrize("km, minutes, metres", [
    (None, None, 1000),      # nothing said: a ten-minute-ish default
    (0.5, None, 500),
    (1.6, None, 1600),       # a mile
    (None, 15, 920),         # 15 min * 80 m/min / 1.3 detour
    (0.5, 30, 500),          # a distance beats a time
    (50, None, 3000),        # capped
    (0.01, None, 100),       # floored
    (-1, None, 1000),        # nonsense falls back to the default
])
def test_the_radius(km, minutes, metres):
    assert radius_from_inputs(km, minutes) == metres


def test_walking_time_sets_the_radius_end_to_end():
    assert call({"location": "Covent Garden", "interest": "art",
                 "walk_minutes": 15}).data["radius_m"] == 920


@pytest.mark.parametrize("raw, tidy", [
    ("war memorial in London", "War memorial"),
    ("transport museum in Covent Garden, London, United Kingdom", "Transport museum"),
    ("British clipper ship", "British clipper ship"),
    ("art museum of the Courtauld Institute of Art, located in London",
     "Art museum of the Courtauld Institute of Art"),
])
def test_wikidata_descriptions_are_tidied(raw, tidy):
    assert tidy_wikidata(raw) == tidy


def test_every_request_has_a_timeout_and_the_user_agent():
    """Nominatim refuses requests with no User-Agent."""
    assert "london-wander" in app_module.USER_AGENT


def test_the_runner_enforces_the_manifest(monkeypatch):
    from pippa_agentapp_sdk import Result

    monkeypatch.setattr(App, "handle", lambda self, c: Result(data={"surprise": "hello"}))
    with pytest.raises(OutputShapeViolation):
        call({"location": "Covent Garden", "interest": "art"})
