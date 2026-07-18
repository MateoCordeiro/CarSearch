"""
discovery/registry.py — state dealer-license registry adapters (plan step 4).

Launch adapters: TX (TxDMV GDN dealer list) and FL (HSMV dealer lists).
Snapshots land in `state_registry`, refreshed every `registry_refresh_days`
(config.DISCOVERY) and served from the snapshot otherwise — these are bulk,
whole-state downloads, not something to refetch per discovery run. Radius
filtering happens client-side in RegistryProvider.find(), resolving each
row's ZIP to a centroid via the existing offline zip_coords table
(registries carry no lat/lng of their own — same ZIP-centroid convention
used everywhere else in this codebase, see database.zip_to_coords).

Confirmed live against the real sources 2026-07-18 — see docs/PROGRESS.md
for exactly what was checked and what changed from the plan's original
assumptions:
  - TX's "downloadable spreadsheet" is a legacy .xls (not CSV, not .xlsx),
    so this needs `xlrd`, not the plan's assumed `openpyxl`.
  - FL's own internal links mix two different URL schemes for the same
    letter-range pages (old permalinks 301-redirect to new ones) — both
    forms are hardcoded below exactly as scraped, not guessed.
"""
import math
import re
import time
from datetime import datetime

import requests
import xlrd
from bs4 import BeautifulSoup
from geopy.distance import geodesic

from database import get_conn, log_event
from discovery.base import Candidate

_HEADERS = {"User-Agent": "BumperScraper/1.x (personal use; +contact)"}
_TIMEOUT = 60


# ── TX: TxDMV GDN dealer list ──────────────────────────────────

TX_LIST_PAGE = "https://texasdmv.my.salesforce-sites.com/dealers/apex/motorvehicledealerliststaging"
TX_BASE = "https://texasdmv.my.salesforce-sites.com"

# TxDMV licenses trailers and motorcycles through the same system; this
# project is about car listings, not the full breadth of everything TxDMV
# regulates, so only car-relevant license types are kept.
TX_RELEVANT_LICENSE_TYPES = {
    "Motor Vehicle",
    "Wholesale Dealer License",
    "Wholesale Motor Vehicle Auction License",
    "Independent Mobility Motor Vehicle Dealer",
}
TX_ACTIVE_STATUSES = {"Active", "Active - Pending Renewal"}


def _tx_download_url():
    """The download link embeds a Salesforce content-file id that rotates
    whenever TxDMV re-uploads the sheet, so it's scraped from the page fresh
    each time rather than hardcoded."""
    r = requests.get(TX_LIST_PAGE, headers=_HEADERS, timeout=_TIMEOUT)
    r.raise_for_status()
    m = re.search(r'href="([^"]*servlet\.FileDownload[^"]*)"', r.text)
    if not m:
        raise RuntimeError("TX DMV dealer-list download link not found on page")
    href = m.group(1).replace("&amp;", "&")
    if href.startswith("http"):
        return href
    if href.startswith("/dealers"):
        return TX_BASE + href
    return TX_BASE + "/dealers/" + href.lstrip("/")


def _tx_rows_from_sheet(header, data_rows):
    """Pure transform: header row + data rows (as plain lists, whatever
    xlrd handed back) -> state_registry-shaped dicts. Split out from
    fetch_tx() so tests can feed it literal fixture rows without needing a
    binary .xls fixture file."""
    idx = {str(h).strip(): i for i, h in enumerate(header)}

    def cell(row, name, default=""):
        i = idx.get(name)
        if i is None or i >= len(row):
            return default
        v = row[i]
        return str(v).strip() if v not in (None, "") else default

    rows = []
    for row in data_rows:
        status = cell(row, "LicenseStatus")
        ltype = cell(row, "LicenseType")
        if status not in TX_ACTIVE_STATUSES or ltype not in TX_RELEVANT_LICENSE_TYPES:
            continue
        address = cell(row, "PhysicalAddress")
        addr2 = cell(row, "PhysAddressTwo")
        if addr2:
            address = f"{address} {addr2}".strip()
        rows.append({
            "license_no": cell(row, "LicenseNumber"),
            "name": cell(row, "BusinessName"),
            "dba": cell(row, "DBAName"),
            "address": address,
            "city": cell(row, "City"),
            "zip": cell(row, "Zip"),
            "phone": cell(row, "Phone"),
            "email": cell(row, "BusinessEmail"),
            "license_type": ltype,
        })
    return rows


def fetch_tx():
    """Download and parse the live TxDMV GDN dealer spreadsheet."""
    url = _tx_download_url()
    r = requests.get(url, headers=_HEADERS, timeout=_TIMEOUT * 3)
    r.raise_for_status()
    wb = xlrd.open_workbook(file_contents=r.content)
    sh = wb.sheet_by_index(0)
    header = [sh.cell_value(0, c) for c in range(sh.ncols)]
    data_rows = [[sh.cell_value(r_i, c) for c in range(sh.ncols)] for r_i in range(1, sh.nrows)]
    return _tx_rows_from_sheet(header, data_rows)


