"""
discovery/places.py — Google Places Text Search, resolution-only (plan
step 5).

NOT a discovery source of its own — no gridded/nearby search here, that was
cut in the plan's review pass. This is used only to try to find a website
for a candidate that has one from neither an OSM tag nor cross-source merge.
Requires an API key (config.DISCOVERY["google_places_key"]); does nothing
without one — keyless-by-default is the whole point of this project, Places
is opt-in.
"""
import requests
from geopy.distance import geodesic

PLACES_URL = "https://places.googleapis.com/v1/places:searchText"
FIELD_MASK = "places.websiteUri,places.location,places.formattedAddress"
PROXIMITY_MILES = 2.0
_TIMEOUT = 15


class PlacesBudget:
    """Per-run call budget. Places New's free tier is 1,000 calls/month;
    without a hard per-run cap, a handful of large-radius runs a day blows
    through that in days (300 calls/run daily ~= $280/mo vs free) — this is
    the guard the plan calls out by name."""

    def __init__(self, cap):
        self.cap = cap
        self.calls_made = 0

    def spend(self):
        if self.calls_made >= self.cap:
            return False
        self.calls_made += 1
        return True

    @property
    def exhausted(self):
        return self.calls_made >= self.cap


def search_website(api_key, name, city, state, lat, lng, budget, _post=requests.post):
    """Text-search for a dealer by name+city+state; accept a result only if
    its returned location is within PROXIMITY_MILES of the candidate's own
    coordinates — guards against Places confidently returning a same-named
    dealer in a different city/state. Returns a website URL or None. Never
    raises — a Places outage must not take down a discovery run.

    `_post` is injectable so tests can stub the HTTP call (see
    tests/test_discovery.py) without a real network request or API key."""
    if not api_key or budget is None or lat is None or lng is None:
        return None
    if not budget.spend():
        return None
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": FIELD_MASK,
    }
    query = f"{name} {city} {state}".strip()
    try:
        r = _post(PLACES_URL, headers=headers, json={"textQuery": query}, timeout=_TIMEOUT)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        print(f"[places] search failed for {query!r}: {e}")
        return None

    places = data.get("places") or []
    if not places:
        return None
    top = places[0]
    website = top.get("websiteUri")
    location = top.get("location") or {}
    p_lat, p_lng = location.get("latitude"), location.get("longitude")
    if not website or p_lat is None or p_lng is None:
        return None
    if geodesic((lat, lng), (p_lat, p_lng)).miles > PROXIMITY_MILES:
        return None
    return website
