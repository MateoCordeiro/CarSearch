"""
Database layer for Car Search.
Uses SQLite — no server setup needed, everything lives in data/cars.db
"""

import sqlite3
import json
from datetime import datetime
from config import DB_PATH


def get_conn():
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row   # rows behave like dicts
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=15000")  # wait out concurrent writers
    return conn


def init_db():
    """Create all tables if they don't exist yet."""
    conn = get_conn()
    c = conn.cursor()

    # ── Dealerships ───────────────────────────────────────────
    c.execute("""
    CREATE TABLE IF NOT EXISTS dealerships (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        name        TEXT NOT NULL,
        address     TEXT,
        city        TEXT,
        state       TEXT,
        zip         TEXT,
        phone       TEXT,
        website     TEXT UNIQUE,
        lat         REAL,
        lng         REAL,
        distance_mi REAL,
        first_seen  TEXT DEFAULT (datetime('now')),
        last_seen   TEXT DEFAULT (datetime('now'))
    )""")

    # ── Listings ──────────────────────────────────────────────
    # One row per listing per source. Same car on AutoTrader AND
    # CarGurus = two rows, linked by duplicate_group_id.
    c.execute("""
    CREATE TABLE IF NOT EXISTS listings (
        id                INTEGER PRIMARY KEY AUTOINCREMENT,
        source            TEXT NOT NULL,       -- 'autotrader','cargurus','carmax','craigslist','dealer','facebook'
        source_id         TEXT,                -- listing ID on source site
        url               TEXT UNIQUE,
        dealership_id     INTEGER REFERENCES dealerships(id),

        -- Vehicle identity
        vin               TEXT,
        year              INTEGER,
        make              TEXT,
        model             TEXT,
        trim              TEXT,
        exterior_color    TEXT,
        interior_color    TEXT,
        mileage           INTEGER,

        -- Price
        price             INTEGER,
        msrp              INTEGER,

        -- Location
        city              TEXT,
        state             TEXT,
        zip               TEXT,
        distance_mi       REAL,

        -- Media
        image_url         TEXT,
        image_urls        TEXT,               -- JSON array

        -- Duplicate tracking
        duplicate_group_id  INTEGER,          -- listings sharing this ID are the same car
        is_duplicate        INTEGER DEFAULT 0, -- 1 = another listing has this car cheaper/closer

        -- Meta
        raw_data          TEXT,               -- JSON blob of full scraped data
        first_seen        TEXT DEFAULT (datetime('now')),
        last_seen         TEXT DEFAULT (datetime('now')),
        is_active         INTEGER DEFAULT 1
    )""")

    # ── Duplicate Groups ──────────────────────────────────────
    c.execute("""
    CREATE TABLE IF NOT EXISTS duplicate_groups (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        vin           TEXT,                   -- null if matched by fuzzy logic
        match_method  TEXT,                   -- 'vin' or 'fuzzy'
        listing_count INTEGER DEFAULT 0,
        created_at    TEXT DEFAULT (datetime('now'))
    )""")

    # ── Search Configs (saved searches) ──────────────────────
    c.execute("""
    CREATE TABLE IF NOT EXISTS search_configs (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        name        TEXT NOT NULL,
        make        TEXT,
        model       TEXT,
        year_min    INTEGER,
        year_max    INTEGER,
        price_min   INTEGER,
        price_max   INTEGER,
        mileage_max INTEGER,
        zip         TEXT,
        radius_mi   INTEGER,
        sources     TEXT,                     -- JSON array
        is_active   INTEGER DEFAULT 1,
        created_at  TEXT DEFAULT (datetime('now'))
    )""")

    # ── Scrape Log ────────────────────────────────────────────
    c.execute("""
    CREATE TABLE IF NOT EXISTS scrape_log (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        source       TEXT,
        status       TEXT,                    -- 'success','error','partial'
        listings_found INTEGER DEFAULT 0,
        listings_new   INTEGER DEFAULT 0,
        error_msg    TEXT,
        duration_sec REAL,
        ran_at       TEXT DEFAULT (datetime('now'))
    )""")

    # ── TX Directory ──────────────────────────────────────────
    # UNTOUCHED master snapshot of every Texas dealer from cardealerdb.com.
    # Populated by tx_directory.py; never modified by inventory crawls.
    c.execute("""
    CREATE TABLE IF NOT EXISTS tx_directory (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        cardealerdb_id  TEXT UNIQUE,     -- numeric id from the /go/.../{id} URL
        name            TEXT,
        address         TEXT,
        city            TEXT,
        state           TEXT DEFAULT 'TX',
        zip             TEXT,
        phone           TEXT,
        website         TEXT,
        source_url      TEXT,            -- cardealerdb detail page
        lat             REAL,            -- resolved from zip_coords
        lng             REAL,
        captured_at     TEXT DEFAULT (datetime('now'))
    )""")

    # ── ZIP coordinates (offline geocoding for radial search) ─
    c.execute("""
    CREATE TABLE IF NOT EXISTS zip_coords (
        zip   TEXT PRIMARY KEY,
        lat   REAL,
        lng   REAL,
        city  TEXT,
        state TEXT
    )""")

    # ── Activity log (dealer discovery + inventory scans) ─────
    c.execute("""
    CREATE TABLE IF NOT EXISTS scan_log (
        id        INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id    TEXT,        -- groups events from one run
        run_type  TEXT,        -- 'directory'|'discover'|'inventory'|'classify'
        action    TEXT,        -- 'dealer_added'|'listing_added'|'listing_sold'|'listing_updated'|'summary'|'info'
        dealer    TEXT,        -- dealership name
        detail    TEXT,        -- vin/url/year-make-model or a summary string
        ran_at    TEXT DEFAULT (datetime('now'))
    )""")

    # Indexes for fast lookups
    c.execute("CREATE INDEX IF NOT EXISTS idx_listings_vin    ON listings(vin)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_listings_source ON listings(source)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_listings_dupgrp ON listings(duplicate_group_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_listings_active ON listings(is_active)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_txdir_city      ON tx_directory(city)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_scanlog_run     ON scan_log(run_id)")

    # ── Migration: transmission on listings ───────────────────
    listing_cols = {r[1] for r in c.execute("PRAGMA table_info(listings)").fetchall()}
    if "transmission" not in listing_cols:
        c.execute("ALTER TABLE listings ADD COLUMN transmission TEXT")

    # ── Migration: per-dealer scrape diagnostics ──────────────
    # Tracks which dealers we can/can't scrape and why.
    existing_cols = {r[1] for r in c.execute("PRAGMA table_info(dealerships)").fetchall()}
    for col, ddl in [
        ("platform",          "TEXT"),    # detected site platform (dealer.com, dealeron, …)
        ("scrape_status",     "TEXT"),    # 'ok' | 'empty' | 'blocked' | 'unreachable' | 'unsupported'
        ("scrape_count",      "INTEGER"), # listings pulled on last attempt
        ("scrape_note",       "TEXT"),    # human-readable reason / detail
        ("scrape_at",         "TEXT"),    # timestamp of last attempt
        ("directory_id",      "INTEGER"), # link to tx_directory row
        ("last_inventory_at", "TEXT"),    # last time inventory was scanned
    ]:
        if col not in existing_cols:
            c.execute(f"ALTER TABLE dealerships ADD COLUMN {col} {ddl}")

    conn.commit()
    conn.close()
    print("✓ Database initialized at", DB_PATH)


