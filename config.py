"""
BumperScraper configuration — loaded from config.json.
Edit config.json directly, or use the dashboard UI.
"""

import json
import os
import sys

# Make console output UTF-8 safe. Several modules print non-ASCII status glyphs
# (✓, →, …); on a default Windows console (cp1252/cp437) those raise
# UnicodeEncodeError and abort the program (e.g. init_db's "✓ Database
# initialized" crashed every CLI tool). config is imported — directly or
# transitively — by every entry point, so reconfiguring here protects them all.
# No-op where stdout is already UTF-8 or doesn't support reconfigure.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

_cfg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")


def _load():
    with open(_cfg_path) as f:
        return json.load(f)


def save_config(data: dict):
    """Merge and persist config changes to config.json."""
    existing = _load()
    for key in ("search", "location", "sources", "discovery"):
        if key in data:
            existing[key] = data[key]
    with open(_cfg_path, "w") as f:
        json.dump(existing, f, indent=4)


_data = _load()

SEARCH           = _data["search"]
LOCATION         = _data["location"]
SOURCES          = _data["sources"]
DUPLICATE        = _data.get("duplicate", {"fuzzy_threshold": 85})
DB_PATH          = _data.get("db_path", "data/cars.db")
AUTO_REFRESH_HOURS = _data.get("auto_refresh_hours", 6)

# Autonomous dealer-discovery round. Every key has a .get() default because
# existing config.json files (pre-discovery installs) won't have this block
# at all — see docs/PLAN-discovery.md "Config read at call time".
_discovery_data = _data.get("discovery", {})
DISCOVERY = {
    "sources": {
        "osm":      _discovery_data.get("sources", {}).get("osm", True),
        "registry": _discovery_data.get("sources", {}).get("registry", True),
        "places":   _discovery_data.get("sources", {}).get("places", False),
    },
    "google_places_key":          _discovery_data.get("google_places_key", ""),
    "places_call_budget_per_run": _discovery_data.get("places_call_budget_per_run", 300),
    # Off by default: this is the one discovery source that impersonates a
    # browser (DuckDuckGo blocks the honest UA used everywhere else in
    # discovery/) rather than identifying honestly — opt-in, not opt-out.
    "web_search_fallback":        _discovery_data.get("web_search_fallback", False),
    "registry_refresh_days":      _discovery_data.get("registry_refresh_days", 7),
}
