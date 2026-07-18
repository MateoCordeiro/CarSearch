"""
discovery — autonomous dealer discovery for any US ZIP + radius (plan
step 6 orchestrator).

run_discovery() is the entry point dealer_ops.discover_dealers wraps
(engine='auto', the default). It fetches from every enabled source (OSM,
state registries), merges candidates across sources, resolves a website
for whatever's still site-less (subject to the 30-day re-attempt guard),
upserts into `dealerships` via dealer_ops._ensure_dealer, and finally
recomputes distance_mi/location_tag for every dealer row against the
current origin (the "moved to New Orleans, still scanning Round Rock" fix).

See docs/PLAN-discovery.md for the full design and docs/PROGRESS.md for
what changed from the plan during implementation.
"""
import uuid
from datetime import datetime

from geopy.distance import geodesic

from database import get_conn, init_db, log_event, zip_to_coords, update_dealer_scrape_status
from discovery.base import normalize_name, zip5
from discovery.merge import merge_candidates
from discovery.osm import OverpassProvider
from discovery.registry import RegistryProvider, _zip_coords_map
from discovery.places import PlacesBudget
from discovery.websites import resolve_website, should_attempt, usable_website

# Providers that produce raw candidates. Places is deliberately NOT here —
# it's resolution-only this round (see discovery/places.py), invoked from
# inside the website-resolution step below, not as its own source.
_PROVIDERS = (OverpassProvider(), RegistryProvider())


def _lookup_website_checked_at(conn, candidate):
    """Read-only lookup mirroring _ensure_dealer's match order (minus the
    website step, since a site-less candidate's website is exactly what
    we're deciding whether to spend a Places call trying to resolve) —
    used only to feed the 30-day re-attempt guard before resolve_website
    runs, not to upsert anything."""
    if candidate.source and candidate.source_id:
        row = conn.execute(
            "SELECT website_checked_at FROM dealerships WHERE discovery_source=? AND discovery_source_id=?",
            (candidate.source, candidate.source_id)).fetchone()
        if row:
            return row["website_checked_at"]
    if candidate.name and candidate.zip:
        z5, nm = zip5(candidate.zip), normalize_name(candidate.name)
        if z5 and nm:
            for row in conn.execute(
                "SELECT name, website_checked_at FROM dealerships WHERE zip LIKE ?", (z5 + "%",)
            ).fetchall():
                if normalize_name(row["name"]) == nm:
                    return row["website_checked_at"]
    return None


def _recompute_stale_origins(conn, origin, location_tag):
    """At the end of every run, recompute distance_mi for EVERY dealership
    row (not just this run's candidates) against the CURRENT origin, and
    overwrite location_tag. Fixes the case where the user's configured
    ZIP+radius changed since a dealer was last discovered: its distance_mi
    now correctly reflects the new origin (almost always excluding it from
    scan_inventory's distance_mi<=radius filter without needing anything
    deleted), rather than leaving a stale distance from wherever the origin
    used to be."""
    rows = conn.execute("SELECT id, lat, lng, zip FROM dealerships").fetchall()
    # Registry-sourced dealers carry no lat/lng, so a registry-heavy DB has
    # thousands of rows falling back to the ZIP lookup — batch it in one
    # query instead of one SQLite connection per row (same fix as
    # RegistryProvider.find's _zip_coords_map, same reason).
    coords_by_zip = _zip_coords_map(
        conn, [r["zip"] for r in rows if r["lat"] is None and r["zip"]])
    for row in rows:
        coords = (row["lat"], row["lng"]) if row["lat"] is not None \
            else coords_by_zip.get(zip5(row["zip"]))
        if not coords or coords[0] is None:
            continue
        dist = round(geodesic(origin, coords).miles, 1)
        conn.execute("UPDATE dealerships SET distance_mi=?, location_tag=? WHERE id=?",
                     (dist, location_tag, row["id"]))