def log_event(run_id, run_type, action, dealer=None, detail=None):
    """Append one row to the activity log (scan_log)."""
    conn = get_conn()
    conn.execute(
        "INSERT INTO scan_log (run_id, run_type, action, dealer, detail) VALUES (?,?,?,?,?)",
        (run_id, run_type, action, dealer, (detail or "")[:400]),
    )
    conn.commit()
    conn.close()


def zip_to_coords(zip_code):
    """Look up (lat, lng) for a ZIP from the offline zip_coords table, or None."""
    if not zip_code:
        return None
    z = str(zip_code).strip()[:5]
    conn = get_conn()
    row = conn.execute("SELECT lat, lng FROM zip_coords WHERE zip=?", (z,)).fetchone()
    conn.close()
    if row and row["lat"] is not None:
        return (row["lat"], row["lng"])
    return None


def update_dealer_scrape_status(dealer_id, platform, status, count, note=""):
    """Record the outcome of a scrape attempt for one dealer.
    Preserves a previously-detected platform when this attempt couldn't detect
    one (e.g. a known Dealer Inspire site that's temporarily WAF-blocked)."""
    conn = get_conn()
    if platform in (None, "", "unknown", "none"):
        row = conn.execute("SELECT platform FROM dealerships WHERE id=?", (dealer_id,)).fetchone()
        if row and row[0] and row[0] not in ("unknown", "none", ""):
            platform = row[0]
    conn.execute(
        "UPDATE dealerships SET platform=?, scrape_status=?, scrape_count=?, "
        "scrape_note=?, scrape_at=? WHERE id=?",
        (platform, status, count, note[:300], datetime.utcnow().isoformat(), dealer_id),
    )
    conn.commit()
    conn.close()


