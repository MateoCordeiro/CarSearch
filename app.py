"""
Flask web server for the BumperScraper dashboard.
Run with: python app.py  →  open http://localhost:5000
"""

import threading
from flask import Flask, render_template, jsonify, request

from database import init_db, get_listings, get_stats, get_conn
from duplicates import run_deduplication
from search import run_search
import config as cfg

app = Flask(__name__)

# ── Background job state ──────────────────────────────────────

_search_status = {"running": False, "message": "Idle", "progress": 0}
_search_thread = None


# ── Routes ────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/listings")
def api_listings():
    filters = {
        "make":            request.args.get("make"),
        "model":           request.args.get("model"),
        "year_min":        _int(request.args.get("year_min")),
        "year_max":        _int(request.args.get("year_max")),
        "price_min":       _int(request.args.get("price_min")),
        "price_max":       _int(request.args.get("price_max")),
        "mileage_max":     _int(request.args.get("mileage_max")),
        "source":          request.args.get("source"),
        "transmission":    request.args.get("transmission"),
        "real_price_only": request.args.get("real_price_only") == "1",
        "hide_duplicates": request.args.get("hide_duplicates") == "1",
    }
    filters  = {k: v for k, v in filters.items() if v is not None}
    listings = get_listings(filters)

    # duplicate_count for the "Listed on N sites" badge. ONE grouped query for
    # all active groups, then mapped in Python — was an N+1 (a COUNT(*) per
    # listing = thousands of round-trips and the main cause of slow loads).
    conn = get_conn()
    counts = dict(conn.execute(
        "SELECT duplicate_group_id, COUNT(*) FROM listings "
        "WHERE is_active=1 AND duplicate_group_id IS NOT NULL "
        "GROUP BY duplicate_group_id").fetchall())
    conn.close()
    for l in listings:
        l["duplicate_count"] = counts.get(l.get("duplicate_group_id"), 1)
    return jsonify(listings)


@app.route("/api/duplicates/<int:group_id>")
def api_duplicates(group_id):
    """All active listings of one car (its duplicate group), so the UI can show
    WHICH dealers/sites it's listed on. Cheapest first; the canonical (best)
    listing is flagged is_duplicate=0."""
    conn = get_conn()
    rows = [dict(r) for r in conn.execute("""
        SELECT l.url, l.price, l.mileage, l.source, l.is_duplicate,
               d.name AS dealer_name, d.website AS dealer_website
        FROM listings l LEFT JOIN dealerships d ON l.dealership_id = d.id
        WHERE l.duplicate_group_id = ? AND l.is_active = 1
        ORDER BY (l.price IS NULL), l.price ASC
    """, (group_id,)).fetchall()]
    conn.close()
    return jsonify(rows)


@app.route("/api/stats")
def api_stats():
    return jsonify(get_stats())


@app.route("/api/config")
def api_config():
    return jsonify({
        "search":   cfg.SEARCH,
        "location": cfg.LOCATION,
        "sources":  cfg.SOURCES,
    })


@app.route("/api/config", methods=["POST"])
def api_config_save():
    data = request.json
    if not data:
        return jsonify({"error": "No data"}), 400
    cfg.save_config(data)
    import importlib
    importlib.reload(cfg)
    return jsonify({"ok": True})


# ── Search (query existing DB) ────────────────────────────────

@app.route("/api/search", methods=["POST"])
def api_search():
    """Run query-based search against live sources (AutoTrader, Craigslist etc.)."""
    global _search_thread, _search_status
    if _search_status["running"]:
        return jsonify({"error": "Search already running"}), 409

    def _do_search():
        global _search_status
        _search_status = {"running": True, "message": "Searching...", "progress": 5}
        try:
            import importlib
            importlib.reload(cfg)
            search_cfg = cfg.SEARCH | {"zip": cfg.LOCATION["zip"], "radius_mi": cfg.LOCATION["radius"]}
            run_search(search_cfg, cfg.SOURCES,
                       lambda src, pct: _search_status.update(
                           {"message": f"Searching {src}...", "progress": pct}
                       ))
            _search_status = {"running": False, "message": "Running deduplication...", "progress": 90}
            run_deduplication()
            _search_status = {"running": False, "message": "Done!", "progress": 100}
        except Exception as e:
            _search_status = {"running": False, "message": f"Error: {e}", "progress": 0}

    _search_thread = threading.Thread(target=_do_search, daemon=True)
    _search_thread.start()
    return jsonify({"ok": True})


