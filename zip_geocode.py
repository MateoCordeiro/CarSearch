"""
Populate the offline zip_coords table (ZIP -> lat/lng) from the free GeoNames
US postal dataset, then resolve coordinates onto tx_directory rows.

One-time-ish: downloads ~2MB once, works offline afterward. No heavy deps.

Run:  python zip_geocode.py
"""
import io
import zipfile

from curl_cffi import requests as creq
from database import get_conn, init_db

GEONAMES_US = "https://download.geonames.org/export/zip/US.zip"


def load_zip_coords():
    init_db()
    print("Downloading GeoNames US postal data…", flush=True)
    r = creq.get(GEONAMES_US, impersonate="chrome", timeout=60)
    r.raise_for_status()

    rows = []
    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        with zf.open("US.txt") as f:
            for line in io.TextIOWrapper(f, encoding="utf-8"):
                # country, postal, place, admin1, admin1_code, admin2, admin2_code,
                # admin3, admin3_code, latitude, longitude, accuracy
                parts = line.rstrip("\n").split("\t")
                if len(parts) < 11:
                    continue
                zip_code, city, state = parts[1], parts[2], parts[4]
                try:
                    lat, lng = float(parts[9]), float(parts[10])
                except ValueError:
                    continue
                rows.append((zip_code, lat, lng, city, state))

    conn = get_conn()
    conn.executemany(
        "INSERT OR REPLACE INTO zip_coords (zip, lat, lng, city, state) VALUES (?,?,?,?,?)",
        rows,
    )
    conn.commit()
    n = conn.execute("SELECT COUNT(*) FROM zip_coords").fetchone()[0]
    conn.close()
    print(f"Loaded {len(rows)} US ZIPs ({n} in table).", flush=True)


def resolve_directory_coords():
    """Fill tx_directory.lat/lng from zip_coords by ZIP."""
    conn = get_conn()
    conn.execute("""
        UPDATE tx_directory
        SET lat = (SELECT lat FROM zip_coords WHERE zip_coords.zip = substr(tx_directory.zip,1,5)),
            lng = (SELECT lng FROM zip_coords WHERE zip_coords.zip = substr(tx_directory.zip,1,5))
        WHERE zip IS NOT NULL AND zip != ''
    """)
    conn.commit()
    got = conn.execute("SELECT COUNT(*) FROM tx_directory WHERE lat IS NOT NULL").fetchone()[0]
    tot = conn.execute("SELECT COUNT(*) FROM tx_directory").fetchone()[0]
    conn.close()
    print(f"Resolved coordinates for {got}/{tot} directory dealers.", flush=True)


if __name__ == "__main__":
    load_zip_coords()
    resolve_directory_coords()
