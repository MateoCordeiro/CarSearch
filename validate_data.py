"""Data-quality audit of scraped dealer listings.
Read-only. Reports field completeness (overall + per platform), sanity checks,
and sample rows so we can see how accurately each platform is being parsed."""
import sqlite3, re
from collections import defaultdict

conn = sqlite3.connect("data/cars.db")
conn.row_factory = sqlite3.Row
c = conn.cursor()

FIELDS = ["year", "make", "model", "trim", "price", "mileage", "vin",
          "exterior_color", "image_url"]

def pct(n, d): return f"{100*n/d:4.0f}%" if d else "  — "

# Pull all active dealer listings joined to their dealer's platform
rows = c.execute("""
    SELECT l.*, d.platform AS platform
    FROM listings l LEFT JOIN dealerships d ON l.dealership_id = d.id
    WHERE l.is_active = 1 AND l.source = 'dealer'
""").fetchall()
rows = [dict(r) for r in rows]
print(f"Total active dealer listings: {len(rows)}\n")

# ── Completeness by platform ──────────────────────────────────
by_plat = defaultdict(list)
for r in rows:
    by_plat[r.get("platform") or "(none/unknown)"].append(r)

print("FIELD COMPLETENESS BY PLATFORM")
hdr = "platform".ljust(16) + "n".rjust(6) + "  " + "".join(f.split('_')[0][:7].rjust(8) for f in FIELDS)
print(hdr)
print("-" * len(hdr))
for plat in sorted(by_plat, key=lambda p: -len(by_plat[p])):
    rs = by_plat[plat]
    line = plat[:15].ljust(16) + str(len(rs)).rjust(6) + "  "
    for f in FIELDS:
        filled = sum(1 for r in rs if r.get(f) not in (None, "", 0))
        line += pct(filled, len(rs)).rjust(8)
    print(line)

# ── Sanity checks ─────────────────────────────────────────────
print("\nSANITY CHECKS")
prices  = [r["price"] for r in rows if r["price"]]
miles   = [r["mileage"] for r in rows if r["mileage"]]
years   = [r["year"] for r in rows if r["year"]]
def stats(xs):
    xs = sorted(xs)
    return f"min={xs[0]:,} med={xs[len(xs)//2]:,} max={xs[-1]:,}" if xs else "none"
print(f"  price:   n={len(prices)}  {stats(prices)}")
print(f"           $0/null: {sum(1 for r in rows if not r['price'])}  | <$1000: {sum(1 for p in prices if p<1000)}  | >$200k: {sum(1 for p in prices if p>200000)}")
print(f"  mileage: n={len(miles)}  {stats(miles)}")
print(f"           null: {sum(1 for r in rows if not r['mileage'])}  | >300k mi: {sum(1 for m in miles if m>300000)}")
print(f"  year:    {stats(years)}  | future(>2026): {sum(1 for y in years if y>2026)} | old(<1995): {sum(1 for y in years if y<1995)}")

# ── VIN quality ───────────────────────────────────────────────
vin_re = re.compile(r"^[A-HJ-NPR-Z0-9]{17}$")
vins = [r["vin"] for r in rows if r["vin"]]
valid = [v for v in vins if vin_re.match(v.upper())]
print(f"\nVIN: {len(vins)} present ({pct(len(vins),len(rows))} of listings), {len(valid)} valid 17-char")
from collections import Counter
dupe_vins = {v:n for v,n in Counter(valid).items() if n>1}
print(f"     {len(dupe_vins)} VINs appear on >1 listing (cross-listed or dup rows)")

# ── Per-platform sample rows ──────────────────────────────────
print("\nSAMPLE ROWS PER PLATFORM")
for plat in sorted(by_plat, key=lambda p: -len(by_plat[p])):
    rs = [r for r in by_plat[plat] if r.get("make")][:2] or by_plat[plat][:2]
    print(f"\n[{plat}]")
    for r in rs:
        print(f"  {r.get('year')} {r.get('make')} {r.get('model')} {r.get('trim')} | "
              f"${r.get('price')} | {r.get('mileage')}mi | vin={r.get('vin')} | "
              f"color={r.get('exterior_color')} | img={'Y' if r.get('image_url') else 'N'}")
        print(f"     url: {(r.get('url') or '')[:90]}")

conn.close()
