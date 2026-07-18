"""
First-run bootstrap — prepares the offline data the dealer flow depends on.

A fresh database has an empty `zip_coords` table, and WITHOUT it every
radius/distance calculation silently comes up empty (autonomous discovery's
OSM/registry sources, the legacy TX-directory engine, and scan_inventory's
distance filter all read it). This script fills the gap:

  1. creates the SQLite schema (init_db)
  2. downloads the GeoNames ZIP->lat/lng table once (~2MB) if missing
  3. reports the legacy TX-directory status (optional — see below)

Every step is idempotent — safe to run repeatedly; already-done steps are skipped.

Run:  python bootstrap.py
"""
from database import get_conn, init_db


def main():
    init_db()
    conn = get_conn()
    zc = conn.execute("SELECT COUNT(*) FROM zip_coords").fetchone()[0]
    td = conn.execute("SELECT COUNT(*) FROM tx_directory").fetchone()[0]
    conn.close()

    # 1) ZIP -> coordinates (needed for every radius/distance calculation)
    if zc == 0:
        print("[bootstrap] zip_coords is empty -> downloading GeoNames US ZIP data...")
        from zip_geocode import load_zip_coords, resolve_directory_coords
        load_zip_coords()
        resolve_directory_coords()
    else:
        print(f"[bootstrap] zip_coords OK ({zc} ZIPs).")

    # 2) Legacy TX-only directory (OPTIONAL). The default "Find dealers in
    #    radius" flow is now autonomous discovery (OpenStreetMap + state
    #    dealer-license registries, any US ZIP+radius, no directory needed)
    #    — nothing below is required to use it. tx_directory only matters
    #    for the old TX-only engine, kept reachable for parity comparisons.
    if td == 0:
        print("[bootstrap] tx_directory is empty (fine — autonomous discovery doesn't")
        print("            need it). Only build it if you want the legacy TX-only")
        print("            engine or a parity comparison:")
        print("              python tx_directory.py round-rock austin pflugerville")
        print("              python tx_directory.py            (whole state -- slow)")
    else:
        print(f"[bootstrap] tx_directory OK ({td} dealers, legacy engine only).")
        # re-resolve in case new directory rows were added since last run
        from zip_geocode import resolve_directory_coords
        resolve_directory_coords()

    print("[bootstrap] Done. Start the app with:  python app.py")


if __name__ == "__main__":
    main()
