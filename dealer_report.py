"""
Dealer scrape report.

For every dealership already in the database, attempt an inventory scrape,
record the per-dealer outcome (platform / status / count / reason) back into
the `dealerships` table, and write a human-readable report to
DEALER_SCRAPE_REPORT.md.

Run:  python dealer_report.py [max_pages_per_dealer]

This does NOT re-discover dealers — it reports on the dealers already saved,
which is exactly "which of our dealers can / can't we scrape, and why".
"""
import sys
import time
from datetime import datetime, timedelta
from collections import Counter, defaultdict

from database import get_conn, update_dealer_scrape_status, upsert_listing, init_db
from scrapers.dealers import DealerScraper


def main():
    init_db()  # ensure the diagnostic columns exist
    cap = int(sys.argv[1]) if len(sys.argv) > 1 else 5

    scraper = DealerScraper()
    scraper.MAX_PAGES_PER_DEALER = cap
    scraper.delay_range = (0.3, 0.8)   # bulk classification — touch each domain only a few times
    scraper.timeout = 12

    # Resume support: pass "resume" to skip dealers already scraped in the last
    # 30 min (so a crash-and-restart continues instead of redoing everything).
    resume = "resume" in sys.argv

    conn = get_conn()
    dealers = [dict(r) for r in conn.execute(
        "SELECT id, name, city, state, zip, website, scrape_at FROM dealerships ORDER BY name"
    ).fetchall()]
    conn.close()

    if resume:
        cutoff = (datetime.utcnow() - timedelta(minutes=30)).isoformat()
        before = len(dealers)
        dealers = [d for d in dealers if not (d.get("scrape_at") and d["scrape_at"] > cutoff)]
        print(f"Resume: skipping {before - len(dealers)} dealers scraped in the last 30 min")

    print(f"Reporting on {len(dealers)} dealers (page cap {cap})\n")
    rows = []
    t0 = time.time()

    for i, d in enumerate(dealers, 1):
        if not d.get("website"):
            update_dealer_scrape_status(d["id"], "none", "unsupported", 0, "no website on file")
            rows.append((d["name"], "none", "unsupported", 0, "no website on file"))
            print(f"[{i:3}/{len(dealers)}] unsupported  {d['name'][:38]:38}    0  (no website)")
            continue

        try:
            listings, platform, status, note = scraper._scrape_inventory(d)
        except Exception as e:
            update_dealer_scrape_status(d["id"], "unknown", "error", 0, f"scrape error: {e}")
            rows.append((d["name"], "unknown", "error", 0, str(e)))
            print(f"[{i:3}/{len(dealers)}] error       {d['name'][:38]:38}    0  ({e})")
            continue

        for l in listings:
            l["dealership_id"] = d["id"]
            if l.get("url"):
                try:
                    upsert_listing(l)
                except Exception as e:
                    print(f"      ! skipped a listing for {d['name']}: {e}")
        update_dealer_scrape_status(d["id"], platform, status, len(listings), note)
        rows.append((d["name"], platform, status, len(listings), note))
        print(f"[{i:3}/{len(dealers)}] {status:11} {d['name'][:38]:38} {len(listings):4}  ({platform})")

    _write_report(rows, time.time() - t0, cap)


def _write_report(rows, secs, cap):
    by_status   = Counter(r[2] for r in rows)
    by_platform = Counter(r[1] for r in rows)
    total_cars  = sum(r[3] for r in rows)

    groups = defaultdict(list)
    for name, platform, status, count, note in rows:
        groups[status].append((name, platform, count, note))

    lines = []
    lines.append("# Dealer Scrape Report")
    lines.append("")
    lines.append(f"_Generated {time.strftime('%Y-%m-%d %H:%M')} · "
                 f"{len(rows)} dealers · {total_cars} vehicles sampled · "
                 f"page cap {cap} · {secs:.0f}s_")
    lines.append("")
    lines.append("## Summary by outcome")
    lines.append("")
    lines.append("| Status | Dealers | Meaning |")
    lines.append("|---|---|---|")
    meaning = {
        "ok":          "inventory scraped successfully",
        "empty":       "platform detected but returned 0 vehicles",
        "blocked":     "bot/WAF block (Imperva/Cloudflare) — works on a clean IP",
        "unreachable": "dead domain / DNS / timeout",
        "unsupported": "JS-only or unrecognized platform / no website",
    }
    for status, cnt in by_status.most_common():
        lines.append(f"| {status} | {cnt} | {meaning.get(status,'')} |")
    lines.append("")
    lines.append("## Summary by platform")
    lines.append("")
    lines.append("| Platform | Dealers |")
    lines.append("|---|---|")
    for plat, cnt in by_platform.most_common():
        lines.append(f"| {plat} | {cnt} |")
    lines.append("")

    order = ["ok", "blocked", "empty", "unsupported", "unreachable", "none"]
    for status in sorted(groups, key=lambda s: order.index(s) if s in order else 99):
        items = sorted(groups[status], key=lambda x: -x[2])
        lines.append(f"## {status.upper()} ({len(items)})")
        lines.append("")
        lines.append("| Dealer | Platform | Cars | Note |")
        lines.append("|---|---|---|---|")
        for name, platform, count, note in items:
            safe = (note or "").replace("|", "/")
            lines.append(f"| {name} | {platform} | {count} | {safe} |")
        lines.append("")

    with open("DEALER_SCRAPE_REPORT.md", "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    print(f"\n{'='*50}")
    print("OUTCOME:", dict(by_status))
    print("PLATFORM:", dict(by_platform))
    print(f"Vehicles sampled: {total_cars}")
    print("Wrote DEALER_SCRAPE_REPORT.md")


if __name__ == "__main__":
    main()