@app.route("/api/search/status")
def api_search_status():
    return jsonify(_search_status)


# Keep old /api/scrape route as alias for search (backward compat)
@app.route("/api/scrape", methods=["POST"])
def api_scrape():
    return api_search()

@app.route("/api/scrape/status")
def api_scrape_status():
    return api_search_status()


@app.route("/api/dealerships")
def api_dealerships():
    conn = get_conn()
    rows = conn.execute("""
        SELECT d.*,
               (SELECT COUNT(*) FROM listings l
                WHERE l.dealership_id = d.id AND l.is_active = 1) AS active_listings
        FROM dealerships d
        ORDER BY distance_mi ASC NULLS LAST
    """).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


# ── Dealer operations: discover / classify / scan (separated) ─

_jobs = {}   # name -> {running, message, progress, result}


def _run_job(name, fn):
    """Run fn(progress_cb) in a daemon thread, tracking _jobs[name]."""
    if _jobs.get(name, {}).get("running"):
        return False
    _jobs[name] = {"running": True, "message": "Starting…", "progress": 0, "result": None}

    def worker():
        try:
            res = fn(lambda msg, pct: _jobs[name].update({"message": msg, "progress": pct}))
            _jobs[name] = {"running": False, "message": _jobs[name]["message"],
                           "progress": 100, "result": res}
        except Exception as e:
            _jobs[name] = {"running": False, "message": f"Error: {e}", "progress": 0, "result": None}

    threading.Thread(target=worker, daemon=True).start()
    return True


@app.route("/api/dealers/discover", methods=["POST"])
def api_discover():
    import importlib, dealer_ops; importlib.reload(cfg)
    zip_code = cfg.LOCATION["zip"]; radius = cfg.LOCATION["radius"]
    ok = _run_job("discover", lambda cb: dealer_ops.discover_dealers(zip_code, radius, progress_cb=cb))
    return (jsonify({"ok": True}) if ok else (jsonify({"error": "already running"}), 409))


@app.route("/api/dealers/classify", methods=["POST"])
def api_classify():
    scope = (request.json or {}).get("scope", "all")
    ok = _run_job("classify", _classify_job(scope))
    return (jsonify({"ok": True}) if ok else (jsonify({"error": "already running"}), 409))


def _scan_job(cb):
    """Scan in-radius 'ok' dealers, diff inventory, then dedup listings.
    Shared by the manual /api/dealers/scan route and the scheduler so both go
    through the same _run_job lock and can never run concurrently."""
    import importlib, dealer_ops; importlib.reload(cfg)
    res = dealer_ops.scan_inventory(radius_mi=cfg.LOCATION["radius"], progress_cb=cb)
    run_deduplication()
    return res


def _classify_job(scope, resume=True):
    def job(cb):
        import importlib, dealer_ops; importlib.reload(cfg)
        return dealer_ops.classify_dealers(
            scope=scope, zip_code=cfg.LOCATION["zip"], radius_mi=cfg.LOCATION["radius"],
            resume=resume, progress_cb=cb)
    return job


@app.route("/api/dealers/scan", methods=["POST"])
def api_scan():
    ok = _run_job("scan", _scan_job)
    return (jsonify({"ok": True}) if ok else (jsonify({"error": "already running"}), 409))


@app.route("/api/job/<name>/status")
def api_job_status(name):
    return jsonify(_jobs.get(name, {"running": False, "message": "Idle", "progress": 0, "result": None}))


