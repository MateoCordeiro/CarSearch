"""
Orchestrator: runs the query-based scrapers and saves results to the database.

Entry point:
  run_search(config, sources) -- query-based search for AutoTrader, Craigslist, etc.

Dealer-website inventory is handled by the discover→classify→scan pipeline in
dealer_ops.py, not here.
"""

import time
from database import upsert_listing, get_conn


def _count_listings():
    conn = get_conn()
    n = conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0]
    conn.close()
    return n


def run_search(config: dict, sources: dict, progress_cb=None):
    """Run query-based scrapers (AutoTrader, Craigslist, etc.) with current search config."""
    def _cb(source, pct):
        if progress_cb:
            progress_cb(source, pct)

    source_order = [
        ("autotrader", "scrapers.autotrader", "AutoTraderScraper"),
        ("cargurus",   "scrapers.cargurus",   "CarGurusScraper"),
        ("carmax",     "scrapers.carmax",      "CarMaxScraper"),
        ("craigslist", "scrapers.craigslist",  "CraigslistScraper"),
    ]

    enabled_sources = [s for s, _, _ in source_order if sources.get(s, False)]
    total = max(len(enabled_sources), 1)

    for i, (name, module_path, class_name) in enumerate(source_order):
        if not sources.get(name, False):
            continue

        pct = int((i / total) * 85) + 5
        _cb(name, pct)

        start       = time.time()
        found_count = 0
        error_msg   = None
        status      = "success"
        rows_before = _count_listings()

        try:
            import importlib
            module   = importlib.import_module(module_path)
            cls      = getattr(module, class_name)
            scraper  = cls()
            listings = scraper.search(config)

            for listing_data in listings:
                if not listing_data or not listing_data.get("url"):
                    continue
                upsert_listing(listing_data)
                found_count += 1

        except Exception as e:
            error_msg = str(e)
            status    = "error"
            print(f"[{name}] Error: {e}")

        # listings_new = rows actually inserted (upserts of already-known URLs
        # don't count), so the log reflects genuinely new inventory.
        new_count = _count_listings() - rows_before
        duration  = time.time() - start
        conn = get_conn()
        conn.execute("""
            INSERT INTO scrape_log (source, status, listings_found, listings_new, error_msg, duration_sec)
            VALUES (?,?,?,?,?,?)
        """, (name, status, found_count, new_count, error_msg, round(duration, 2)))
        conn.commit()
        conn.close()

    _cb("done", 90)
    print("✓ Search finished")