# ── FL: HSMV independent/wholesale/auction/salvage dealer lists ──

# "Independent, Wholesale, Auction and Salvage" — the closest FL category to
# "dealer license = dealer" for used-car lots. FL's separate "Franchise"
# (new-car) list is NOT included here; OSM already finds franchise lots via
# shop=car regardless of license category, so this is a gap only for the
# state_registry snapshot itself, not for scan coverage. See docs/PROGRESS.md.
#
# The site's own navigation mixes an older and a newer URL scheme for this
# page group (verified live 2026-07-18: the older links 301-redirect to the
# newer ones) — every URL below is copied verbatim from the real index
# page's hrefs, not guessed or normalized to one scheme.
FL_LIST_PAGES = [
    "https://www.flhsmv.gov/motor-vehicles-tags-titles/dealers-installers-manufacturers-distributors-and-importers/motor-vehicle-recreational-vehicle-and-mobile-home-dealer-broker-licenses/list-of-licensed-dealers/licensed-independent-wholesale-auction-and-salvage-dealers/wholesale-auction-salvage-dealers-1-b/",
    "https://www.flhsmv.gov/motor-vehicles-tags-titles/dealers-installers-manufacturers-distributors-and-importers/motor-vehicle-recreational-vehicle-and-mobile-home-dealer-broker-licenses/list-of-licensed-dealers/licensed-independent-wholesale-auction-and-salvage-dealers/wholesale-auction-salvage-dealers-c-e/",
    "https://www.flhsmv.gov/motor-vehicles-tags-titles/dealers-installers-manufacturers-distributors-importers/mv-rv-mh-dealer-broker-licenses/list-licensed-dealers/wholesale-auction-salvage-dealers/list-of-licensed-independent-wholesale-auction-and-salvage-dealers-f-i/",
    "https://www.flhsmv.gov/motor-vehicles-tags-titles/dealers-installers-manufacturers-distributors-and-importers/motor-vehicle-recreational-vehicle-and-mobile-home-dealer-broker-licenses/list-of-licensed-dealers/licensed-independent-wholesale-auction-and-salvage-dealers/wholesale-auction-salvage-dealers-j-l/",
    "https://www.flhsmv.gov/motor-vehicles-tags-titles/dealers-installers-manufacturers-distributors-importers/mv-rv-mh-dealer-broker-licenses/list-licensed-dealers/wholesale-auction-salvage-dealers/list-of-licensed-independent-wholesale-auction-and-salvage-dealers-m-q/",
    "https://www.flhsmv.gov/motor-vehicles-tags-titles/dealers-installers-manufacturers-distributors-and-importers/motor-vehicle-recreational-vehicle-and-mobile-home-dealer-broker-licenses/list-of-licensed-dealers/licensed-independent-wholesale-auction-and-salvage-dealers/wholesale-auction-salvage-dealers-r-s/",
    "https://www.flhsmv.gov/motor-vehicles-tags-titles/dealers-installers-manufacturers-distributors-importers/mv-rv-mh-dealer-broker-licenses/list-licensed-dealers/wholesale-auction-salvage-dealers/list-of-licensed-independent-wholesale-auction-and-salvage-dealers-t-z/",
]


def _parse_fl_table(html):
    """html.parser + find_all ONLY — never .select() (fatal CPython 3.14
    soupsieve crash, see CLAUDE.md). Matches tables by header content
    rather than position/id, since a TablePress id is WordPress-internal
    and could change on the next content edit."""
    soup = BeautifulSoup(html, "html.parser")
    rows = []
    for table in soup.find_all("table"):
        head_row = table.find("tr")
        if not head_row:
            continue
        headers = [th.get_text(strip=True).upper() for th in head_row.find_all("th")]
        if "DEALER NAME" not in headers:
            continue
        idx = {h: i for i, h in enumerate(headers)}

        def cell(cells, name, default=""):
            i = idx.get(name)
            if i is None or i >= len(cells):
                return default
            return cells[i].get_text(strip=True)

        body = table.find("tbody") or table
        for tr in body.find_all("tr"):
            cells = tr.find_all("td")
            if not cells:
                continue
            name = cell(cells, "DEALER NAME")
            if not name:
                continue
            rows.append({
                "license_no": cell(cells, "LIC NUM"),
                "name": name,
                "dba": "",
                "address": cell(cells, "LOCATION ADDRESS"),
                "city": cell(cells, "LOCATION CITY"),
                "zip": cell(cells, "LOCATION ZIP"),
                "phone": cell(cells, "PHONE"),
                "email": cell(cells, "EMAIL ADDRESS"),
                "license_type": cell(cells, "LIC TYPE"),
            })
    return rows


