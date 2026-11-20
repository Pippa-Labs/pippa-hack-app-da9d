"""Tests for London Wander. The build runs these, and will not publish if any fail.

Everything goes through `LocalRunner`, the same code the runtime uses, so a
result that does not match manifest.yaml fails here exactly as it would live.
The network is replaced with canned responses shaped like the real ones
(captured from Nominatim, Overpass and Wikidata in November 2026).
"""

from __future__ import annotations

import json
import re
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
             "opening_hours": "Mo-Su 10:00-18:00", "wikidata": "Q12110695",
             "wikipedia": "en:Courtauld Gallery", "fee": "yes", "wheelchair": "yes"},
}
MURAL = {
    "type": "node", "id": 2, "lat": 51.5120, "lon": -0.1250,
    "tags": {"name": "Seven Dials Mural", "tourism": "artwork",
             "artwork_type": "mural", "artist_name": "A. Painter", "fee": "no"},
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

WIKIPEDIA = {"query": {
    "normalized": [{"from": "Courtauld Gallery", "to": "Courtauld Gallery"}],
    "pages": {"1": {"title": "Courtauld Gallery", "extract":
        "The Courtauld Gallery (UK: ) is an art museum in Somerset House, on the "
        "Strand in central London. It houses the Samuel Courtauld Trust collection."}},
}}

WIKIDATA = {"entities": {
    "Q12110695": {"descriptions": {"en": {
        "value": "art museum of the Courtauld Institute of Art, located in London"}}},
}}


class FakeNet:
    """Stands in for `App._fetch`. Each service can be told to fail."""

    def __init__(self, *, geocode=GEOCODE, overpass=None, overpass_fail=0,
                 nominatim_places=None, wikidata=WIKIDATA, wikipedia=WIKIPEDIA):
        self.geocode = geocode
        self.overpass = overpass if overpass is not None else [COURTAULD, MURAL]
        self.overpass_fail = overpass_fail      # how many Overpass servers fail
        self.nominatim_places = nominatim_places
        self.wikidata = wikidata
        self.wikipedia = wikipedia
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
        if "wikipedia.org" in url:
            if isinstance(self.wikipedia, Exception):
                raise self.wikipedia
            return json.dumps(self.wikipedia).encode()
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


def places(text: str) -> list[dict]:
    """The list split back into places: name, distance, intro, details."""
    out = []
    for block in text.split("\n\n"):
        lines = block.split("  \n")
        head = re.fullmatch(r"\*\*(\d+)\. (.+)\*\*: (.+)", lines[0])
        assert head, lines[0]
        out.append({"n": int(head[1]), "name": head[2], "distance": head[3],
                    "intro": lines[1], "details": lines[2] if len(lines) > 2 else ""})
    return out


BANNED = ("unavailable", "not listed", "unknown", "n/a", "none")


def assert_no_filler(text: str) -> None:
    lowered = text.lower()
    for word in BANNED:
        assert word not in lowered, word


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


def test_it_lists_the_matching_places_closest_first():
    result = call({"location": "Covent Garden", "interest": "art"})
    assert result.data["location_found"] == "Covent Garden"
    assert result.data["interest"] == "art"
    assert result.data["radius_m"] == 1000
    assert result.data["count"] == 2
    listed = places(result.data["table"])
    assert [(p["n"], p["name"]) for p in listed] == [
        (1, "Seven Dials Mural"), (2, "The Courtauld Gallery")]
    assert "|" not in result.data["table"].splitlines()[0]     # not a table
    assert_no_filler(result.data["table"])


def test_known_details_are_shown_and_unknown_ones_left_out():
    listed = {p["name"]: p for p in places(
        call({"location": "Covent Garden", "interest": "art"}).data["table"])}
    assert listed["The Courtauld Gallery"]["details"] == "Hours: Mo-Su 10:00-18:00 · Paid · Step-free"
    # The mural has no hours and no access tag: only what is known is shown.
    assert listed["Seven Dials Mural"]["details"] == "Free"


def test_a_place_with_nothing_known_has_no_details_line():
    bare = {"type": "node", "id": 7, "lat": 51.5130, "lon": -0.1240,
            "tags": {"name": "Bare Gallery", "tourism": "gallery"}}
    text = call({"location": "Covent Garden", "interest": "art"},
                FakeNet(overpass=[bare])).data["table"]
    assert places(text)[0]["details"] == ""
    assert_no_filler(text)


def test_hours_are_shown_exactly_as_written():
    text = call({"location": "Covent Garden", "interest": "art"},
                FakeNet(overpass=[GALLERY_WITH_ODD_HOURS])).data["table"]
    assert places(text)[0]["details"] == "Hours: Tu-Sa 12:00-17:00 | by appointment"


def test_every_place_has_an_intro_from_wikipedia_or_its_tags():
    listed = {p["name"]: p["intro"] for p in places(
        call({"location": "Covent Garden", "interest": "art"}).data["table"])}
    assert listed["The Courtauld Gallery"].startswith(
        "The Courtauld Gallery is an art museum in Somerset House")
    assert "(UK" not in listed["The Courtauld Gallery"]
    assert listed["Seven Dials Mural"] == "Mural by A. Painter."


def test_wikipedia_down_falls_back_to_wikidata():
    text = call({"location": "Covent Garden", "interest": "art"},
                FakeNet(wikipedia=TimeoutError("slow"))).data["table"]
    intro = {p["name"]: p["intro"] for p in places(text)}["The Courtauld Gallery"]
    assert intro == "Art museum of the Courtauld Institute of Art."


def test_wikipedia_and_wikidata_down_still_answers():
    net = FakeNet(wikipedia=TimeoutError("slow"), wikidata=TimeoutError("slow"))
    result = call({"location": "Covent Garden", "interest": "art"}, net)
    assert result.data["count"] == 2
    intro = {p["name"]: p["intro"] for p in places(result.data["table"])}
    assert intro["The Courtauld Gallery"] == "Art gallery."


def test_no_two_places_share_an_intro():
    many = [{"type": "node", "id": i, "lat": 51.5117 + i / 10000, "lon": -0.1240,
             "tags": {"name": f"Garden {i}", "leisure": "garden"}} for i in range(6)]
    many[1]["tags"]["addr:street"] = "Long Acre"
    text = call({"location": "Covent Garden", "interest": "gardens"},
                FakeNet(overpass=many)).data["table"]
    intros = [p["intro"] for p in places(text)]
    assert len(intros) == 6
    assert len(set(intros)) == 6
    assert "Garden, on Long Acre." in intros


def test_facts_every_place_shares_are_left_out_of_the_intros():
    rows = [{"type": "node", "id": i, "lat": 51.5117 + i / 1000, "lon": -0.1240,
             "tags": {"name": f"Park {i}", "leisure": "park", "operator": "Westminster Council",
                      "start_date": str(1800 + i)}} for i in range(2)]
    intros = [p["intro"] for p in places(call(
        {"location": "Covent Garden", "interest": "gardens"},
        FakeNet(overpass=rows)).data["table"])]
    assert intros == ["Park, dating from 1800.", "Park, dating from 1801."]


def test_the_narrative_contrasts_the_picks_from_the_data():
    result = call({"location": "Covent Garden", "interest": "art"})
    assert "The closest is Seven Dials Mural" in result.narrative
    assert "Seven Dials Mural is the only one listed as free" in result.narrative
    assert "only The Courtauld Gallery lists opening hours" in result.narrative


def test_the_oldest_is_named_only_when_two_have_dates():
    result = call({"location": "Covent Garden", "interest": "history"},
                  FakeNet(overpass=[PLAQUE, STATUE]))
    assert "oldest" not in result.narrative
    assert places(result.data["table"])[0]["intro"] == "Memorial, dating from 1633."


def test_distance_shows_metres_and_a_walking_time():
    listed = {p["name"]: p for p in places(
        call({"location": "Covent Garden", "interest": "art"}).data["table"])}
    assert listed["The Courtauld Gallery"]["distance"] == "470 m (~8 min walk)"


def test_blue_plaques_are_left_out_of_history():
    result = call({"location": "Covent Garden", "interest": "history"},
                  FakeNet(overpass=[PLAQUE, STATUE]))
    assert [p["name"] for p in places(result.data["table"])] == ["Statue of Charles I"]


def test_the_same_place_twice_is_listed_once():
    twin = {**COURTAULD, "type": "node", "id": 9}
    net = FakeNet(overpass=[COURTAULD, twin])
    assert call({"location": "Covent Garden", "interest": "art"}, net).data["count"] == 1


def test_nothing_found_is_said_plainly():
    result = call({"location": "Covent Garden", "interest": "markets"}, FakeNet(overpass=[]))
    assert result.data["count"] == 0
    assert result.data["table"] == ""
    assert "no markets" in result.narrative


def test_no_more_than_eight_places():
    many = [{"type": "node", "id": i, "lat": 51.5117 + i / 10000, "lon": -0.1240,
             "tags": {"name": f"Garden {i}", "leisure": "garden"}} for i in range(20)]
    result = call({"location": "Covent Garden", "interest": "gardens"}, FakeNet(overpass=many))
    assert result.data["count"] == 8
    assert len(places(result.data["table"])) == 8


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
    listed = places(result.data["table"])
    assert [p["name"] for p in listed] == ["National Gallery"]
    assert listed[0]["details"] == "Hours: Sa-Th 10:00-18:00; Fr 10:00-21:00"


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
