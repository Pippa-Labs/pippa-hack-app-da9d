"""London Wander: places in London that match an interest, within a walk.

Given a starting point in London, an interest (history, gardens, art, ...)
and how far the person is happy to walk, it looks up matching places in
OpenStreetMap and returns a short table: name, a one-line description, the
distance, and the opening hours exactly as OpenStreetMap writes them ("hours
not listed" when there are none).

Where the data comes from, in order:

1. Nominatim turns the location into coordinates, inside a box around
   Greater London only.
2. Overpass finds the matching places around that point. The public Overpass
   servers are often at capacity, so two are tried in turn.
3. If neither Overpass server answers, Nominatim's own category search (for
   example "museum" inside a box around the point) is the fallback. It finds
   fewer places, but it finds them reliably.
4. Wikidata supplies a clean one-line description for the notable places, in
   one request. If it fails, descriptions are built from the OpenStreetMap tags.

All of it shares one time budget, well inside Pippa's 20 seconds.
"""

from __future__ import annotations

import json
import math
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlencode

from pippa_agentapp_sdk import (
    AgentAppError,
    Call,
    Manifest,
    RateLimitedError,
    Result,
    ServiceUnavailableError,
    load_manifest_file,
    validate_manifest,
)

MANIFEST_PATH = Path(__file__).with_name("manifest.yaml")

#: Nominatim, Overpass and Wikidata all ask for a descriptive User-Agent, and
#: Nominatim and Wikimedia answer 403 without one.
USER_AGENT = "pippa-agent-app-london-wander/0.1 (https://github.com/Pippa-Labs)"

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
OVERPASS_URLS = (
    "https://overpass.private.coffee/api/interpreter",
    "https://overpass-api.de/api/interpreter",
)
WIKIDATA_URL = "https://www.wikidata.org/w/api.php"
WIKIPEDIA_URL = "https://en.wikipedia.org/w/api.php"

#: Greater London, as Nominatim's viewbox: west, north, east, south.
LONDON_VIEWBOX = "-0.5103,51.6919,0.3340,51.2868"

#: The whole call must finish inside this many seconds. Pippa allows 20.
BUDGET_SECONDS = 16.0
#: Overpass must give up this long before the end, so that the Nominatim
#: fallback (two searches a second apart) and Wikidata still fit.
RESERVE_AFTER_OVERPASS = 7.0

DEFAULT_RADIUS_M = 1000
MIN_RADIUS_M = 100
MAX_RADIUS_M = 3000
#: A steady walking pace, in metres per minute (about 3 mph).
WALK_M_PER_MIN = 80
#: Streets are not straight lines. A walk is usually about this much longer.
DETOUR_FACTOR = 1.3

MAX_ROWS = 8

#: Memorial types that are too small or too many to be worth walking to.
SKIP_MEMORIALS = {"plaque", "blue_plaque", "bench", "stolperstein", "stone"}


@dataclass(frozen=True)
class Rule:
    """One kind of OpenStreetMap feature that counts for an interest."""

    key: str
    values: tuple[str, ...]
    #: Extra tags that must also match, e.g. museum=art for an art museum.
    also: tuple[tuple[str, str], ...] = ()

    def matches(self, tags: dict) -> bool:
        if tags.get(self.key) not in self.values:
            return False
        return all(tags.get(k) == v for k, v in self.also)

    def overpass(self) -> str:
        selector = f'["{self.key}"~"^({"|".join(self.values)})$"]'
        for k, v in self.also:
            selector += f'["{k}"="{v}"]'
        return selector


@dataclass(frozen=True)
class Interest:
    label: str
    #: How the narrative names the places, e.g. "Gardens and parks".
    title: str
    words: tuple[str, ...]
    rules: tuple[Rule, ...]
    #: Nominatim category phrases, used only when Overpass is unavailable.
    fallback: tuple[str, ...]


HISTORIC = (
    "monument", "memorial", "castle", "ruins", "archaeological_site",
    "building", "church", "palace", "fort", "manor", "city_gate", "ship",
    "tomb", "wayside_cross", "battlefield", "house", "citywalls",
)