def fetch_fl():
    """Fetch all 7 letter-range pages and concatenate. The delay between
    requests is basic courtesy to a state government site — 7 sequential
    GETs of static content pages, not a scraping burst."""
    rows = []
    for i, url in enumerate(FL_LIST_PAGES):
        if i:
            time.sleep(0.5)
        try:
            r = requests.get(url, headers=_HEADERS, timeout=_TIMEOUT)
            r.raise_for_status()
        except Exception as e:
            print(f"[registry:FL] {url} failed: {e}")
            continue
        rows.extend(_parse_fl_table(r.text))
    return rows


STATE_ADAPTERS = {"TX": fetch_tx, "FL": fetch_fl}


# ── snapshot storage + radius lookup ──────────────────────────

def _snapshot_age_days(conn, state):
    row = conn.execute(
        "SELECT captured_at FROM state_registry WHERE state=? ORDER BY captured_at DESC LIMIT 1",
        (state,),
    ).fetchone()
    if not row or not row["captured_at"]:
        return None
    try:
        captured = datetime.fromisoformat(row["captured_at"])
    except ValueError:
        return None
    return (datetime.utcnow() - captured).days


def snapshot_state(state, refresh_days=7, run_id=None):
    """Ensure state_registry has a snapshot for `state` no older than
    refresh_days. Fails soft: an adapter exception is logged and whatever
    snapshot already exists (possibly stale, possibly none) is left in
    place — a broken source must never delete data it can't replace."""
    adapter = STATE_ADAPTERS.get(state)
    if not adapter:
        return
    conn = get_conn()
    age = _snapshot_age_days(conn, state)
    if age is not None and age < refresh_days:
        conn.close()
        return
    try:
        rows = adapter()
    except Exception as e:
        print(f"[registry:{state}] snapshot refresh failed, keeping existing data: {e}")
        log_event(run_id, "discover", "info", detail=f"registry:{state} refresh failed: {e}")
        conn.close()
        return
    conn.execute("DELETE FROM state_registry WHERE state=?", (state,))
    conn.executemany(
        "INSERT INTO state_registry (state, license_no, name, dba, address, city, zip, "
        "phone, email, license_type) VALUES (?,?,?,?,?,?,?,?,?,?)",
        [(state, r["license_no"], r["name"], r["dba"], r["address"], r["city"], r["zip"],
          r["phone"], r["email"], r["license_type"]) for r in rows],
    )
    conn.commit()
    conn.close()
    log_event(run_id, "discover", "info", detail=f"registry:{state} snapshot refreshed, {len(rows)} rows")


def states_in_radius(lat, lng, radius_mi):
    """Distinct US states with at least one ZIP centroid inside the radius —
    same bbox-prefilter-then-geodesic technique as osm.nearest_zip."""
    dlat = radius_mi / 69.0 + 0.1
    dlng = dlat / max(0.15, math.cos(math.radians(lat)))
    conn = get_conn()
    rows = conn.execute(
        "SELECT DISTINCT state, lat, lng FROM zip_coords "
        "WHERE lat BETWEEN ? AND ? AND lng BETWEEN ? AND ?",
        (lat - dlat, lat + dlat, lng - dlng, lng + dlng),
    ).fetchall()
    conn.close()
    states = set()
    for row in rows:
        if row["state"] and geodesic((lat, lng), (row["lat"], row["lng"])).miles <= radius_mi:
            states.add(row["state"])
    return states


def _zip_coords_map(conn, zips):
    """Batch zip->(lat,lng) lookup for a set of ZIPs in one query. A state
    snapshot can be 15k+ rows; calling database.zip_to_coords() per row
    would open and close that many separate SQLite connections in a tight
    loop for no reason — this does one query instead."""
    zips5 = sorted({str(z)[:5] for z in zips if z})
    if not zips5:
        return {}
    placeholders = ",".join("?" * len(zips5))
    rows = conn.execute(
        f"SELECT zip, lat, lng FROM zip_coords WHERE zip IN ({placeholders})", zips5
    ).fetchall()
    return {row["zip"]: (row["lat"], row["lng"]) for row in rows}


class RegistryProvider:
    name = "registry"

    def find(self, lat, lng, radius_mi, refresh_days=7, run_id=None):
        states = states_in_radius(lat, lng, radius_mi) & STATE_ADAPTERS.keys()
        candidates = []
        for state in states:
            snapshot_state(state, refresh_days=refresh_days, run_id=run_id)
            conn = get_conn()
            rows = conn.execute("SELECT * FROM state_registry WHERE state=?", (state,)).fetchall()
            zip_map = _zip_coords_map(conn, [r["zip"] for r in rows])
            conn.close()
            for row in rows:
                coords = zip_map.get(str(row["zip"] or "")[:5])
                if not coords:
                    continue
                if geodesic((lat, lng), coords).miles > radius_mi:
                    continue
                candidates.append(Candidate(
                    name=row["dba"] or row["name"],
                    address=row["address"],
                    city=row["city"],
                    state=row["state"],
                    zip=row["zip"],
                    lat=coords[0],
                    lng=coords[1],
                    phone=row["phone"],
                    source=f"registry:{state}",
                    source_id=row["license_no"],
                ))
        return candidates
