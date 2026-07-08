"""
Dealer operations — cleanly separated and logged.

  dealers_in_radius(zip, radius)  -> directory dealers within true per-dealer distance
  discover_dealers(...)           -> add in-radius directory dealers to the working set
  classify_dealers(...)           -> test-scrape each dealer's site -> platform / can-scrape
  scan_inventory(...)             -> re-scrape inventory, diff vs DB, mark SOLD, log everything

Every run writes to the scan_log activity table so the user can see exactly what
was added / removed / updated.
"""
import re
import uuid
from datetime import datetime, timedelta
from urllib.parse import urlparse

from geopy.distance import geodesic

from database import (get_conn, init_db, log_event, zip_to_coords,
                      upsert_listing, update_dealer_scrape_status)
from scrapers.dealers import DealerScraper


# ── Directory hygiene: drop garbage cardealerdb entries ───────
# cardealerdb is not always accurate: a dealer entry can carry a website (and
# address) belonging to an ENTIRELY DIFFERENT dealer — e.g. "Classic Oldsmobile"
# and "Round Rock Classic Cars" both list roundrocktoyota.com, which is really
# Round Rock Toyota. Such entries are stale junk; scraping them just re-scrapes
# the real dealer's site under a wrong name. Rule: for each website, keep only
# the one entry whose NAME matches the domain; the rest are garbage.

_JUNK_NAME = re.compile(r"\b(floral|flower|florist|boutique|salon|bakery|realty)\b", re.I)


def _domain(url):
    """Canonical host for a dealer website (scheme/www/trailing-slash stripped)."""
    if not url:
        return ""
    u = url if "://" in url else "http://" + url
    return re.sub(r"^www\.", "", urlparse(u).netloc.lower()).strip("/")


def _name_domain_score(name, domain):
    """How well a dealer NAME matches its website DOMAIN, by token overlap.
    A real dealer's name appears in its domain (roundrocktoyota.com ↔ "Round
    Rock Toyota" = 3); a stale entry pointing at someone else's site
    ("Classic Oldsmobile" → roundrocktoyota.com = 0) scores low. Junk
    non-dealer names (florist, etc.) are penalised."""
    dom = re.sub(r"\.[a-z]+$", "", domain)            # drop the TLD
    words = [w for w in re.findall(r"[a-z0-9]+", (name or "").lower()) if len(w) > 2]
    overlap = sum(1 for w in words if w in dom)
    return overlap - (5 if _JUNK_NAME.search(name or "") else 0)


def dedupe_by_website(dealers):
    """Collapse dealers that share a website domain to one real dealer per site
    (best name↔domain match). Returns (kept, dropped); 'dropped' are garbage
    entries that must NOT be scraped separately. Dealers without a website pass
    through untouched."""
    groups, no_site = {}, []
    for d in dealers:
        dom = _domain(d.get("website"))
        if dom:
            groups.setdefault(dom, []).append(d)
        else:
            no_site.append(d)
    kept, dropped = [], []
    for dom, members in groups.items():
        if len(members) == 1:
            kept.append(members[0])
            continue
        best = max(members, key=lambda m: _name_domain_score(m.get("name"), dom))
        kept.append(best)
        dropped.extend(m for m in members if m is not best)
    return kept + no_site, dropped