#: Checked in this order, so "art museums" is art and "history museums" is
#: history, before the plain museums entry sees either.
INTERESTS: tuple[Interest, ...] = (
    Interest(
        "art", "Art galleries and artworks",
        ("art", "arts", "artwork", "artworks", "gallery", "galleries",
         "sculpture", "sculptures", "statue", "statues", "mural", "murals",
         "painting", "paintings", "street art"),
        (Rule("tourism", ("gallery", "artwork")),
         Rule("amenity", ("arts_centre", "art_gallery")),
         Rule("tourism", ("museum",), (("museum", "art"),))),
        ("gallery", "artwork"),
    ),
    Interest(
        "history", "Historic places and museums",
        ("history", "historic", "historical", "heritage", "monument",
         "monuments", "memorial", "memorials", "castle", "castles", "ruins",
         "roman", "medieval", "old buildings"),
        (Rule("historic", HISTORIC), Rule("tourism", ("museum",))),
        ("museum", "monument"),
    ),
    Interest(
        "gardens", "Gardens and parks",
        ("garden", "gardens", "park", "parks", "green space", "green spaces",
         "greenery", "flowers", "botanical", "botanic"),
        (Rule("leisure", ("garden", "park")),),
        ("garden", "park"),
    ),
    Interest(
        "museums", "Museums",
        ("museum", "museums", "exhibition", "exhibitions"),
        (Rule("tourism", ("museum",)),),
        ("museum",),
    ),
    Interest(
        "churches", "Churches and places of worship",
        ("church", "churches", "cathedral", "cathedrals", "chapel", "chapels",
         "religious", "worship", "mosque", "mosques", "temple", "temples",
         "synagogue", "synagogues", "abbey"),
        (Rule("amenity", ("place_of_worship",)),),
        ("church",),
    ),
    Interest(
        "theatre and music", "Theatres and music venues",
        ("theatre", "theatres", "theater", "theaters", "music", "concert",
         "concerts", "shows", "opera", "cinema", "cinemas", "gigs", "live music"),
        (Rule("amenity", ("theatre", "cinema", "music_venue", "concert_hall")),),
        ("theatre", "cinema"),
    ),
    Interest(
        "markets", "Markets",
        ("market", "markets", "street food", "food market"),
        (Rule("amenity", ("marketplace",)),),
        ("market",),
    ),
    Interest(
        "nature", "Nature spots",
        ("nature", "wildlife", "woodland", "woods", "nature reserve",
         "nature reserves", "birds", "birdwatching", "wetlands"),
        (Rule("leisure", ("nature_reserve",)), Rule("natural", ("wood",))),
        ("nature reserve",),
    ),
)

SUPPORTED = "history, gardens, art, museums, churches, theatre and music, markets or nature"

# ── Errors about the question itself ────────────────────────────────────────


class LocationMissing(AgentAppError):
    user_message = (
        "Tell me where in London to start from: a street, a station, "
        "a neighbourhood or a postcode."
    )
    retryable = False


class LocationNotFound(AgentAppError):
    user_message = "I could not find that place in London."
    retryable = False


class InterestMissing(AgentAppError):
    user_message = f"Tell me what you are interested in: {SUPPORTED}."
    retryable = False


class InterestUnknown(AgentAppError):
    user_message = f"I can look for {SUPPORTED}."
    retryable = False


# ── The app ─────────────────────────────────────────────────────────────────


