"""
Car Search Configuration — loaded from config.json.
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
    for key in ("search", "location", "sources"):
        if key in data:
            existing[key] = data[key]
    with open(_cfg_path, "w") as f:
        json.dump(existing, f, indent=4)


_data = _load()

SEARCH           = _data["search"]
LOCATION         = _data["location"]
SOURCES          = _data["sources"]
DUPLICATE        = _data.get("duplicate", {"use_vin": True, "fuzzy_threshold": 85})
DB_PATH          = _data.get("db_path", "data/cars.db")
AUTO_REFRESH_HOURS = _data.get("auto_refresh_hours", 6)
