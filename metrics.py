"""
Project health metrics — the KPI baseline the improvement roadmap tracks.

Read-only. Prints a scannable report and (with --json) writes data/metrics.json
so phases can be compared before/after. Run: python metrics.py [--json]

KPIs: working-set coverage (ok vs cannot-scrape), per-platform field
completeness, synthetic-URL count, directory geocoding, and a per-dealer
data-quality summary (Phase 5 — self-catching quality).
"""
import json
import sqlite3
import sys
from datetime import datetime

from config import DB_PATH

FIELDS = ["year", "make", "model", "trim", "mileage", "vin", "price",
          "exterior_color", "transmission", "image_url"]

# ── Phase 5: per-dealer quality model ─────────────────────────────
# Platforms where a missing mileage is a SCRAPE GAP, not a legitimately new
# car. DDC / Dealer Inspire franchises carry lots of brand-new cars (0 mileage
# is correct there), so they're NOT penalized on mileage — this matches the
# Phase-3 VDP-enrichment scope.
MILEAGE_EXPECTED = {"generic", "sitemap"}
MIN_QUALITY_LISTINGS = 3      # don't score tiny lots — too noisy to be meaningful
QUALITY_FLAG_THRESHOLD = 80   # score below this ⇒ surfaced for attention
HIGH_FLAGS = {"synthetic_urls", "missing_vin"}  # always surface, any score


def dealer_quality(conn, min_listings=MIN_QUALITY_LISTINGS):
    """Per-dealer data-quality scan over ACTIVE listings. Read-only.

    Returns dicts sorted worst-first: {id, name, platform, n, score, flags[]}.
    Catches the two real bug classes the user hit:
      • synthetic '#' VDP urls  (M1 Motors: link went to the whole inventory)
      • missing mileage on used lots (Signature: 83k mi never captured)
    plus missing vin / price / image. New-car franchises (DDC/DI) are not
    penalized for 0 mileage.
    """
    rows = [dict(r) for r in conn.execute(
        "SELECT l.dealership_id did, l.url, l.vin, l.price, l.mileage, l.image_url, "
        "COALESCE(d.platform,'?') platform, d.name name "
        "FROM listings l JOIN dealerships d ON l.dealership_id=d.id "
        "WHERE l.is_active=1 AND l.dealership_id IS NOT NULL "
        "AND d.canonical_dealer_id IS NULL")]   # skip duplicate-feed rows (inert)
    by = {}
    for r in rows:
        by.setdefault(r["did"], []).append(r)

    out = []
    for did, rs in by.items():
        n = len(rs)
        if n < min_listings:
            continue
        plat, name = rs[0]["platform"], rs[0]["name"]
        frac = lambda pred: sum(1 for r in rs if pred(r)) / n
        syn   = frac(lambda r: (not r["url"]) or ("#" in r["url"]))
        novin = frac(lambda r: not r["vin"])
        nopr  = frac(lambda r: not r["price"])
        nomi  = frac(lambda r: not r["mileage"])
        noimg = frac(lambda r: not r["image_url"])

        flags, score = [], 100.0
        if syn   > 0.05:                              flags.append("synthetic_urls"); score -= 40 * syn
        if novin > 0.10:                              flags.append("missing_vin");    score -= 25 * novin
        if nopr  > 0.20:                              flags.append("missing_price");  score -= 20 * nopr
        if nomi  > 0.50 and plat in MILEAGE_EXPECTED: flags.append("missing_mileage");score -= 15 * nomi
        if noimg > 0.50:                              flags.append("missing_image");  score -= 10 * noimg
        out.append({"id": did, "name": name, "platform": plat, "n": n,
                    "score": max(0, round(score)), "flags": flags})
    out.sort(key=lambda d: (d["score"], -d["n"]))
    return out


def is_flagged(d):
    """A dealer is surfaced if it's below threshold OR has a high-severity flag."""
    return d["score"] < QUALITY_FLAG_THRESHOLD or bool(set(d["flags"]) & HIGH_FLAGS)


