"""
Duplicate detection engine.

Two-pass approach:
  Pass 1 — Exact VIN match: same VIN = same car, guaranteed.
  Pass 2 — Fuzzy match: same year+make+model, compatible trim, and close on
           BOTH mileage and price when both are known (either alone when only
           one is comparable). Clustered with a VIN-guarded union-find.

After detection, within each duplicate group we mark every listing as
is_duplicate=1 EXCEPT the cheapest/closest one (the "best" listing).
"""

import re
from database import get_conn
from rapidfuzz import fuzz
from config import DUPLICATE


def run_deduplication():
    """Run full deduplication across all active listings. Safe to call repeatedly."""
    print("Running duplicate detection...")

    # Reset all flags first (re-run from scratch)
    _reset_duplicate_flags()

    n_vin   = _dedup_by_vin()
    n_fuzzy = _dedup_by_fuzzy()

    print(f"  VIN matches: {n_vin} groups | Fuzzy matches: {n_fuzzy} groups")
    _mark_best_per_group()
    print("✓ Deduplication complete")


def _reset_duplicate_flags():
    conn = get_conn()
    conn.execute("UPDATE listings SET duplicate_group_id=NULL, is_duplicate=0 WHERE is_active=1")
    conn.execute("DELETE FROM duplicate_groups")
    conn.commit()
    conn.close()


def _dedup_by_vin() -> int:
    """Group listings that share the same non-null VIN."""
    conn = get_conn()
    c = conn.cursor()

    # Find VINs that appear more than once
    dupes = c.execute("""
        SELECT vin, COUNT(*) AS cnt
        FROM listings
        WHERE is_active=1 AND vin IS NOT NULL AND vin != ''
        GROUP BY vin
        HAVING cnt > 1
    """).fetchall()

    group_count = 0
    for row in dupes:
        vin = row["vin"]

        # Create a duplicate group
        c.execute("INSERT INTO duplicate_groups (vin, match_method, listing_count) VALUES (?,?,?)",
                  (vin, "vin", row["cnt"]))
        group_id = c.lastrowid

        # Tag all listings in this VIN group
        c.execute("UPDATE listings SET duplicate_group_id=? WHERE vin=? AND is_active=1",
                  (group_id, vin))
        group_count += 1

    conn.commit()
    conn.close()
    return group_count


# Common make spellings that must land in the same blocking bucket even though
# their first letters differ from the canonical name.
_MAKE_ALIASES = {
    "chevy": "chevrolet",
    "vw": "volkswagen",
    "mercedes": "mercedes benz",
    "benz": "mercedes benz",
    "landrover": "land rover",
    "rollsroyce": "rolls royce",
    "alfa": "alfa romeo",
}


def _make_key(make):
    """Normalized bucketing key for a make ('Chevy' and 'CHEVROLET' collide)."""
    m = re.sub(r"[^a-z0-9 ]+", " ", (make or "").lower())
    m = " ".join(m.split())
    m = _MAKE_ALIASES.get(m, m)
    return m[:5]


def _dedup_by_fuzzy() -> int:
    """
    Group listings without a VIN match using fuzzy heuristics.
    Only compares listings that don't already have a duplicate_group_id.

    Pairwise comparison happens only inside (year, make) buckets — per-bucket
    O(k²) instead of O(n²) over the whole table. Clusters are built with a
    union-find whose roots carry their cluster's VIN: two clusters anchored to
    DIFFERENT VINs can never be merged, so a VIN-less listing can't chain two
    distinct physical cars into one group transitively.
    """
    conn = get_conn()
    c = conn.cursor()

    rows = [dict(r) for r in c.execute("""
        SELECT id, vin, year, make, model, trim, mileage, price
        FROM listings
        WHERE is_active=1
          AND duplicate_group_id IS NULL
          AND year IS NOT NULL
          AND make IS NOT NULL
          AND model IS NOT NULL
    """).fetchall()]

    parent   = {r["id"]: r["id"] for r in rows}
    root_vin = {r["id"]: ((r.get("vin") or "").strip().upper() or None) for r in rows}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]   # path halving
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra == rb:
            return
        va, vb = root_vin[ra], root_vin[rb]
        if va and vb and va != vb:
            return                          # different physical cars — refuse
        parent[rb] = ra
        if vb and not va:
            root_vin[ra] = vb               # cluster inherits its anchor VIN

    buckets = {}
    for r in rows:
        buckets.setdefault((r["year"], _make_key(r["make"])), []).append(r)

    for members in buckets.values():
        for i, a in enumerate(members):
            for b in members[i + 1:]:
                if _is_fuzzy_match(a, b):
                    union(a["id"], b["id"])

    clusters = {}
    for r in rows:
        clusters.setdefault(find(r["id"]), []).append(r["id"])

    group_count = 0
    for ids in clusters.values():
        if len(ids) < 2:
            continue
        c.execute("INSERT INTO duplicate_groups (vin, match_method, listing_count) VALUES (?,?,?)",
                  (None, "fuzzy", len(ids)))
        gid = c.lastrowid
        c.executemany("UPDATE listings SET duplicate_group_id=? WHERE id=?",
                      [(gid, lid) for lid in ids])
        group_count += 1

    conn.commit()
    conn.close()
    return group_count