def dedupe_dealers_by_inventory(min_jaccard=0.9, apply=False, run_id=None):
    """Collapse dealer rows whose ACTIVE inventory is near-identical (VIN-set
    Jaccard >= min_jaccard) — i.e. different rooftop DOMAINS of one dealer group
    all serving the same shared inventory feed. Roger Beasley is the live case:
    3 domains (mazdageorgetown.com / rogerbeasleymazda.com / mazdacentral.com)
    each return the SAME 674 VINs, so the cars get stored 3x.

    `dedupe_by_website()` can't see this (different domains); this is the
    cross-domain complement. Keeps ONE canonical row per cluster — the one the
    listing-level dedup already blessed (most is_duplicate=0 active listings),
    tie-broken by quality_score then proximity — and marks the rest with
    `canonical_dealer_id`, deactivating their listings so they stop inflating the
    active count, wasting scans, and skewing per-dealer quality. The
    canonical_dealer_id flag is durable: scan/classify/quality all skip flagged
    rows, so a re-classify won't resurrect them.

    apply=False is a dry run (returns what it WOULD do, changes nothing).
    """
    import itertools
    run_id = run_id or uuid.uuid4().hex[:8]
    conn = get_conn()
    dealers = [dict(r) for r in conn.execute(
        "SELECT id, name, distance_mi, quality_score FROM dealerships "
        "WHERE canonical_dealer_id IS NULL").fetchall()]
    vinset, canon_kept = {}, {}
    for d in dealers:
        rows = conn.execute(
            "SELECT vin, SUM(CASE WHEN is_duplicate=0 THEN 1 ELSE 0 END) FROM listings "
            "WHERE dealership_id=? AND is_active=1 AND vin IS NOT NULL GROUP BY vin",
            (d["id"],)).fetchall()
        vinset[d["id"]] = {r[0] for r in rows}
        canon_kept[d["id"]] = sum(r[1] or 0 for r in rows)  # # of is_duplicate=0 listings
    conn.close()

    ids = [d["id"] for d in dealers if vinset[d["id"]]]
    parent = {i: i for i in ids}
    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]; x = parent[x]
        return x
    for a, b in itertools.combinations(ids, 2):
        va, vb = vinset[a], vinset[b]
        if len(va & vb) / len(va | vb) >= min_jaccard:
            parent[find(a)] = find(b)
    clusters = {}
    for i in ids:
        clusters.setdefault(find(i), []).append(i)

    byid = {d["id"]: d for d in dealers}
    report, to_apply = [], []   # to_apply = list of (dup_id, canonical_id)
    for members in clusters.values():
        if len(members) < 2:
            continue
        canon = max(members, key=lambda i: (canon_kept[i],
                                            byid[i]["quality_score"] or 0,
                                            -(byid[i]["distance_mi"] if byid[i]["distance_mi"] is not None else 9e9)))
        dups = [i for i in members if i != canon]
        report.append({"canonical_id": canon, "canonical": byid[canon]["name"],
                       "n_vins": len(vinset[canon]),
                       "duplicates": [byid[i]["name"] for i in dups]})
        to_apply += [(i, canon) for i in dups]

    if apply and to_apply:
        conn = get_conn()
        for dup_id, canon_id in to_apply:
            conn.execute("UPDATE listings SET is_active=0 WHERE dealership_id=? AND is_active=1", (dup_id,))
            conn.execute("UPDATE dealerships SET canonical_dealer_id=? WHERE id=?", (canon_id, dup_id))
        conn.commit(); conn.close()
        # log AFTER closing the write txn (log_event opens its own connection)
        for dup_id, canon_id in to_apply:
            log_event(run_id, "dedupe", "dealer_merged", dealer=byid[dup_id]["name"],
                      detail=f"duplicate inventory feed of '{byid[canon_id]['name']}' (id={canon_id}) "
                             f"— listings deactivated, row skipped in future scans")
        log_event(run_id, "dedupe", "summary",
                  detail=f"Dealer inventory-dedupe: {len(to_apply)} duplicate rows collapsed "
                         f"into {len(report)} canonical dealers")
    return {"clusters": report, "duplicates_collapsed": len(to_apply),
            "applied": bool(apply), "run_id": run_id}


def _norm_addr(a):
    return re.sub(r"[^a-z0-9]+", " ", (a or "").lower()).strip()


def clean_directory_garbage(delete=False):
    """Find tx_directory rows that are EXACT duplicates — same website domain AND
    same street address (one physical dealership listed under two names, e.g.
    'Vandergriff Honda' twice, or 'Wheels Leasing' + 'Midway Auto Group' at one
    address/site). Keeps the best name↔domain match, returns the rest as garbage;
    deletes them when delete=True.

    Deliberately conservative: it will NOT remove dealer-group stores that merely
    share a CORPORATE domain at DIFFERENT addresses (AutoNation has ~28 real TX
    stores on autonation.com). Stale entries pointing at another dealer's site
    from a different address (e.g. 'Classic Oldsmobile' -> roundrocktoyota.com)
    are left in place but are never scraped — dedupe_by_website collapses them to
    the real dealer at discover time."""
    conn = get_conn()
    rows = [dict(r) for r in conn.execute(
        "SELECT id, name, address, city, zip, website FROM tx_directory "
        "WHERE website IS NOT NULL AND website != ''").fetchall()]
    conn.close()

    groups = {}
    for r in rows:
        dom, na = _domain(r.get("website")), _norm_addr(r.get("address"))
        if dom and na:                       # require a real address to be certain
            groups.setdefault((dom, na), []).append(r)

    garbage = []
    for (dom, na), members in groups.items():
        if len(members) < 2:
            continue
        best = max(members, key=lambda m: _name_domain_score(m.get("name"), dom))
        for m in members:
            if m is not best:
                m["_keeps"], m["_domain"] = best["name"], dom
                garbage.append(m)

    if delete and garbage:
        conn = get_conn()
        conn.executemany("DELETE FROM tx_directory WHERE id=?", [(g["id"],) for g in garbage])
        conn.commit()
        conn.close()
    return garbage


