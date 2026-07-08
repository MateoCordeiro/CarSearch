"""
Capture real SRP HTML snapshots into tests/fixtures/ so the extractor tests can
run offline and deterministically. Re-run when a platform's markup changes.

Run: python tests/capture_fixtures.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scrapers.dealers import DealerScraper

FIX = os.path.join(os.path.dirname(__file__), "fixtures")

# one representative dealer per platform we parse
TARGETS = {
    "carsforsale_signature":          "https://www.signatureautos.com/inventory",
    "dealercarsearch_m1":             "https://m1atx.com/inventory",
    "vehicledetails_mazdageorgetown": "https://www.mazdageorgetown.com/used-inventory/index.htm",
    "inlinejson_austineautos":        "https://www.austineautos.com/inventory?limit=1000",
}


def main():
    os.makedirs(FIX, exist_ok=True)
    s = DealerScraper()
    s.delay_range = (0.3, 0.8)
    for name, url in TARGETS.items():
        r = s._get_retry(url, retries=3)
        if r and r.status_code == 200 and len(r.text) > 5000:
            with open(os.path.join(FIX, name + ".html"), "w", encoding="utf-8") as f:
                f.write(r.text)
            print(f"saved  {name:32} ({len(r.text)//1000}k)")
        else:
            print(f"FAILED {name:32} -> {r.status_code if r else 'no response'}")


if __name__ == "__main__":
    main()
