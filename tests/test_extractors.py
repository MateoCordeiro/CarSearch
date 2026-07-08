"""
Offline regression tests for the dealer extractors, run against the saved HTML
fixtures in tests/fixtures/ (capture them with tests/capture_fixtures.py).

These lock in the behaviour we fixed — real VDP urls, sold filtering, mileage
enrichment, year/make/model from url slugs — so future extractor edits can be
proven safe without hitting live sites.

Run:  python tests/test_extractors.py      (standalone)
  or: pytest tests/                        (if pytest is installed)
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scrapers.dealers import DealerScraper

FIX = os.path.join(os.path.dirname(__file__), "fixtures")
S = DealerScraper()
DEALER = {"name": "test", "website": "https://example.com",
          "city": "Austin", "state": "TX", "zip": "78758"}


def _load(name):
    with open(os.path.join(FIX, name + ".html"), encoding="utf-8") as f:
        return f.read()


def _pct(listings, field):
    if not listings:
        return 0.0
    return sum(1 for l in listings if l.get(field) not in (None, "", 0)) / len(listings)


def _run(html, url, extractor):
    """Mirror the generic SRP pipeline: extract then enrich (url-slug + HTML)."""
    listings = extractor(html, url, DEALER)
    S._enrich_listings(listings, html, url)
    return listings


# ── carsforsale (Signature): JSON-LD has no mileage; HTML enrichment fills it ──
def test_carsforsale_mileage_and_real_urls():
    url = "https://www.signatureautos.com/inventory"
    ls = _run(_load("carsforsale_signature"), url, S._extract_jsonld_inventory)
    assert len(ls) >= 15, f"only {len(ls)} listings"
    assert _pct(ls, "mileage") >= 0.8, f"mileage {_pct(ls,'mileage'):.0%}"
    assert all("#" not in (l.get("url") or "") for l in ls), "synthetic urls present"


# ── DealerCarSearch (M1): real VDP urls built from listingId; sold filtered out ──
def test_dealercarsearch_realurls_and_sold_filtered():
    url = "https://m1atx.com/inventory"
    ls = _run(_load("dealercarsearch_m1"), url, S._extract_inline_vehicle_json)
    assert len(ls) >= 10, f"only {len(ls)} listings"
    assert all("#" not in (l.get("url") or "") for l in ls), "synthetic urls present"
    assert not any(l.get("vin") == "JHMAP1145YT008465" for l in ls), "sold S2000 leaked through"


# ── vehicleDetails (Roger Beasley): no title in call → year/make/model from url ──
def test_vehicledetails_ymm_from_url():
    url = "https://www.mazdageorgetown.com/used-inventory/index.htm"
    ls = _run(_load("vehicledetails_mazdageorgetown"), url, S._extract_vehicledetails_inventory)
    assert len(ls) >= 20, f"only {len(ls)} listings"
    assert _pct(ls, "make") >= 0.9, f"make {_pct(ls,'make'):.0%}"
    assert _pct(ls, "model") >= 0.8, f"model {_pct(ls,'model'):.0%}"


# ── inline-json (Austin Eautos): own vdp url field + HTML mileage ──
def test_inlinejson_vdp_urls_and_mileage():
    url = "https://www.austineautos.com/inventory"
    ls = _run(_load("inlinejson_austineautos"), url, S._extract_inline_vehicle_json)
    assert len(ls) >= 15, f"only {len(ls)} listings"
    assert all("#" not in (l.get("url") or "") for l in ls), "synthetic urls present"
    assert _pct(ls, "mileage") >= 0.8, f"mileage {_pct(ls,'mileage'):.0%}"


# ── pure unit: url-slug parser ──
def test_ymm_from_url_unit():
    assert S._ymm_from_url("/details/used-2009-bentley-continental/125246228") == \
        {"year": 2009, "make": "Bentley", "model": "Continental"}
    assert S._ymm_from_url("/viewdetails/used/x/2025-mazda-cx-30-sport-utility")["model"] == "CX-30"
    assert S._ymm_from_url("/viewdetails/used/x/2021-jeep-grand-cherokee-sport-utility")["model"] == "Grand Cherokee"
    # a year embedded in a VIN must NOT be picked up
    assert S._ymm_from_url("/viewdetails/used/1g1zg5st7pf199927") == {}


if __name__ == "__main__":
    fns = [(n, f) for n, f in sorted(globals().items())
           if n.startswith("test_") and callable(f)]
    passed = skipped = failed = 0
    for name, fn in fns:
        try:
            fn()
            print(f"PASS  {name}"); passed += 1
        except FileNotFoundError as e:
            print(f"SKIP  {name} (missing fixture: {os.path.basename(e.filename)})"); skipped += 1
        except AssertionError as e:
            print(f"FAIL  {name}: {e}"); failed += 1
        except Exception as e:
            print(f"ERROR {name}: {type(e).__name__}: {e}"); failed += 1
    print(f"\n{passed} passed, {failed} failed, {skipped} skipped")
    sys.exit(1 if failed else 0)