# ── CRUD helpers ──────────────────────────────────────────────

def _bounded(v, lo, hi):
    """Coerce v to an int within [lo, hi], else None. Protects against garbage
    numbers from HTML parsers (e.g. a phone number scraped as a price) that would
    otherwise overflow SQLite's 64-bit INTEGER column."""
    try:
        n = int(v)
    except (TypeError, ValueError):
        return None
    return n if lo <= n <= hi else None


def _sanitize_listing(data: dict):
    """Clamp numeric fields to plausible ranges before they hit the DB."""
    data["year"]    = _bounded(data.get("year"), 1900, 2100)
    data["price"]   = _bounded(data.get("price"), 0, 10_000_000)
    data["msrp"]    = _bounded(data.get("msrp"), 0, 10_000_000)
    data["mileage"] = _bounded(data.get("mileage"), 0, 2_000_000)
    return data


def upsert_listing(data: dict) -> int:
    """Insert or update a listing. Returns the row id."""
    data = _sanitize_listing(data)
    conn = get_conn()
    c = conn.cursor()
    now = datetime.utcnow().isoformat()

    # Check if URL already exists
    existing = c.execute("SELECT id FROM listings WHERE url = ?", (data.get("url"),)).fetchone()

    if existing:
        # COALESCE(?, col): overwrite when the re-scrape has a value, keep the
        # existing value when it's null. Lets a richer parser backfill fields
        # (vin/trim/color) without ever nulling-out previously good data.
        c.execute("""
            UPDATE listings SET
                vin            = COALESCE(?, vin),
                year           = COALESCE(?, year),
                make           = COALESCE(?, make),
                model          = COALESCE(?, model),
                trim           = COALESCE(?, trim),
                exterior_color = COALESCE(?, exterior_color),
                transmission   = COALESCE(?, transmission),
                price          = COALESCE(?, price),
                mileage        = COALESCE(?, mileage),
                image_url      = COALESCE(?, image_url),
                last_seen=?, is_active=1, raw_data=?
            WHERE id=?
        """, (data.get("vin"), data.get("year"), data.get("make"), data.get("model"),
              data.get("trim"), data.get("exterior_color"), data.get("transmission"),
              data.get("price"), data.get("mileage"), data.get("image_url"), now,
              json.dumps(data.get("raw", {})), existing["id"]))
        row_id = existing["id"]
    else:
        c.execute("""
            INSERT INTO listings
                (source, source_id, url, vin, year, make, model, trim,
                 exterior_color, transmission, mileage, price, msrp, city, state, zip,
                 distance_mi, image_url, image_urls, raw_data, dealership_id)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            data.get("source"), data.get("source_id"), data.get("url"),
            data.get("vin"), data.get("year"), data.get("make"), data.get("model"),
            data.get("trim"), data.get("exterior_color"), data.get("transmission"),
            data.get("mileage"),
            data.get("price"), data.get("msrp"), data.get("city"), data.get("state"),
            data.get("zip"), data.get("distance_mi"), data.get("image_url"),
            json.dumps(data.get("image_urls", [])), json.dumps(data.get("raw", {})),
            data.get("dealership_id")
        ))
        row_id = c.lastrowid

    conn.commit()
    conn.close()
    return row_id


def upsert_dealership(data: dict) -> int:
    conn = get_conn()
    c = conn.cursor()
    existing = c.execute("SELECT id FROM dealerships WHERE website = ?", (data.get("website"),)).fetchone()
    now = datetime.utcnow().isoformat()

    if existing:
        c.execute("UPDATE dealerships SET last_seen=?, distance_mi=? WHERE id=?",
                  (now, data.get("distance_mi"), existing["id"]))
        row_id = existing["id"]
    else:
        c.execute("""
            INSERT INTO dealerships (name, address, city, state, zip, phone, website, lat, lng, distance_mi)
            VALUES (?,?,?,?,?,?,?,?,?,?)
        """, (data.get("name"), data.get("address"), data.get("city"), data.get("state"),
              data.get("zip"), data.get("phone"), data.get("website"),
              data.get("lat"), data.get("lng"), data.get("distance_mi")))
        row_id = c.lastrowid

    conn.commit()
    conn.close()
    return row_id


def get_listings(filters: dict = None) -> list:
    """Fetch active listings, optionally filtered."""
    conn = get_conn()
    c = conn.cursor()
    sql = """
        SELECT l.*, d.name AS dealer_name, d.website AS dealer_website
        FROM listings l
        LEFT JOIN dealerships d ON l.dealership_id = d.id
        WHERE l.is_active = 1
    """
    params = []
    if filters:
        if filters.get("make"):
            sql += " AND LOWER(l.make) = LOWER(?)"
            params.append(filters["make"])
        if filters.get("model"):
            sql += " AND LOWER(l.model) LIKE LOWER(?)"
            params.append(f"%{filters['model']}%")
        if filters.get("year_min"):
            sql += " AND l.year >= ?"
            params.append(filters["year_min"])
        if filters.get("year_max"):
            sql += " AND l.year <= ?"
            params.append(filters["year_max"])
        if filters.get("price_max"):
            sql += " AND l.price <= ?"
            params.append(filters["price_max"])
        if filters.get("price_min"):
            sql += " AND l.price >= ?"
            params.append(filters["price_min"])
        if filters.get("mileage_max"):
            sql += " AND l.mileage <= ?"
            params.append(filters["mileage_max"])
        if filters.get("source"):
            sql += " AND l.source = ?"
            params.append(filters["source"])
        if filters.get("transmission") == "manual":
            # stick-shift only: says "manual" but not "automated/automatic"
            sql += " AND LOWER(l.transmission) LIKE '%manual%' AND LOWER(l.transmission) NOT LIKE '%automat%'"
        elif filters.get("transmission") == "automatic":
            # everything 2-pedal: has a transmission and isn't a true manual
            # (covers Automatic, CVT, dual-clutch, PDK, etc.)
            sql += (" AND l.transmission IS NOT NULL AND l.transmission != ''"
                    " AND NOT (LOWER(l.transmission) LIKE '%manual%'"
                    " AND LOWER(l.transmission) NOT LIKE '%automat%')")
        if filters.get("real_price_only"):
            # Just drop missing prices and obvious payment fragments (monthly /
            # down payments like "$225" or "$530"). Deliberately low so it never
            # hides a genuinely cheap car — a real used car clears $1,000.
            sql += " AND l.price IS NOT NULL AND l.price >= 1000"
        if filters.get("hide_duplicates"):
            sql += " AND l.is_duplicate = 0"

    sql += " ORDER BY l.price ASC"
    rows = c.execute(sql, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_stats() -> dict:
    conn = get_conn()
    c = conn.cursor()
    stats = {}
    stats["total"]      = c.execute("SELECT COUNT(*) FROM listings WHERE is_active=1").fetchone()[0]
    stats["duplicates"] = c.execute("SELECT COUNT(*) FROM listings WHERE is_active=1 AND is_duplicate=1").fetchone()[0]
    stats["dealerships"]= c.execute("SELECT COUNT(*) FROM dealerships").fetchone()[0]
    stats["sources"]    = {r[0]: r[1] for r in c.execute(
        "SELECT source, COUNT(*) FROM listings WHERE is_active=1 GROUP BY source"
    ).fetchall()}
    stats["last_scrape"]= c.execute(
        "SELECT ran_at FROM scrape_log ORDER BY id DESC LIMIT 1"
    ).fetchone()
    stats["last_scrape"] = stats["last_scrape"][0] if stats["last_scrape"] else "Never"
    conn.close()
    return stats
