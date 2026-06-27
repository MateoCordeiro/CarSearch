"""
First-run bootstrap — prepares the offline data the dealer flow depends on.

A fresh database has empty `zip_coords` and `tx_directory` tables, and WITHOUT
them the discover/classify/scan flow silently finds nothing (radius search reads
`tx_directory`; distance math reads `zip_coords`). This script fills the gap:

  1. creates the SQLite schema (init_db)
  2. downloads the GeoNames ZIP->lat/lng table once (~2MB) if missing
  3. reports the dealer-directory status and prints how to build it

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

    # 2) Dealer directory (the source the radius search pulls dealers from)
    if td == 0:
        print("[bootstrap] tx_directory is EMPTY -- build it for your area before")
        print("            using the Dealer Database flow. Examples:")
        print("              python tx_directory.py round-rock austin pflugerville")
        print("              python tx_directory.py            (whole state -- slow)")
    else:
        print(f"[bootstrap] tx_directory OK ({td} dealers).")
        # re-resolve in case new directory rows were added since last run
        from zip_geocode import resolve_directory_coords
        resolve_directory_coords()

    print("[bootstrap] Done. Start the app with:  python app.py")


if __name__ == "__main__":
    main()