def _is_fuzzy_match(a: dict, b: dict) -> bool:
    """Return True if two listings are likely the same car."""
    # A VIN uniquely identifies a physical car. If BOTH listings carry a VIN and
    # the VINs differ, they are definitively different cars — never merge them.
    # (Listings that share a VIN are already grouped by the VIN pass and never
    # reach here, so this only blocks distinct-VIN cars that happen to match on
    # year/make/model/price — e.g. a row of identically-priced new vehicles.)
    va = (a.get("vin") or "").strip().upper()
    vb = (b.get("vin") or "").strip().upper()
    if va and vb and va != vb:
        return False

    # Must be the same year
    if a["year"] != b["year"]:
        return False

    # Make and model must be similar
    make_sim  = fuzz.token_sort_ratio(
        (a["make"] or "").lower(),
        (b["make"] or "").lower()
    )
    model_sim = fuzz.token_sort_ratio(
        (a["model"] or "").lower(),
        (b["model"] or "").lower()
    )
    threshold = DUPLICATE.get("fuzzy_threshold", 85)
    if make_sim < threshold or model_sim < threshold:
        return False

    # Trim gate: when BOTH listings name a trim and the trims clearly disagree
    # ("LX" vs "Touring"), they're different cars. token_set_ratio so a short
    # trim matching a verbose one ("SE" vs "SE Sport Utility") still passes,
    # and a missing trim never blocks the match.
    ta = (a.get("trim") or "").strip().lower()
    tb = (b.get("trim") or "").strip().lower()
    if ta and tb and fuzz.token_set_ratio(ta, tb) < 60:
        return False

    # Proximity: when both dimensions are comparable, BOTH must be close —
    # two same-model cars at the same price but 20k miles apart are different
    # cars (the old OR-rule merged them and hid real inventory). With only one
    # dimension comparable, that one must be close; with neither, don't guess.
    has_mileage = bool(a.get("mileage") and b.get("mileage"))
    has_price   = bool(a.get("price") and b.get("price"))
    mileage_close = has_mileage and abs(a["mileage"] - b["mileage"]) <= 500
    price_close   = has_price and abs(a["price"] - b["price"]) <= 500

    if has_mileage and has_price:
        return mileage_close and price_close
    if has_mileage:
        return mileage_close
    if has_price:
        return price_close
    return False


def _mark_best_per_group():
    """
    Within each duplicate group, keep is_duplicate=0 for the best listing
    (cheapest price, then closest distance), mark the rest as is_duplicate=1.
    """
    conn = get_conn()
    c = conn.cursor()

    groups = c.execute(
        "SELECT DISTINCT duplicate_group_id FROM listings WHERE duplicate_group_id IS NOT NULL AND is_active=1"
    ).fetchall()

    for (group_id,) in groups:
        members = c.execute("""
            SELECT id, price, distance_mi
            FROM listings
            WHERE duplicate_group_id=? AND is_active=1
        """, (group_id,)).fetchall()

        if len(members) < 2:
            continue

        # Pick the cheapest, then closest — but guard against a price outlier
        # winning. Within a duplicate group every listing is the SAME car, so a
        # value far below the others (e.g. a payment/fee that slipped through as
        # a "price") is bogus. Treat anything under 40% of the group's top price
        # — or under $1,000 outright (payment-sized even in a cheap group) — as
        # suspect and rank it last so it can't become the canonical listing.
        prices = [m["price"] for m in members if m["price"]]
        ref = max(prices) if prices else None

        def sort_key(m):
            p = m["price"]
            suspect = (p is None or p < 1000
                       or (ref is not None and p < 0.40 * ref))
            return (
                1 if suspect else 0,
                p if (p is not None and not suspect) else float("inf"),
                m["distance_mi"] if m["distance_mi"] is not None else float("inf"),
            )

        members = sorted(members, key=sort_key)
        best_id = members[0]["id"]
        other_ids = [m["id"] for m in members[1:]]

        c.execute("UPDATE listings SET is_duplicate=0 WHERE id=?", (best_id,))
        if other_ids:
            placeholders = ",".join("?" * len(other_ids))
            c.execute(f"UPDATE listings SET is_duplicate=1 WHERE id IN ({placeholders})", other_ids)

    conn.commit()
    conn.close()