def _conn():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def collect():
    c = _conn()
    one = lambda q, *a: c.execute(q, a).fetchone()[0]
    m = {"generated_at": datetime.now().isoformat(timespec="seconds")}

    # ── Coverage (working-set classify outcomes) ──
    m["coverage"] = {r["scrape_status"]: r["n"] for r in c.execute(
        "SELECT scrape_status, COUNT(*) n FROM dealerships "
        "WHERE scrape_status IS NOT NULL GROUP BY scrape_status")}
    m["dealers_total"] = one("SELECT COUNT(*) FROM dealerships")

    # ── Listings + accuracy by platform ──
    m["active_listings"] = one("SELECT COUNT(*) FROM listings WHERE is_active=1")
    m["unique_cars"] = one("SELECT COUNT(*) FROM listings WHERE is_active=1 AND is_duplicate=0")
    m["synthetic_urls"] = one("SELECT COUNT(*) FROM listings WHERE is_active=1 AND url LIKE '%#%'")

    rows = [dict(r) for r in c.execute(
        "SELECT l.*, COALESCE(d.platform,'?') platform FROM listings l "
        "LEFT JOIN dealerships d ON l.dealership_id=d.id WHERE l.is_active=1")]
    plats = {}
    for r in rows:
        plats.setdefault(r["platform"], []).append(r)
    m["completeness"] = {}
    for p, rs in sorted(plats.items(), key=lambda kv: -len(kv[1])):
        n = len(rs)
        m["completeness"][p] = {"n": n, **{
            f: round(100 * sum(1 for r in rs if r.get(f) not in (None, "", 0)) / n)
            for f in FIELDS}}

    # ── Finding / directory ──
    m["directory"] = {
        "total": one("SELECT COUNT(*) FROM tx_directory"),
        "with_website": one("SELECT COUNT(*) FROM tx_directory WHERE website IS NOT NULL AND website!=''"),
        "geocoded": one("SELECT COUNT(*) FROM tx_directory WHERE lat IS NOT NULL"),
    }

    # ── Data quality (Phase 5 — self-catching quality) ──
    # The mission is broad + ACCURATE coverage, so the KPI is "how clean is our
    # data and which dealers are thin", not a single target vehicle.
    scored = dealer_quality(c)
    flagged = [d for d in scored if is_flagged(d)]
    m["quality"] = {
        "dealers_scored": len(scored),
        "flagged": len(flagged),
        "avg_score": round(sum(d["score"] for d in scored) / len(scored), 1) if scored else None,
        "worst": [{"name": d["name"], "platform": d["platform"], "score": d["score"],
                   "n": d["n"], "flags": d["flags"]} for d in scored[:10]],
    }
    c.close()
    return m


def report(m):
    print(f"=== BumperScraper metrics  {m['generated_at']} ===\n")
    cov = m["coverage"]
    ok = cov.get("ok", 0)
    print(f"COVERAGE  working set {m['dealers_total']} dealers | "
          f"ok={ok}  " + "  ".join(f"{k}={v}" for k, v in cov.items() if k != "ok"))
    print(f"LISTINGS  active={m['active_listings']}  unique={m['unique_cars']}  "
          f"synthetic_urls={m['synthetic_urls']}")
    print()
    print("FIELD COMPLETENESS BY PLATFORM (%)")
    hdr = "platform".ljust(15) + "n".rjust(6) + "".join(f.split('_')[0][:5].rjust(7) for f in FIELDS)
    print(hdr); print("-" * len(hdr))
    for p, d in m["completeness"].items():
        print(p[:14].ljust(15) + str(d["n"]).rjust(6) + "".join(str(d[f]).rjust(7) for f in FIELDS))
    print()
    dr = m["directory"]
    print(f"DIRECTORY total={dr['total']}  with_website={dr['with_website']}  geocoded={dr['geocoded']}")
    print()
    qy = m["quality"]
    print(f"DATA QUALITY  scored={qy['dealers_scored']}  flagged={qy['flagged']}  "
          f"avg_score={qy['avg_score']}  (flag if <{QUALITY_FLAG_THRESHOLD} or {'/'.join(sorted(HIGH_FLAGS))})")
    worst = [d for d in qy["worst"] if d["score"] < 90 or d["flags"]]
    if worst:
        print("  worst dealers:")
        for d in worst:
            print(f"    {d['score']:>3}  {d['name'][:32]:32} {d['platform'][:8]:8} "
                  f"n={d['n']:<4} {','.join(d['flags']) or '—'}")


if __name__ == "__main__":
    m = collect()
    report(m)
    if "--json" in sys.argv:
        import os
        os.makedirs("data", exist_ok=True)
        with open("data/metrics.json", "w", encoding="utf-8") as f:
            json.dump(m, f, indent=2)
        print("\nWrote data/metrics.json")