class App:
    def __init__(self) -> None:
        self._manifest = validate_manifest(load_manifest_file(MANIFEST_PATH))
        # Seams for the tests: no network, no real waiting, a fake clock.
        self._sleep = time.sleep
        self._clock = time.monotonic

    @property
    def manifest(self) -> Manifest:
        return self._manifest

    def _fetch(self, url: str, timeout: float = 6.0, data: bytes | None = None) -> bytes:
        """Every outbound request goes through here."""
        request = urllib.request.Request(url, data=data, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read()

    # ── handle ──────────────────────────────────────────────────────────

    def handle(self, call: Call) -> Result:
        if call.outcome_id != "places.walk":
            raise AgentAppError(f"this app cannot do {call.outcome_id!r}")

        deadline = self._clock() + BUDGET_SECONDS
        location = (call.inputs.get("location") or "").strip()
        interest_text = (call.inputs.get("interest") or "").strip()

        if not location:
            raise LocationMissing("no location in the message")
        if not interest_text:
            raise InterestMissing("no interest in the message")
        interest = match_interest(interest_text)
        if interest is None:
            error = InterestUnknown(f"unknown interest {interest_text!r}")
            error.user_message = (
                f"I don't know how to look for {interest_text!r}. "
                f"I can look for {SUPPORTED}."
            )
            raise error

        radius = radius_from_inputs(call.inputs.get("distance_km"),
                                    call.inputs.get("walk_minutes"))

        lat, lon, place_name = self._geocode(location, deadline)
        places = self._find_places(interest, lat, lon, radius, deadline)
        rows = choose_rows(places)
        intros = self._wikipedia_intros(rows, deadline)
        missing = [r for r in rows if r["wikidata"] and r["wikipedia"] not in intros]
        descriptions = self._wikidata_descriptions(missing, deadline)
        for place in rows:
            place["intro"] = (intros.get(place["wikipedia"])
                              or tag_intro(place, rows, descriptions.get(place["wikidata"])))
        make_intros_unique(rows)

        if rows:
            table = make_list(rows)
            narrative = (
                f"{interest.title} within {format_metres(radius)} of "
                f"{place_name}, from OpenStreetMap, closest first (distances "
                f"are straight-line, with a rough walking time). "
                + contrast(rows)
            ).strip()
        else:
            table = ""
            narrative = (
                f"OpenStreetMap lists no {interest.title.lower()} within "
                f"{format_metres(radius)} of {place_name}. A longer walk may find some."
            )

        return Result(
            data={
                "location_found": place_name,
                "interest": interest.label,
                "radius_m": radius,
                "count": len(rows),
                "table": table,
            },
            narrative=narrative,
        )

    # ── the three services ──────────────────────────────────────────────

    def _remaining(self, deadline: float) -> float:
        return deadline - self._clock()

    def _geocode(self, location: str, deadline: float) -> tuple[float, float, str]:
        query = urlencode({
            "q": location, "format": "jsonv2", "limit": 1, "countrycodes": "gb",
            "viewbox": LONDON_VIEWBOX, "bounded": 1,
        })
        try:
            raw = self._fetch(f"{NOMINATIM_URL}?{query}",
                              timeout=min(5.0, self._remaining(deadline)))
            results = json.loads(raw)
        except urllib.error.HTTPError as exc:          # before URLError: it IS one
            if exc.code == 429:
                raise RateLimitedError(f"nominatim rate limited: {exc.code}")
            raise ServiceUnavailableError(f"nominatim returned {exc.code}")
        except TimeoutError as exc:
            raise ServiceUnavailableError(f"nominatim timed out: {exc}")
        except (urllib.error.URLError, OSError) as exc:
            raise ServiceUnavailableError(f"could not reach nominatim: {exc}")
        except ValueError as exc:
            raise ServiceUnavailableError(f"nominatim sent something unreadable: {exc}")

        if not isinstance(results, list) or not results:
            error = LocationNotFound(f"nominatim found nothing for {location!r}")
            error.user_message = (
                f"I could not find {location} in London. Try a street, "
                f"a station or a postcode."
            )
            raise error
        first = results[0]
        name = (first.get("name") or "").strip()
        if not name:
            name = ", ".join(first.get("display_name", location).split(", ")[:2])
        return float(first["lat"]), float(first["lon"]), name

    def _find_places(self, interest: Interest, lat: float, lon: float,
                     radius: int, deadline: float) -> list[dict]:
        places = self._overpass(interest, lat, lon, radius, deadline)
        if places is None:
            places = self._nominatim_places(interest, lat, lon, radius, deadline)
        if places is None:
            raise ServiceUnavailableError("overpass and nominatim both failed")
        return places

    def _overpass(self, interest: Interest, lat: float, lon: float,
                  radius: int, deadline: float) -> list[dict] | None:
        """Matching places from Overpass, or None if no server answered."""
        around = f"(around:{radius},{lat:.6f},{lon:.6f})"
        skip = "|".join(sorted(SKIP_MEMORIALS))
        parts = "".join(
            f'nwr{around}{rule.overpass()}["name"]["memorial"!~"^({skip})$"]'
            f'["access"!~"^(private|no)$"];'
            for rule in interest.rules
        )
        query = f"[out:json][timeout:5];({parts});out center tags 200;"
        body = urlencode({"data": query}).encode()

        for url in OVERPASS_URLS:
            # A healthy server answers in about a second. One at capacity
            # hangs, so neither may eat the time the fallback needs.
            timeout = min(5.0, self._remaining(deadline) - RESERVE_AFTER_OVERPASS)
            if timeout < 1.5:
                break
            try:
                payload = json.loads(self._fetch(url, timeout=timeout, data=body))
            except (urllib.error.URLError, TimeoutError, OSError, ValueError):
                continue        # at capacity, down or slow: try the next one
            elements = payload.get("elements") if isinstance(payload, dict) else None
            if not isinstance(elements, list):
                continue
            if not elements and "runtime error" in str(payload.get("remark", "")):
                continue        # a 200 that is really a timeout on their side
            places = []
            for element in elements:
                tags = element.get("tags") or {}
                point = element.get("center") or element
                if "lat" not in point or "lon" not in point:
                    continue
                place = make_place(interest, tags, float(point["lat"]),
                                   float(point["lon"]), lat, lon)
                if place:
                    places.append(place)
            return places
        return None

    def _nominatim_places(self, interest: Interest, lat: float, lon: float,
                          radius: int, deadline: float) -> list[dict] | None:
        """The fallback: Nominatim's category search in a box around the point."""
        dlat = radius / 111_320
        dlon = radius / (111_320 * math.cos(math.radians(lat)))
        viewbox = f"{lon - dlon:.6f},{lat + dlat:.6f},{lon + dlon:.6f},{lat - dlat:.6f}"
        places: list[dict] = []
        answered = False
        for phrase in interest.fallback:
            if self._remaining(deadline) < 3.0:
                break
            # Nominatim asks for no more than one request a second, and the
            # geocode (or the previous phrase) has only just gone.
            self._sleep(1.0)
            timeout = min(3.0, self._remaining(deadline) - 1.5)
            if timeout < 1.0:
                break
            query = urlencode({
                "q": phrase, "format": "jsonv2", "limit": 40, "extratags": 1,
                "viewbox": viewbox, "bounded": 1,
            })
            try:
                results = json.loads(self._fetch(f"{NOMINATIM_URL}?{query}",
                                                 timeout=timeout))
            except urllib.error.HTTPError as exc:
                if exc.code == 429:
                    raise RateLimitedError(f"nominatim rate limited: {exc.code}")
                continue
            except (urllib.error.URLError, TimeoutError, OSError, ValueError):
                continue
            if not isinstance(results, list):
                continue
            answered = True
            for result in results:
                tags = dict(result.get("extratags") or {})
                tags.setdefault("name", result.get("name") or "")
                tags.setdefault(result.get("category", ""), result.get("type", ""))
                place = make_place(interest, tags, float(result["lat"]),
                                   float(result["lon"]), lat, lon)
                if place and place["distance"] <= radius * 1.1:
                    places.append(place)
        return places if answered else None

    def _wikipedia_intros(self, rows: list[dict], deadline: float) -> dict[str, str]:
        """The first sentence or two of each English Wikipedia article, in
        one batched request, keyed by the title in the OSM tag. Nothing if
        Wikipedia is slow or down: Wikidata and the tags take over."""
        titles = sorted({row["wikipedia"] for row in rows if row["wikipedia"]})
        timeout = min(3.0, self._remaining(deadline) - 2.0)
        if not titles or timeout < 1.0:
            return {}
        query = urlencode({
            "action": "query", "prop": "extracts", "exintro": 1, "explaintext": 1,
            "exsentences": 2, "redirects": 1, "format": "json",
            "titles": "|".join(titles),
        })
        try:
            payload = json.loads(self._fetch(f"{WIKIPEDIA_URL}?{query}", timeout=timeout))
            result = payload["query"]
        except (urllib.error.URLError, TimeoutError, OSError, ValueError,
                KeyError, TypeError):
            return {}
        # Follow the title back through Wikipedia's normalising and redirects.
        renamed = {}
        for step in (result.get("normalized") or []) + (result.get("redirects") or []):
            renamed[step.get("from")] = step.get("to")
        extracts = {}
        for page in (result.get("pages") or {}).values():
            text = tidy_extract(page.get("extract") or "")
            if text:
                extracts[page.get("title")] = text
        out = {}
        for title in titles:
            final = title
            for _ in range(3):
                final = renamed.get(final, final)
            if final in extracts:
                out[title] = extracts[final]
        return out

    def _wikidata_descriptions(self, rows: list[dict], deadline: float) -> dict[str, str]:
        """Wikidata's one-line descriptions, or nothing if it is slow or down."""
        ids = sorted({row["wikidata"] for row in rows if row["wikidata"]})
        timeout = min(3.0, self._remaining(deadline) - 0.5)
        if not ids or timeout < 1.0:
            return {}
        query = urlencode({
            "action": "wbgetentities", "ids": "|".join(ids),
            "props": "descriptions", "languages": "en", "format": "json",
        })
        try:
            payload = json.loads(self._fetch(f"{WIKIDATA_URL}?{query}", timeout=timeout))
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            return {}
        out = {}
        entities = payload.get("entities") if isinstance(payload, dict) else None
        for qid, entity in (entities or {}).items():
            try:
                text = entity["descriptions"]["en"]["value"]
            except (KeyError, TypeError):
                continue
            text = tidy_wikidata(text)
            if text:
                out[qid] = text
        return out


# ── Pure helpers, kept outside the class so they are easy to test ──────────


def match_interest(text: str) -> Interest | None:
    lowered = text.lower()
    for interest in INTERESTS:
        for word in interest.words:
            if re.search(rf"\b{re.escape(word)}\b", lowered):
                return interest
    return None


def radius_from_inputs(distance_km, walk_minutes) -> int:
    """The search radius in metres, from whichever the person gave."""
    metres = None
    if isinstance(distance_km, (int, float)) and distance_km > 0:
        metres = distance_km * 1000
    elif isinstance(walk_minutes, (int, float)) and walk_minutes > 0:
        # A walk of N minutes covers less ground as the crow flies.
        metres = walk_minutes * WALK_M_PER_MIN / DETOUR_FACTOR
    if metres is None:
        return DEFAULT_RADIUS_M
    return int(round(max(MIN_RADIUS_M, min(MAX_RADIUS_M, metres)), -1))


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6_371_000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def make_place(interest: Interest, tags: dict, lat: float, lon: float,
               from_lat: float, from_lon: float) -> dict | None:
    """One table-ready place, or None if it does not belong in the answer."""
    name = (tags.get("name") or "").strip()
    if not name:
        return None
    if not any(rule.matches(tags) for rule in interest.rules):
        return None
    if tags.get("memorial") in SKIP_MEMORIALS or tags.get("access") in ("private", "no"):
        return None
    wikidata = tags.get("wikidata", "")
    wikipedia = tags.get("wikipedia", "")
    wikipedia = wikipedia[3:].strip() if wikipedia.startswith("en:") else ""
    year = re.match(r"\d{4}", tags.get("start_date", ""))
    return {
        "tags": tags,
        "wikipedia": wikipedia,
        "year": int(year.group()) if year else None,
        "fee": {"yes": "Paid", "no": "Free"}.get(tags.get("fee", "")),
        "step_free": {"yes": "Step-free", "limited": "Partly step-free"}.get(
            tags.get("wheelchair", "")),
        "name": name,
        "kind": describe_kind(tags),
        "description": osm_description(tags),
        "hours": " ".join((tags.get("opening_hours") or "").split()),
        "distance": haversine_m(from_lat, from_lon, lat, lon),
        "wikidata": wikidata if re.fullmatch(r"Q\d+", wikidata) else "",
        "notable": bool(wikidata or tags.get("wikipedia")),
    }


def describe_kind(tags: dict) -> str:
    """A short description built from the tags, when nothing better exists."""
    tourism, amenity, leisure = tags.get("tourism"), tags.get("amenity"), tags.get("leisure")
    if tourism == "artwork":
        kind = (tags.get("artwork_type") or "artwork").replace("_", " ").capitalize()
        artist = tags.get("artist_name") or tags.get("artist")
        text = f"{kind} by {artist}" if artist else f"Public {kind.lower()}"
    elif tourism == "museum":
        text = "Art museum" if tags.get("museum") == "art" else "Museum"
    elif tourism == "gallery" or amenity == "art_gallery":
        text = "Art gallery"
    elif amenity == "arts_centre":
        text = "Arts centre"
    elif amenity == "place_of_worship":
        religion = (tags.get("religion") or "").replace("_", " ")
        text = "Church" if religion == "christian" else (
            f"{religion.capitalize()} place of worship" if religion else "Place of worship")
    elif amenity == "marketplace":
        text = "Market"
    elif amenity in ("theatre", "cinema", "music_venue", "concert_hall"):
        text = amenity.replace("_", " ").capitalize()
    elif leisure == "nature_reserve":
        text = "Nature reserve"
    elif tags.get("natural") == "wood":
        text = "Woodland"
    elif leisure in ("garden", "park"):
        garden = tags.get("garden:type")
        text = f"{garden.capitalize()} garden" if leisure == "garden" and garden else leisure.capitalize()
    elif tags.get("historic"):
        text = "Historic " + tags["historic"].replace("_", " ")
        if tags["historic"] in ("memorial", "monument"):
            text = tags["historic"].capitalize()
    else:
        text = "Place of interest"
    return text


def osm_description(tags: dict) -> str:
    text = " ".join((tags.get("description") or "").split())
    return shorten(text, 90) if text else ""


def tidy_wikidata(text: str) -> str:
    """'war memorial in London, England' -> 'War memorial'."""
    text = " ".join(text.split())
    # Cut from the first " in ..." (or ", located in ...") that names London
    # or the country: the person already knows where they are.
    text = re.sub(r",?\s+(?:located\s+|situated\s+)?in\s+.*"
                  r"(?:London|England|United Kingdom|\bUK\b).*$", "", text)
    text = re.sub(r",\s*(?:London|England|United Kingdom|UK)$", "", text)
    text = shorten(text, 90)
    return text[:1].upper() + text[1:]


def shorten(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0].rstrip(",;:.") + "…"


def choose_rows(places: list[dict]) -> list[dict]:
    """The few places worth a row: notable ones first, then closest."""
    best: dict[str, dict] = {}
    for place in places:                      # one row per name, the closest
        key = place["name"].casefold()
        if key not in best or place["distance"] < best[key]["distance"]:
            best[key] = place
    picked = sorted(best.values(), key=lambda p: (not p["notable"], p["distance"]))[:MAX_ROWS]
    return sorted(picked, key=lambda p: p["distance"])


def format_metres(metres: float) -> str:
    if metres < 1000:
        return f"{int(round(metres, -1))} m"
    return f"{metres / 1000:.1f} km"


def format_distance(metres: float) -> str:
    minutes = max(1, round(metres * DETOUR_FACTOR / WALK_M_PER_MIN))
    return f"{format_metres(metres)} (~{minutes} min walk)"


def tidy_extract(text: str) -> str:
    """A Wikipedia intro, trimmed to one or two clean sentences."""
    text = re.sub(r"\s*\([^()]*\)", "", " ".join(text.split()))   # (pronunciation)
    text = re.sub(r"\s+([,.;])", r"\1", text)
    return shorten(text, 240)


def tag_facts(place: dict) -> list[str]:
    """Distinguishing facts from the OSM tags, as short phrases."""
    tags = place["tags"]
    facts = []
    if place["year"]:
        facts.append(f"dating from {place['year']}")
    if tags.get("architect"):
        facts.append(f"designed by {tags['architect']}")
    listed = tags.get("listed_status") or tags.get("heritage:grade")
    if listed:
        facts.append(listed if "listed" in listed.lower() else f"{listed} listed")
    denomination = tags.get("denomination")
    if denomination:
        facts.append(f"{denomination.replace('_', ' ').capitalize()} congregation")
    if tags.get("cuisine"):
        facts.append(f"serving {tags['cuisine'].replace(';', ', ').replace('_', ' ')} food")
    if tags.get("operator"):
        facts.append(f"run by {tags['operator']}")
    if tags.get("inscription") and len(tags["inscription"]) <= 80:
        facts.append(f"inscribed \u201c{tags['inscription']}\u201d")
    return facts


def tag_intro(place: dict, rows: list[dict], wikidata_text: str | None) -> str:
    """One sentence for a place with no Wikipedia intro. Facts every row
    shares (all run by the same council, say) tell nobody anything, so
    they are dropped."""
    base = wikidata_text or place["description"] or place["kind"]
    facts = tag_facts(place)
    if len(rows) > 1:
        shared = set.intersection(*(set(tag_facts(r)) for r in rows))
        facts = [f for f in facts if f not in shared]
    sentence = base.rstrip(".")
    if facts:
        sentence += ", " + ", ".join(facts[:2])
    return sentence[:1].upper() + sentence[1:] + "."


ORDINALS = ("second", "third", "fourth", "fifth", "sixth", "seventh", "eighth")


def make_intros_unique(rows: list[dict]) -> None:
    """Never the same sentence twice: add the street, then an ordinal."""
    seen: dict[str, int] = {}
    for place in rows:
        intro = place["intro"]
        if intro in seen:
            street = place["tags"].get("addr:street")
            if street:
                intro = intro.rstrip(".") + f", on {street}."
            if intro in seen:
                n = seen[place["intro"]]
                intro = place["intro"].rstrip(".") + f" (the {ORDINALS[min(n - 1, 6)]} of its kind here)."
        seen[place["intro"]] = seen.get(place["intro"], 0) + 1
        seen.setdefault(intro, 1)
        place["intro"] = intro


def contrast(rows: list[dict]) -> str:
    """One sentence telling the picks apart, built only from the data."""
    if len(rows) < 2:
        return ""
    parts = [f"the closest is {rows[0]['name']}"]
    free = [r for r in rows if r["fee"] == "Free"]
    if len(free) == 1:
        parts.append(f"{free[0]['name']} is the only one listed as free")
    dated = [r for r in rows if r["year"]]
    if len(dated) >= 2:
        oldest = min(dated, key=lambda r: r["year"])
        parts.append(f"{oldest['name']} is the oldest, from {oldest['year']}")
    hours = [r for r in rows if r["hours"]]
    if len(hours) == 1:
        parts.append(f"only {hours[0]['name']} lists opening hours")
    step = [r for r in rows if r["step_free"] == "Step-free"]
    if len(step) == 1:
        parts.append(f"{step[0]['name']} is the only one tagged step-free")
    sentence = "; ".join(parts[:4])
    return sentence[:1].upper() + sentence[1:] + "."


def make_list(rows: list[dict]) -> str:
    """One block per place, which renders cleanly in chat. A line with no
    data is left out rather than filled with "unavailable"."""
    blocks = []
    for number, place in enumerate(rows, 1):
        lines = [f"**{number}. {place['name']}**: {format_distance(place['distance'])}",
                 place["intro"]]
        details = [d for d in (f"Hours: {place['hours']}" if place["hours"] else "",
                               place["fee"] or "", place["step_free"] or "") if d]
        if details:
            lines.append(" · ".join(details))
        # Two trailing spaces: a hard line break in markdown.
        blocks.append("  \n".join(lines))
    return "\n\n".join(blocks)
