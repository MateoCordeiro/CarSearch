"""
Build the Texas master dealer directory from cardealerdb.com.

Scrapes  region/TX  ->  in/TX/{city}  ->  go/TX/{city}/{dealer}/{id}  and stores
every dealer (name/address/zip/phone/website) in the `tx_directory` table.

This table is the UNTOUCHED reference snapshot — inventory crawls never modify it.

Run:  python tx_directory.py            (full statewide, resumable)
      python tx_directory.py <city>...  (only the named city slugs)

Resumable: dealers already captured (by cardealerdb id) are skipped, so a crash
or stop can be re-run to continue.
"""
import re
import sys
import time
import uuid

from bs4 import BeautifulSoup
from database import get_conn, init_db, log_event
from scrapers.dealers import DealerScraper

BASE = "https://cardealerdb.com"


def get_tx_cities(scraper):
    resp = scraper._get_raw(f"{BASE}/region/TX")
    if not resp or resp.status_code != 200:
        return []
    soup = BeautifulSoup(resp.text, "html.parser")
    slugs = []
    seen = set()
    for a in soup.select("a[href]"):
        m = re.search(r"(?:^|/)in/TX/([^\s\"'?/]+)", a.get("href", ""))
        if m and m.group(1) not in seen:
            seen.add(m.group(1))
            slugs.append(m.group(1))
    return slugs


def get_city_dealers(scraper, slug):
    """Return [(go_url, cardealerdb_id)] for one city."""
    resp = scraper._get_raw(f"{BASE}/in/TX/{slug}")
    if not resp or resp.status_code != 200:
        return []
    soup = BeautifulSoup(resp.text, "html.parser")
    out, seen = [], set()
    for a in soup.select("a[href]"):
        m = re.search(r"(?:^|/)(go/TX/[^\s\"']+?/(\d+))(?:[\s\"']|$)", a.get("href", "") + " ")
        if not m:
            continue
        cid = m.group(2)
        if cid in seen:
            continue
        seen.add(cid)
        out.append((f"{BASE}/{m.group(1)}", cid))
    return out


def _existing_ids(conn):
    return {r[0] for r in conn.execute("SELECT cardealerdb_id FROM tx_directory").fetchall()}


def scrape_directory(cities=None, progress_cb=None, run_id=None):
    init_db()
    run_id = run_id or uuid.uuid4().hex[:8]
    s = DealerScraper()
    s.delay_range = (0.25, 0.6)
    s.timeout = 12

    def cb(msg, pct):
        print(msg, flush=True)
        if progress_cb:
            progress_cb(msg, pct)

    if not cities:
        cb("Fetching Texas city list from cardealerdb…", 2)
        cities = get_tx_cities(s)
    cb(f"{len(cities)} Texas cities to scan", 4)
    log_event(run_id, "directory", "info", detail=f"Starting TX directory: {len(cities)} cities")

    conn = get_conn()
    have = _existing_ids(conn)
    conn.close()

    added = 0
    for ci, slug in enumerate(cities, 1):
        dealers = get_city_dealers(s, slug)
        new_here = 0
        for go_url, cid in dealers:
            if cid in have:
                continue
            detail = s._fetch_dealer_detail(go_url, slug.replace("-", " ").title(), "TX")
            if not detail:
                continue
            conn = get_conn()
            try:
                conn.execute("""
                    INSERT OR IGNORE INTO tx_directory
                        (cardealerdb_id, name, address, city, state, zip, phone, website, source_url)
                    VALUES (?,?,?,?,?,?,?,?,?)
                """, (cid, detail.get("name"), detail.get("address"), detail.get("city"),
                      "TX", detail.get("zip"), detail.get("phone"),
                      detail.get("website"), go_url))
                conn.commit()
            finally:
                conn.close()
            have.add(cid)
            added += 1
            new_here += 1
        pct = 4 + int(ci / max(len(cities), 1) * 92)
        cb(f"[{ci}/{len(cities)}] {slug:24} +{new_here} dealers (total new: {added})", pct)

    conn = get_conn()
    total = conn.execute("SELECT COUNT(*) FROM tx_directory").fetchone()[0]
    with_site = conn.execute("SELECT COUNT(*) FROM tx_directory WHERE website IS NOT NULL AND website != ''").fetchone()[0]
    conn.close()
    log_event(run_id, "directory", "summary",
              detail=f"TX directory: {total} dealers ({with_site} with websites); +{added} this run")
    cb(f"Done. TX directory now holds {total} dealers ({with_site} with websites).", 100)
    return total


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a]
    scrape_directory(cities=args or None)
