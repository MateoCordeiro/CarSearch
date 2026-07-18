"""
discovery/osm.py — OpenStreetMap Overpass provider (plan step 3).

Primary discovery source: queries Overpass for shop=car nodes/ways/relations
within a radius, then reverse-fills the zip/city/state that most OSM
elements don't carry as tags. Deliberately does NOT reuse DealerScraper /
curl_cffi Chrome impersonation — Overpass's usage policy wants an honest,
identifying User-Agent, not a browser fingerprint, and this is a completely
different kind of endpoint (a public API, not a dealer site behind a WAF).
"""
import hashlib
import json
import math
import os

import requests
from geopy.distance import geodesic

from database import get_conn
from discovery.base import Candidate

NAME = "osm"

# Hardcoded pair, no config surface — Overpass mirror choice isn't something
# a user needs to tune, and every extra option is more failure surface.
_ENDPOINTS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
)

# Per Overpass's usage policy: identify yourself, don't pretend to be a
# browser. <10k queries/day is fine unauthenticated; this provider runs at
# most a couple of times a day per user.
_USER_AGENT = "BumperScraper/1.x (personal use; +contact)"
_TIMEOUT = 185  # the query itself carries a server-side 180s budget
_RETRYABLE_STATUS = (429, 504)

_CACHE_DIR = os.path.join("data", "http_cache")


def _build_query(lat, lng, radius_mi):
    radius_m = int(radius_mi * 1609.34)
    return (f'[out:json][timeout:180];'
            f'nwr["shop"="car"](around:{radius_m},{lat},{lng});'
            f'out center;')


def _cache_key(query):
    return hashlib.sha256(query.encode("utf-8")).hexdigest()


def _cache_load(query):
    path = os.path.join(_CACHE_DIR, _cache_key(query) + ".osm.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _cache_store(query, payload):
    try:
        os.makedirs(_CACHE_DIR, exist_ok=True)
        path = os.path.join(_CACHE_DIR, _cache_key(query) + ".osm.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f)
    except Exception:
        pass


def fetch_overpass(query):
    """POST to the primary endpoint, then the fallback, then the last
    successfully cached payload for this exact query, then give up (None).
    Never raises — a broken/rate-limited Overpass mirror must not take down
    a discovery run that has other sources to try."""
    headers = {"User-Agent": _USER_AGENT}
    for url in _ENDPOINTS:
        try:
            r = requests.post(url, data={"data": query}, headers=headers, timeout=_TIMEOUT)
        except Exception as e:
            print(f"[osm] {url} request error: {e}")
            continue
        if r.status_code == 200:
            payload = r.json()
            _cache_store(query, payload)
            return payload
        print(f"[osm] {url} returned {r.status_code}, trying next endpoint")
    cached = _cache_load(query)
    if cached is not None:
        print("[osm] all endpoints failed, serving last cached payload")
        return cached
    print("[osm] all endpoints failed and no cached payload — OSM source yields nothing this run")
    return None


def _element_center(el):
    """A node carries lat/lon directly; a way/relation queried with
    `out center` carries a 'center' object instead."""
    if "lat" in el and "lon" in el:
        return el["lat"], el["lon"]
    center = el.get("center") or {}
    return center.get("lat"), center.get("lon")


def _element_website(tags):
    for key in ("website", "contact:website", "url"):
        v = tags.get(key)
        if v:
            return v
    return None


def _element_address(tags):
    housenumber, street = tags.get("addr:housenumber"), tags.get("addr:street")
    if housenumber and street:
        return f"{housenumber} {street}"
    return street or None


def nearest_zip(conn, lat, lng):
    """Nearest zip_coords row to (lat, lng): a lat/lng bounding-box prefilter
    (cheap, avoids a geodesic call against the whole table) then exact
    geodesic distance on the survivors. Most OSM car-shop elements carry no
    addr:* tags at all, so this reverse fill is what makes the (name, zip)
    merge/match key usable for the primary source — without it an OSM
    candidate is only ever matchable by an exact website hit."""
    dlat = 0.5  # ~34mi of latitude — generous enough for sparse rural ZIPs
    dlng = 0.5 / max(0.15, math.cos(math.radians(lat)))
    rows = conn.execute(
        "SELECT zip, lat, lng, city, state FROM zip_coords "
        "WHERE lat BETWEEN ? AND ? AND lng BETWEEN ? AND ?",
        (lat - dlat, lat + dlat, lng - dlng, lng + dlng),
    ).fetchall()
    best, best_dist = None, None
    for row in rows:
        d = geodesic((lat, lng), (row["lat"], row["lng"])).miles
        if best_dist is None or d < best_dist:
            best, best_dist = row, d
    return best


def candidates_from_payload(payload, conn):
    """Turn a raw Overpass JSON payload into Candidates, applying the
    mandatory reverse-ZIP fill. Split out from find() so fixture tests can
    feed a payload straight in without a network call."""
    candidates = []
    for el in payload.get("elements", []):
        tags = el.get("tags", {}) or {}
        lat, lng = _element_center(el)
        if lat is None or lng is None:
            continue
        zip_code, city, state = tags.get("addr:postcode"), tags.get("addr:city"), tags.get("addr:state")
        if not zip_code:
            nearest = nearest_zip(conn, lat, lng)
            if nearest:
                zip_code = zip_code or nearest["zip"]
                city = city or nearest["city"]
                state = state or nearest["state"]
        candidates.append(Candidate(
            name=tags.get("name"),
            address=_element_address(tags),
            city=city,
            state=state,
            zip=zip_code,
            lat=lat,
            lng=lng,
            website=_element_website(tags),
            phone=tags.get("phone") or tags.get("contact:phone"),
            source="osm",
            source_id=f"{el.get('type')}/{el.get('id')}",
        ))
    return candidates


class OverpassProvider:
    name = NAME

    def find(self, lat, lng, radius_mi):
        payload = fetch_overpass(_build_query(lat, lng, radius_mi))
        if not payload:
            return []
        conn = get_conn()
        try:
            return candidates_from_payload(payload, conn)
        finally:
            conn.close()