@app.route("/api/directory/stats")
def api_directory_stats():
    conn = get_conn()
    total    = conn.execute("SELECT COUNT(*) FROM tx_directory").fetchone()[0]
    with_web = conn.execute("SELECT COUNT(*) FROM tx_directory WHERE website IS NOT NULL AND website!=''").fetchone()[0]
    cities   = conn.execute("SELECT COUNT(DISTINCT city) FROM tx_directory").fetchone()[0]
    conn.close()
    return jsonify({"total": total, "with_website": with_web, "cities": cities})


@app.route("/api/dealers/lists")
def api_dealer_lists():
    """Working-set dealers split into can-scrape vs can't-scrape."""
    conn = get_conn()
    rows = [dict(r) for r in conn.execute(
        "SELECT name, city, zip, website, platform, scrape_status, scrape_count, scrape_note, distance_mi "
        "FROM dealerships WHERE scrape_status IS NOT NULL ORDER BY scrape_count DESC NULLS LAST, name"
    ).fetchall()]
    conn.close()
    can    = [r for r in rows if r["scrape_status"] == "ok"]
    cannot = [r for r in rows if r["scrape_status"] in ("blocked", "unsupported", "unreachable", "empty", "error")]
    return jsonify({"can_scrape": can, "cannot_scrape": cannot})


@app.route("/api/dealers/quality", methods=["POST"])
def api_dealers_quality():
    """Phase 5: recompute per-dealer data-quality on demand (no scrape) and
    persist scores/flags. Returns {scored, flagged}."""
    import dealer_ops
    return jsonify(dealer_ops.flag_quality())


@app.route("/api/dealers/dedupe", methods=["POST"])
def api_dealers_dedupe():
    """Phase 4.1: collapse dealer rows that serve the same inventory feed under
    different rooftop domains (VIN-overlap based). ?dry=1 for a dry run."""
    import dealer_ops
    dry = request.args.get("dry") in ("1", "true", "yes")
    return jsonify(dealer_ops.dedupe_dealers_by_inventory(apply=not dry))


@app.route("/api/scan-log")
def api_scan_log():
    limit = _int(request.args.get("limit")) or 200
    conn = get_conn()
    rows = [dict(r) for r in conn.execute(
        "SELECT run_type, action, dealer, detail, ran_at FROM scan_log ORDER BY id DESC LIMIT ?",
        (limit,)).fetchall()]
    conn.close()
    return jsonify(rows)


def _int(v):
    try:
        return int(v) if v else None
    except (ValueError, TypeError):
        return None


if __name__ == "__main__":
    init_db()

    if cfg.AUTO_REFRESH_HOURS > 0:
        from apscheduler.schedulers.background import BackgroundScheduler
        scheduler = BackgroundScheduler()

        def _scheduled_refresh():
            # Re-scrape already-classified ('ok') dealers in radius and diff vs
            # the DB (new / sold). Same _scan_job as the manual route, behind the
            # same _run_job lock, so a scheduled and a manual scan can't overlap.
            # On a fresh install with nothing classified yet, this is a safe no-op.
            if not _run_job("scan", _scan_job):
                print("[skip] Scheduled scan skipped — a scan is already running")
        scheduler.add_job(_scheduled_refresh, "interval", hours=cfg.AUTO_REFRESH_HOURS)

        def _scheduled_reclassify():
            # Daily: re-classify dealers in radius (resume=True skips any done in
            # the last 12h) so transiently 'unreachable'/'blocked' dealers — e.g. a
            # site that was 503/WAF-blocked at scan time — automatically recover to
            # 'ok' and get picked up by the next scan, with no manual re-run.
            if not _run_job("classify", _classify_job("radius")):
                print("[skip] Scheduled re-classify skipped — classify already running")
        scheduler.add_job(_scheduled_reclassify, "interval", hours=24)

        scheduler.start()
        print(f"[OK] Auto-refresh every {cfg.AUTO_REFRESH_HOURS}h; re-classify every 24h")

    print("✓ BumperScraper running at http://localhost:5000")
    app.run(debug=False, use_reloader=False)