def run_discovery(zip_code, radius_mi, progress_cb=None, run_id=None):
    """Autonomous discovery: OSM + state registries -> merge -> resolve
    websites -> upsert -> recompute distances. Same conventions as sibling
    ops in dealer_ops.py: cb(msg, pct), log_event, a result dict.

    Per-provider failure is caught and logged, never raised — one broken
    source must not block a run that has others to try. The run only
    reports as a soft failure (added=0, in_range=0, errors populated) when
    EVERY source returned nothing; upsert/recompute still proceed normally
    on partial success."""
    import config
    init_db()
    run_id = run_id or uuid.uuid4().hex[:8]

    def cb(msg, pct):
        print(msg, flush=True)
        if progress_cb:
            progress_cb(msg, pct)

    origin = zip_to_coords(zip_code)
    if not origin:
        msg = f"No coordinates for ZIP {zip_code} — run bootstrap.py first?"
        cb(msg, 100)
        log_event(run_id, "discover", "info", detail=msg)
        return {"in_range": 0, "added": 0, "run_id": run_id,
                "per_source": {}, "errors": [msg]}
    lat, lng = origin

    # ── 0-30: fetch from every source ──
    all_candidates = []
    per_source = {}
    errors = []
    for i, provider in enumerate(_PROVIDERS):
        cb(f"Fetching from {provider.name}…", int(i / len(_PROVIDERS) * 28))
        try:
            found = provider.find(lat, lng, radius_mi)
        except Exception as e:
            print(f"[discovery] {provider.name} source failed: {e}")
            log_event(run_id, "discover", "info", detail=f"{provider.name} source failed: {e}")
            errors.append(f"{provider.name}: {e}")
            found = []
        per_source[provider.name] = len(found)
        all_candidates.extend(found)
    cb(f"{len(all_candidates)} raw candidates from {len(_PROVIDERS)} sources", 30)

    if not all_candidates:
        msg = "All discovery sources returned nothing this run"
        cb(msg, 100)
        log_event(run_id, "discover", "summary", detail=f"{msg}: {errors}")
        return {"in_range": 0, "added": 0, "run_id": run_id,
                "per_source": per_source, "errors": errors}

    # ── 30-60: merge + resolve websites ──
    merged = merge_candidates(all_candidates)
    cb(f"{len(merged)} merged candidates ({len(all_candidates)} raw)", 35)

    discovery_cfg = config.DISCOVERY
    places_budget = PlacesBudget(discovery_cfg.get("places_call_budget_per_run", 300))
    location_tag = f"{zip_code}:{radius_mi}"

    lookup_conn = get_conn()
    resolved = []
    for i, c in enumerate(merged, 1):
        checked_at_existing = _lookup_website_checked_at(lookup_conn, c)
        website, website_source, checked_at = None, None, None
        # A usable website tag resolves for free, so always take it. A
        # candidate whose tag is garbage/blocked falls through to Places/DDG
        # inside resolve_website, so it must be gated by the 30-day guard
        # exactly like a site-less one — a bare `c.website` truthiness check
        # here would bypass the guard and re-spend budget on it every run.
        if usable_website(c.website) or should_attempt(checked_at_existing):
            website, website_source = resolve_website(
                c, discovery_cfg, places_budget=places_budget, run_id=run_id)
            checked_at = datetime.utcnow().isoformat()
        resolved.append((c, website, website_source, checked_at))
        if i % 25 == 0:
            cb(f"  resolving websites {i}/{len(merged)}…", 35 + int(i / len(merged) * 25))
    lookup_conn.close()
    cb(f"Website resolution done ({places_budget.calls_made} Places calls used)", 60)

    # ── 60-100: upsert ──
    from dealer_ops import _ensure_dealer
    added = 0
    for i, (c, website, website_source, checked_at) in enumerate(resolved, 1):
        # dealerships.name is NOT NULL — real OSM shop=car elements can
        # legitimately carry no name tag (confirmed live: a run crashed
        # here partway through a real 45-candidate batch). The location is
        # still a real dealership worth keeping (that's the whole point of
        # "find every dealership"), so it gets an honest placeholder
        # instead of being silently dropped or crashing the run.
        name = c.name or f"Unnamed dealer ({c.source}:{c.source_id})"
        row = {
            "name": name, "address": c.address, "city": c.city, "state": c.state,
            "zip": c.zip, "phone": c.phone, "lat": c.lat, "lng": c.lng,
            "website": website, "discovery_source": c.source, "discovery_source_id": c.source_id,
            "website_source": website_source, "website_checked_at": checked_at,
        }
        did, is_new = _ensure_dealer(row, location_tag=location_tag)
        if is_new:
            added += 1
            log_event(run_id, "discover", "dealer_added", dealer=name,
                      detail=f"{c.city} {c.zip} · {c.source} · {website or 'no website found'}")
        if not website:
            # classify_dealers only ever looks at dealers that already have
            # a website, so a site-less discovery dealer would otherwise
            # never get ANY scrape_status and stay invisible in
            # /api/dealers/lists (plan step 6 calls this out by name).
            # COALESCE in _ensure_dealer's UPDATE can preserve a real
            # website from a PRIOR run even when this run's resolution
            # attempt came up empty, so re-check the row's actual final
            # state before stamping it unscrapeable.
            check_conn = get_conn()
            final = check_conn.execute("SELECT website FROM dealerships WHERE id=?", (did,)).fetchone()
            check_conn.close()
            if final and not final["website"]:
                update_dealer_scrape_status(did, None, "no_website", 0, "no website found by discovery")
        if i % 25 == 0:
            cb(f"  upserting {i}/{len(resolved)} ({added} new)…", 60 + int(i / len(resolved) * 35))

    conn = get_conn()
    _recompute_stale_origins(conn, origin, location_tag)
    conn.commit()
    conn.close()

    log_event(run_id, "discover", "summary",
              detail=f"Discovery done: {added} new dealers, {len(merged)} merged "
                     f"candidates ({per_source}) within {radius_mi}mi of {zip_code}")
    cb(f"Done. {added} new dealers added ({len(merged)} candidates in range).", 100)
    return {"in_range": len(merged), "added": added, "run_id": run_id,
            "per_source": per_source, "errors": errors}
