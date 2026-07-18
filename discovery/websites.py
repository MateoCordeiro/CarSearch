"""
discovery/websites.py — website-resolution chain for merged candidates
(plan step 5).

By the time a Candidate reaches here it has already been through
discovery/merge.py, so steps 1-2 of the plan's chain (an OSM website/
contact:website/url tag, or another source filling the domain during merge)
are already resolved — merge.py's conflict-resolution rules make OSM/Places
candidates win the website field over registry candidates. This module only
runs steps 3-4 for whatever's left: Google Places (if a key is configured)
then a DuckDuckGo HTML search, both gated by a blocklist and by
_name_domain_score so a low-confidence guess never gets accepted as fact.
A candidate that still has no website after both is given up on —
(None, None) — and it's the orchestrator's job (plan step 6) to persist
that as scrape_status='no_website' and stamp website_checked_at so the
30-day re-attempt guard below actually has something to check next time.
"""
import time
from datetime import datetime, timedelta

import requests
from bs4 import BeautifulSoup

from dealer_ops import _name_domain_score
from discovery.base import canonical_website
from discovery.places import search_website as _places_search_website

# Sites that can carry a dealer's name in search results without being the
# dealer's own inventory site — accepting one of these as "the website"
# would point the scraper at Facebook/Yelp/an aggregator instead of the lot.
BLOCKLIST = {
    "facebook.com", "instagram.com", "google.com", "yelp.com",
    "cars.com", "cargurus.com", "carsforsale.com", "autotrader.com",
    "craigslist.org",
}

_DDG_URL = "https://html.duckduckgo.com/html/"
# DuckDuckGo's html-only endpoint returns a non-answer (HTTP 202, no results)
# to a GET request or to any honest/identifying User-Agent — verified live
# 2026-07-18, see docs/PROGRESS.md. A POST with a browser-like UA is the only
# combination that returns real results. This is the one deliberate
# exception in discovery/ to identifying honestly (OSM/registry both use
# BumperScraper's real UA) — a conscious tradeoff the user chose to make
# after being walked through the ToS/fragility/IP-risk implications, which
# is also why web_search_fallback defaults to OFF (see config.json) rather
# than on like the rest of discovery's keyless sources.
_DDG_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
_DDG_HEADERS = {"User-Agent": _DDG_UA}
_DDG_MIN_SCORE = 2
_DDG_DELAY_SECONDS = 5  # plan: "DDG HTML (config toggle, >=5s delay)"
_TIMEOUT = 15
REATTEMPT_DAYS = 30


def _is_blocked(website):
    host = (canonical_website(website) or "").replace("https://", "")
    if not host:
        return False
    return any(host == b or host.endswith("." + b) for b in BLOCKLIST)


def usable_website(website):
    """Canonical form of a website value that will resolve for free (parses
    to a real host and isn't blocklisted), else None. The orchestrator's
    30-day guard uses this too: a candidate whose website tag is garbage
    ("yes" — real OSM data) or blocked (a Facebook page) must be gated like
    a site-less candidate, or it re-spends a Places/DDG attempt every
    single run no matter how recently it was last checked."""
    canon = canonical_website(website)
    if canon and not _is_blocked(canon):
        return canon
    return None


def should_attempt(website_checked_at, refresh_days=REATTEMPT_DAYS, now=None):
    """False if this dealer was checked (successfully or not) within
    refresh_days — the Places cost guard: without this, a daily scheduled
    discovery re-spends the whole Places budget re-checking dealers that
    were already confirmed to have no findable website yesterday."""
    if not website_checked_at:
        return True
    try:
        checked = datetime.fromisoformat(website_checked_at)
    except ValueError:
        return True
    now = now or datetime.utcnow()
    return now - checked >= timedelta(days=refresh_days)


def _ddg_search(name, city, state, _post=requests.post, _sleep=time.sleep):
    """Best-effort DuckDuckGo HTML search. Off by default
    (config.DISCOVERY['web_search_fallback'] gates whether this is ever
    called at all); never raises.

    `_post`/`_sleep` are injectable so tests can verify the real HTML
    parsing against a fixture without a real network call or a real 5s
    wait per test."""
    _sleep(_DDG_DELAY_SECONDS)
    query = f"{name} {city} {state} dealership".strip()
    try:
        r = _post(_DDG_URL, data={"q": query}, headers=_DDG_HEADERS, timeout=_TIMEOUT)
        r.raise_for_status()
    except Exception as e:
        print(f"[websites] DDG search failed for {query!r}: {e}")
        return None
    soup = BeautifulSoup(r.text, "html.parser")
    # Sponsored results are marked with a "result--ad" class and can rank
    # ABOVE organic results — confirmed live 2026-07-18: a real query for
    # "Round Rock Toyota" returned a paid ad for a different dealer
    # (Toyota of Cedar Park) as the first result__a on the page. Skip any
    # block carrying that class; only take the first organic ("web-result")
    # one, or nothing.
    for block in soup.find_all("div", class_="result"):
        if "result--ad" in (block.get("class") or []):
            continue
        a = block.find("a", class_="result__a")
        if a and a.get("href"):
            return a.get("href")
    return None


def resolve_website(candidate, config, places_budget=None, run_id=None,
                     places_search=_places_search_website, ddg_search=_ddg_search):
    """Returns (website, website_source) for a MERGED candidate.
    website_source is one of 'osm-tag' | 'merge-fill' | 'places' |
    'web-search', or None if nothing panned out. The blocklist is applied
    at every step, not just DDG.

    `places_search`/`ddg_search` are injectable so tests can stub both
    network calls out entirely (plan: "stubbed tests") — production callers
    should leave them at their defaults."""
    existing = usable_website(candidate.website)
    if existing:
        source = "osm-tag" if candidate.source == "osm" else "merge-fill"
        return existing, source

    if not candidate.name:
        # nothing to search for — Places/DDG would otherwise be asked to
        # find "None <city> <state>", a nonsense query that wastes a
        # Places budget slot and a DDG request for a guaranteed miss.
        return None, None

    cfg = config or {}
    sources_cfg = cfg.get("sources", {})
    api_key = cfg.get("google_places_key")
    if sources_cfg.get("places") and api_key and places_budget is not None:
        website = places_search(api_key, candidate.name, candidate.city, candidate.state,
                                 candidate.lat, candidate.lng, places_budget)
        if website and not _is_blocked(website):
            return canonical_website(website), "places"

    if cfg.get("web_search_fallback"):
        website = ddg_search(candidate.name, candidate.city, candidate.state)
        if website and not _is_blocked(website):
            domain = (canonical_website(website) or "").replace("https://", "")
            if _name_domain_score(candidate.name, domain) >= _DDG_MIN_SCORE:
                return canonical_website(website), "web-search"

    return None, None