# ── Radial search (per-dealer distance, not per-city) ─────────

def dealers_in_radius(zip_code, radius_mi):
    """Return directory dealers (with a website) whose OWN ZIP is within
    radius_mi of the user's ZIP. Each row gets a real distance_mi."""
    origin = zip_to_coords(zip_code)
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM tx_directory WHERE website IS NOT NULL AND website != ''"
    ).fetchall()
    conn.close()

    out = []
    for r in rows:
        d = dict(r)
        coords = (d["lat"], d["lng"]) if d.get("lat") is not None else zip_to_coords(d.get("zip"))
        if not (origin and coords and coords[0] is not None):
            continue
        dist = geodesic(origin, coords).miles
        if dist <= radius_mi:
            d["distance_mi"] = round(dist, 1)
            out.append(d)
    # one real dealer per website — drop cardealerdb junk pointing at another
    # dealer's site, so we never scrape the same site under multiple names
    out, dropped = dedupe_by_website(out)
    if dropped:
        print(f"[dealers_in_radius] dropped {len(dropped)} duplicate/garbage "
              f"entries sharing a site with a better-named dealer")
    out.sort(key=lambda x: x["distance_mi"])
    return out


def _ensure_dealer(d, distance_mi=None):
    """Insert/update a dealerships row from a directory dealer. Returns id."""
    conn = get_conn()
    cur = conn.cursor()
    existing = cur.execute("SELECT id FROM dealerships WHERE website=?", (d.get("website"),)).fetchone()
    now = datetime.utcnow().isoformat()
    if existing:
        cur.execute("UPDATE dealerships SET directory_id=?, distance_mi=COALESCE(?,distance_mi), "
                    "lat=COALESCE(?,lat), lng=COALESCE(?,lng), last_seen=? WHERE id=?",
                    (d.get("id"), distance_mi, d.get("lat"), d.get("lng"), now, existing["id"]))
        did = existing["id"]
    else:
        cur.execute("""INSERT INTO dealerships
            (name,address,city,state,zip,phone,website,lat,lng,distance_mi,directory_id)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (d.get("name"), d.get("address"), d.get("city"), d.get("state"), d.get("zip"),
             d.get("phone"), d.get("website"), d.get("lat"), d.get("lng"), distance_mi, d.get("id")))
        did = cur.lastrowid
    conn.commit()
    conn.close()
    return did, (existing is None)


# ── Discover: add in-radius directory dealers to the working set ──

def discover_dealers(zip_code, radius_mi, progress_cb=None, run_id=None):
    init_db()
    run_id = run_id or uuid.uuid4().hex[:8]
    def cb(msg, pct):
        print(msg, flush=True)
        if progress_cb: progress_cb(msg, pct)

    cands = dealers_in_radius(zip_code, radius_mi)
    cb(f"{len(cands)} dealers within {radius_mi} mi of {zip_code}", 5)
    log_event(run_id, "discover", "info",
              detail=f"{len(cands)} dealers within {radius_mi}mi of {zip_code}")

    added = 0
    for i, d in enumerate(cands, 1):
        did, is_new = _ensure_dealer(d, d["distance_mi"])
        if is_new:
            added += 1
            log_event(run_id, "discover", "dealer_added", dealer=d.get("name"),
                      detail=f"{d.get('city')} {d.get('zip')} · {d['distance_mi']}mi · {d.get('website')}")
        if i % 20 == 0:
            cb(f"  processed {i}/{len(cands)} ({added} new)", 5 + int(i/len(cands)*90))
    log_event(run_id, "discover", "summary",
              detail=f"Discovery done: {added} new dealers added ({len(cands)} in range)")
    cb(f"Done. {added} new dealers added ({len(cands)} within {radius_mi}mi).", 100)
    return {"in_range": len(cands), "added": added, "run_id": run_id}


# ── Classify: which dealers can we scrape, and why not ────────

def classify_dealers(zip_code=None, radius_mi=None, scope="all", resume=True,
                     progress_cb=None, run_id=None, page_cap=2, workers=5):
    """Test-scrape each dealer's website to record platform + scrape_status (which
    dealers we can/can't scrape, and why).
      scope='all'    — import EVERY TX-directory dealer into the working set, then
                       classify. Long job; resumable (skips dealers done <12h ago).
      scope='radius' — classify only dealers within radius_mi of zip_code."""
    init_db()
    run_id = run_id or uuid.uuid4().hex[:8]
    def cb(msg, pct):
        print(msg, flush=True)
        if progress_cb: progress_cb(msg, pct)

    s = DealerScraper(); s.delay_range = (0.3, 0.8); s.timeout = 12; s.MAX_PAGES_PER_DEALER = page_cap
    # classify only needs to confirm scrapability: probe up to 40 sitemap VDPs but
    # stop as soon as 3 parse (fast, yet robust to the first few URLs failing).
    s.sitemap_max_vdps = 40; s.sitemap_stop_after = 3

    # scope='all': pull every directory dealer into the working set first
    if scope == "all":
        cb("Importing TX directory into the working set…", 2)
        conn = get_conn()
        dirs = [dict(r) for r in conn.execute(
            "SELECT * FROM tx_directory WHERE website IS NOT NULL AND website!=''").fetchall()]
        conn.close()
        for d in dirs:
            _ensure_dealer(d, None)
        cb(f"Working set now covers {len(dirs)} directory dealers", 4)

    # candidate set
    if scope == "radius" and zip_code:
        cands = dealers_in_radius(zip_code, radius_mi or 50)
        for d in cands:
            _ensure_dealer(d, d["distance_mi"])
        sites = {d["website"] for d in cands}
        conn = get_conn()
        rows = [dict(r) for r in conn.execute(
            "SELECT id,name,city,state,zip,website,scrape_at FROM dealerships "
            "WHERE website IS NOT NULL AND website!='' AND canonical_dealer_id IS NULL").fetchall()]
        conn.close()
        rows = [r for r in rows if r["website"] in sites]
    else:
        conn = get_conn()
        rows = [dict(r) for r in conn.execute(
            "SELECT id,name,city,state,zip,website,scrape_at FROM dealerships "
            "WHERE website IS NOT NULL AND website!='' AND canonical_dealer_id IS NULL "
            "ORDER BY name").fetchall()]
        conn.close()

    if resume:
        cutoff = (datetime.utcnow() - timedelta(hours=12)).isoformat()
        skip = sum(1 for r in rows if r.get("scrape_at") and r["scrape_at"] > cutoff)
        rows = [r for r in rows if not (r.get("scrape_at") and r["scrape_at"] > cutoff)]
        if skip:
            cb(f"Resume: skipping {skip} dealers classified in the last 12h", 4)

    cb(f"Classifying {len(rows)} dealers…", 5)
    # concurrent scrape (network); status writes stay sequential below
    scraped = {}
    if workers and workers > 1 and len(rows) > 1:
        scraped = _parallel_scrape(
            rows,
            attrs={"delay_range": (0.3, 0.8), "timeout": 12,
                   "MAX_PAGES_PER_DEALER": page_cap,
                   "sitemap_max_vdps": 40, "sitemap_stop_after": 3},
            workers=workers)
    counts = {}
    for i, d in enumerate(rows, 1):
        if d["id"] in scraped:
            listings, platform, status, note = scraped[d["id"]]
        else:
            try:
                listings, platform, status, note = s._scrape_inventory(d)
            except Exception as e:
                platform, status, note, listings = "unknown", "error", str(e), []
        update_dealer_scrape_status(d["id"], platform, status, len(listings), note)
        counts[status] = counts.get(status, 0) + 1
        cb(f"[{i}/{len(rows)}] {status:11} {d['name'][:32]:32} ({platform})",
           5 + int(i/max(len(rows),1)*92))
    log_event(run_id, "classify", "summary", detail=f"Classified {len(rows)} dealers: {counts}")
    cb(f"Done. {counts}", 100)
    return counts


# ── Scan inventory: re-scrape, diff, mark SOLD, log ──────────

def _parallel_scrape(dealers, attrs=None, workers=5):
    """Scrape many dealers' inventory CONCURRENTLY — each worker uses its own
    thread-local DealerScraper (curl_cffi sessions aren't thread-safe). Different
    dealers are different hosts, so this is safe and polite; `workers` is the
    cross-host concurrency. Network only — no DB writes here. Returns
    {dealer_id: (listings, platform, status, note)}."""
    import threading
    from concurrent.futures import ThreadPoolExecutor
    local = threading.local()

    def work(d):
        s = getattr(local, "s", None)
        if s is None:
            s = local.s = DealerScraper()
            for k, v in (attrs or {}).items():
                setattr(s, k, v)
        try:
            return d["id"], s._scrape_inventory(d)
        except Exception as e:
            return d["id"], ([], "unknown", "error", str(e))

    out = {}
    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        for did, res in ex.map(work, dealers):
            out[did] = res
    return out


def scan_inventory(zip_code=None, radius_mi=None, only_ok=True, platforms=None,
                   progress_cb=None, run_id=None, page_cap=100, workers=5):
    init_db()
    run_id = run_id or uuid.uuid4().hex[:8]
    def cb(msg, pct):
        print(msg, flush=True)
        if progress_cb: progress_cb(msg, pct)

    s = DealerScraper(); s.delay_range = (0.3, 0.8); s.timeout = 12; s.MAX_PAGES_PER_DEALER = page_cap

    conn = get_conn()
    q = ("SELECT id,name,city,state,zip,website,distance_mi,platform FROM dealerships "
         "WHERE website IS NOT NULL AND website!='' AND canonical_dealer_id IS NULL")
    if only_ok:
        q += " AND scrape_status='ok'"
    dealers = [dict(r) for r in conn.execute(q).fetchall()]
    conn.close()

    # optional platform filter (e.g. re-scrape only the generic/sitemap dealers a
    # parser fix targets, without re-hitting the big DDC/Dealer Inspire sites)
    if platforms:
        dealers = [d for d in dealers if d.get("platform") in platforms]

    # radius filter (uses the distance stored at discovery time)
    if radius_mi is not None:
        dealers = [d for d in dealers if d.get("distance_mi") is not None and d["distance_mi"] <= radius_mi]

    cb(f"Scanning inventory for {len(dealers)} dealers…", 3)
    tot_new = tot_sold = tot_upd = 0

    # Pre-fetch every dealer's inventory CONCURRENTLY (network is the bottleneck);
    # the diff/SOLD/upsert logic below is unchanged and runs sequentially. Falls
    # back to the inline sequential scrape if disabled (workers<=1).
    scraped = {}
    if workers and workers > 1 and len(dealers) > 1:
        cb(f"Fetching {len(dealers)} dealers concurrently ({workers} workers)…", 3)
        scraped = _parallel_scrape(
            dealers,
            attrs={"delay_range": (0.3, 0.8), "timeout": 12, "MAX_PAGES_PER_DEALER": page_cap},
            workers=workers)

    for i, d in enumerate(dealers, 1):
        if d["id"] in scraped:
            listings, platform, status, note = scraped[d["id"]]
            if status == "error":
                log_event(run_id, "inventory", "info", dealer=d["name"], detail=f"scrape error: {note}")
                continue
        else:
            try:
                listings, platform, status, note = s._scrape_inventory(d)
            except Exception as e:
                log_event(run_id, "inventory", "info", dealer=d["name"], detail=f"scrape error: {e}")
                continue

        # Safety guard: a scrape that returns nothing (blocked/failed) must NOT
        # wipe a dealer's whole inventory as "sold". Skip the diff entirely.
        if not listings:
            log_event(run_id, "inventory", "info", dealer=d["name"],
                      detail=f"no inventory returned ({status}) — left existing listings untouched")
            cb(f"[{i}/{len(dealers)}] {d['name'][:30]:30} no inventory ({status}) — skipped", 3 + int(i/len(dealers)*94))
            continue

        # current active VINs for this dealer
        conn = get_conn()
        current = {r[0]: r[1] for r in conn.execute(
            "SELECT vin, url FROM listings WHERE dealership_id=? AND is_active=1 AND vin IS NOT NULL",
            (d["id"],)).fetchall()}
        conn.close()

        scraped_vins = set()
        scraped_urls = {}
        new_n = upd_n = 0
        for l in listings:
            l["dealership_id"] = d["id"]
            if not l.get("url"):
                continue
            if l.get("vin"):
                scraped_vins.add(l["vin"])
                scraped_urls[l["vin"]] = l["url"]
                if l["vin"] not in current:
                    new_n += 1
                    log_event(run_id, "inventory", "listing_added", dealer=d["name"],
                              detail=f"{l.get('year')} {l.get('make')} {l.get('model')} ${l.get('price')} · {l['vin']}")
                else:
                    upd_n += 1
            try:
                upsert_listing(l)
            except Exception:
                pass

        # SOLD: was active with a VIN, not in this scrape -> deactivate.
        # Do the UPDATEs and the log inserts on ONE connection/transaction to
        # avoid a self-deadlock (a second connection can't get the write lock).
        sold_vins = [v for v in current if v and v not in scraped_vins]
        conn = get_conn()
        for v in sold_vins:
            conn.execute("UPDATE listings SET is_active=0 WHERE dealership_id=? AND vin=?", (d["id"], v))
            conn.execute("INSERT INTO scan_log (run_id,run_type,action,dealer,detail) VALUES (?,?,?,?,?)",
                         (run_id, "inventory", "listing_sold", d["name"], f"VIN {v}"))
        # A parser upgrade can move a still-listed VIN to a NEW url. upsert_listing
        # is keyed on url, so the fresh row gets inserted while the old-url row for
        # the same car lingers active. Deactivate those stale same-VIN duplicates
        # (one VIN = one car = one VDP per dealer).
        for v, u in scraped_urls.items():
            conn.execute("UPDATE listings SET is_active=0 "
                         "WHERE dealership_id=? AND vin=? AND url!=? AND is_active=1",
                         (d["id"], v, u))
        conn.execute("UPDATE dealerships SET last_inventory_at=? WHERE id=?",
                     (datetime.utcnow().isoformat(), d["id"]))
        conn.commit(); conn.close()

        tot_new += new_n; tot_sold += len(sold_vins); tot_upd += upd_n
        cb(f"[{i}/{len(dealers)}] {d['name'][:30]:30} +{new_n} new / -{len(sold_vins)} sold / {upd_n} kept",
           3 + int(i/len(dealers)*94))

    log_event(run_id, "inventory", "summary",
              detail=f"Inventory scan: +{tot_new} new, -{tot_sold} sold, {tot_upd} updated across {len(dealers)} dealers")

    # Phase 5 — self-catching quality: recompute per-dealer data-quality after
    # every scan so thin sites / regressions are flagged automatically. Guarded:
    # a quality bug must never fail the scan that produced the data.
    try:
        fq = flag_quality(run_id=run_id)
        cb(f"Quality: {fq['flagged']}/{fq['scored']} dealers flagged for attention.", 100)
    except Exception as e:
        log_event(run_id, "quality", "info", detail=f"quality pass failed: {e}")

    cb(f"Done. +{tot_new} new, -{tot_sold} sold, {tot_upd} updated.", 100)
    return {"new": tot_new, "sold": tot_sold, "updated": tot_upd, "dealers": len(dealers), "run_id": run_id}


# ── VDP enrichment: backfill thin fields by fetching individual car pages ─────

def enrich_via_vdp(radius_mi=None, only_ok=True, cap_per_dealer=800,
                   platforms=("generic", "sitemap"), progress_cb=None, run_id=None):
    """Gated, cached VDP-fetch pass. For active listings missing
    mileage/trim/color/transmission on in-range dealers, fetch each car's own VDP
    (real url only, through the HTTP cache) and backfill what it exposes. Pure
    accuracy pass: never adds/removes listings, never touches price/VIN, and uses
    COALESCE so it can't null an existing value.

    The single biggest target is Roger Beasley Mazda (646 listings whose
    vehicleDetails SRP carries no mileage, but whose VDPs do)."""
    init_db()
    run_id = run_id or uuid.uuid4().hex[:8]
    def cb(msg, pct):
        print(msg, flush=True)
        if progress_cb: progress_cb(msg, pct)

    s = DealerScraper(); s.delay_range = (0.3, 0.8); s.timeout = 15
    s.cache_enabled = True   # don't refetch VDPs across re-runs / retries

    conn = get_conn()
    q = ("SELECT id, name, distance_mi, platform FROM dealerships "
         "WHERE website IS NOT NULL AND website!='' AND canonical_dealer_id IS NULL")
    if only_ok:
        q += " AND scrape_status='ok'"
    dealers = [dict(r) for r in conn.execute(q).fetchall()]
    conn.close()
    # VDP enrichment only helps thin-SRP platforms (generic/sitemap). DDC/Dealer
    # Inspire feeds are already complete; their 0-mileage rows are genuinely new
    # cars and must NOT trigger pointless VDP fetches.
    if platforms:
        dealers = [d for d in dealers if d.get("platform") in platforms]
    if radius_mi is not None:
        dealers = [d for d in dealers
                   if d.get("distance_mi") is not None and d["distance_mi"] <= radius_mi]

    cb(f"VDP enrichment over {len(dealers)} dealers…", 2)
    total = 0
    for di, d in enumerate(dealers, 1):
        conn = get_conn()
        # Trigger on missing MILEAGE — the one field the probe proved VDPs
        # recover (trim/color/transmission aren't on these dealers' VDPs, so
        # triggering on them would just waste fetches). The method still backfills
        # those too if a VDP happens to expose them.
        rows = [dict(r) for r in conn.execute(
            "SELECT id, url, trim, exterior_color, transmission, mileage "
            "FROM listings WHERE dealership_id=? AND is_active=1 "
            "AND url IS NOT NULL AND url NOT LIKE '%#%' "
            "AND (mileage IS NULL OR mileage=0)",
            (d["id"],)).fetchall()]
        conn.close()
        if not rows:
            continue

        n = s.enrich_via_vdp(rows, cap=cap_per_dealer)

        # write back ONLY the four enrich fields, by id, with COALESCE so an
        # existing value is never nulled. (Deliberately avoids upsert_listing,
        # whose UPDATE would overwrite raw_data.)
        conn = get_conn()
        for r in rows:
            conn.execute(
                "UPDATE listings SET mileage=COALESCE(?,mileage), trim=COALESCE(?,trim), "
                "exterior_color=COALESCE(?,exterior_color), "
                "transmission=COALESCE(?,transmission), last_seen=? WHERE id=?",
                (r.get("mileage"), r.get("trim"), r.get("exterior_color"),
                 r.get("transmission"), datetime.utcnow().isoformat(), r["id"]))
        conn.commit(); conn.close()

        total += n
        cb(f"[{di}/{len(dealers)}] {d['name'][:28]:28} enriched {n}/{len(rows)} "
           f"(missing-field listings)", 2 + int(di / max(len(dealers), 1) * 96))

    log_event(run_id, "inventory", "summary",
              detail=f"VDP enrichment: {total} listings backfilled across {len(dealers)} dealers")
    cb(f"Done. {total} listings enriched.", 100)
    return {"enriched": total, "dealers": len(dealers), "run_id": run_id}


def flag_quality(run_id=None, min_listings=3):
    """Phase 5 — self-catching quality. Recompute per-dealer data-quality over
    active listings, persist score/flags onto `dealerships`, and log the dealers
    that need attention to `scan_log`, so thin sites and scrape regressions
    surface on their own (no manual spot-checks). Called automatically at the
    end of scan_inventory(); also safe to run standalone or via the UI.

    The scoring model lives in metrics.dealer_quality (read-only); this is the
    write/log driver. Returns {scored, flagged, run_id}.
    """
    from metrics import dealer_quality, is_flagged, QUALITY_FLAG_THRESHOLD
    run_id = run_id or uuid.uuid4().hex[:8]

    # Compute + persist on one connection (the SELECT is materialized to a list
    # first, so the same conn can then UPDATE without a cursor conflict).
    conn = get_conn()
    scored = dealer_quality(conn, min_listings=min_listings)
    now = datetime.utcnow().isoformat()
    for d in scored:
        conn.execute("UPDATE dealerships SET quality_score=?, quality_flags=?, quality_at=? WHERE id=?",
                     (d["score"], ",".join(d["flags"]), now, d["id"]))
    conn.commit(); conn.close()

    # Log AFTER closing the write txn — log_event opens its own connection, and
    # doing that while holding the write lock self-deadlocks (known gotcha).
    worst = [d for d in scored if is_flagged(d)]
    for d in worst:
        log_event(run_id, "quality", "quality_flag", dealer=d["name"],
                  detail=f"score={d['score']} n={d['n']} flags={','.join(d['flags']) or '—'}")
    log_event(run_id, "quality", "summary",
              detail=f"Quality scan: {len(scored)} dealers scored, {len(worst)} flagged "
                     f"(<{QUALITY_FLAG_THRESHOLD} or high-severity)")
    return {"scored": len(scored), "flagged": len(worst), "run_id": run_id}
