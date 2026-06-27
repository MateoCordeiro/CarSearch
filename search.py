"""
Orchestrator: runs scrapers and saves results to the database.

Two entry points:
  crawl_dealers(zip, radius)  -- find dealers + store ALL their inventory (no filtering)
  run_search(config, sources) -- query-based search for AutoTrader, Craigslist, etc.
"""

import time
from datetime import datetime
from database import upsert_listing, upsert_dealership, get_conn


def crawl_dealers(zip_code: str, radius_mi: int = 50, progress_cb=None):
    """Crawl all dealer websites within radius and store full inventory.
    No make/model filtering — everything goes into the DB.
    Call this on a schedule (e.g. nightly), not on every search."""
    def _cb(msg, pct):
        if progress_cb:
            progress_cb(msg, pct)

    _cb("Finding dealerships...", 5)
    start = time.time()

    try:
        from scrapers.dealers import DealerScraper
        scraper  = DealerScraper()
        listings = scraper.crawl(zip_code, radius_mi)

        for listing_data in listings:
            if listing_data and listing_data.get("url"):
                upsert_listing(listing_data)

        duration = time.time() - start
        conn = get_conn()
        conn.execute("""
            INSERT INTO scrape_log (source, status, listings_found, listings_new, error_msg, duration_sec)
            VALUES (?,?,?,?,?,?)
        """, ("dealers", "success", len(listings), len(listings), None, round(duration, 2)))
        conn.commit()
        conn.close()

        _cb("Dealer crawl complete", 100)
        print(f"✓ Dealer crawl finished: {len(listings)} listings in {duration:.1f}s")
        return listings

    except Exception as e:
        duration = time.time() - start
        conn = get_conn()
        conn.execute("""
            INSERT INTO scrape_log (source, status, listings_found, listings_new, error_msg, duration_sec)
            VALUES (?,?,?,?,?,?)
        """, ("dealers", "error", 0, 0, str(e), round(duration, 2)))
        conn.commit()
        conn.close()
        print(f"[dealers] Crawl error: {e}")
        _cb(f"Dealer crawl error: {e}", 0)
        return []


def run_search(config: dict, sources: dict, progress_cb=None):
    """Run query-based scrapers (AutoTrader, Craigslist, etc.) with current search config.
    Dealers are excluded here — use crawl_dealers() for those."""
    def _cb(source, pct):
        if progress_cb:
            progress_cb(source, pct)

    # Dealers are handled separately via crawl_dealers()
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

        start     = time.time()
        new_count = 0
        error_msg = None
        status    = "success"

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
                new_count += 1

        except Exception as e:
            error_msg = str(e)
            status    = "error"
            print(f"[{name}] Error: {e}")

        duration = time.time() - start
        conn = get_conn()
        conn.execute("""
            INSERT INTO scrape_log (source, status, listings_found, listings_new, error_msg, duration_sec)
            VALUES (?,?,?,?,?,?)
        """, (name, status, new_count, new_count, error_msg, round(duration, 2)))
        conn.commit()
        conn.close()

    _cb("done", 90)
    print("✓ Search finished")
