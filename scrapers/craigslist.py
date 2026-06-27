"""
Craigslist scraper.
Craigslist has a simple HTML format and covers private sellers too.
"""

import re
from geopy.geocoders import Nominatim
from geopy.distance import geodesic
from bs4 import BeautifulSoup  # html.parser used — no lxml needed
from .base import BaseScraper

# Major Craigslist regions — we pick the ones closest to the search ZIP
CL_REGIONS = [
    ("losangeles", "Los Angeles", 34.0522, -118.2437),
    ("newyork",    "New York",    40.7128,  -74.0060),
    ("chicago",    "Chicago",     41.8781,  -87.6298),
    ("houston",    "Houston",     29.7604,  -95.3698),
    ("phoenix",    "Phoenix",     33.4484, -112.0740),
    ("sfbay",      "San Francisco", 37.7749, -122.4194),
    ("seattle",    "Seattle",     47.6062, -122.3321),
    ("dallas",     "Dallas",      32.7767,  -96.7970),
    ("atlanta",    "Atlanta",     33.7490,  -84.3880),
    ("miami",      "Miami",       25.7617,  -80.1918),
    ("denver",     "Denver",      39.7392, -104.9903),
    ("boston",     "Boston",      42.3601,  -71.0589),
    ("portland",   "Portland",    45.5051, -122.6750),
    ("minneapolis","Minneapolis", 44.9778,  -93.2650),
    ("sandiego",   "San Diego",   32.7157, -117.1611),
    ("nashville",  "Nashville",   36.1627,  -86.7816),
    ("austin",     "Austin",      30.2672,  -97.7431),
    ("charlotte",  "Charlotte",   35.2271,  -80.8431),
    ("lasvegas",   "Las Vegas",   36.1699, -115.1398),
]


class CraigslistScraper(BaseScraper):
    name = "craigslist"

    def __init__(self):
        super().__init__()
        self._geocoder = Nominatim(user_agent="car-search-app")

    def _zip_to_coords(self, zip_code: str):
        try:
            loc = self._geocoder.geocode({"postalcode": zip_code, "country": "US"})
            if loc:
                return loc.latitude, loc.longitude
        except Exception:
            pass
        return None, None

    def _nearby_regions(self, lat, lng, radius_mi):
        if lat is None:
            return ["sfbay", "losangeles"]
        regions = []
        for slug, name, r_lat, r_lng in CL_REGIONS:
            dist = geodesic((lat, lng), (r_lat, r_lng)).miles
            if dist <= radius_mi + 100:
                regions.append((dist, slug))
        regions.sort()
        return [slug for _, slug in regions[:5]]

    def search(self, config: dict) -> list:
        listings = []
        lat, lng = self._zip_to_coords(config["zip"])
        regions = self._nearby_regions(lat, lng, config.get("radius_mi", 75))

        query = f"{config.get('make', '')} {config.get('model', '')}".strip()
        min_year = config.get("year_min", 1990)
        max_year = config.get("year_max", 2027)
        max_price = config.get("price_max", 0)
        max_miles = config.get("mileage_max", 0)

        for region in regions:
            url = f"https://{region}.craigslist.org/search/cta"
            params = {
                "query":           query,
                "min_auto_year":   min_year,
                "max_auto_year":   max_year,
                "sort":            "priceasc",
                "srchType":        "T",
            }
            if max_price:
                params["max_price"] = max_price
            if max_miles:
                params["max_auto_miles"] = max_miles

            resp = self._get(url, params=params)
            if not resp:
                continue

            soup = BeautifulSoup(resp.text, "html.parser")
            results = soup.select("li.result-row") or soup.select("li.cl-search-result")

            for row in results:
                listing = self._parse_row(row, region, lat, lng)
                if listing:
                    listings.append(listing)

            if len(listings) >= 150:
                break

        # Deduplicate by URL within Craigslist results
        seen = set()
        unique = []
        for l in listings:
            if l["url"] not in seen:
                seen.add(l["url"])
                unique.append(l)

        print(f"[craigslist] Found {len(unique)} listings across {len(regions)} regions")
        return unique

    def _parse_row(self, row, region: str, search_lat, search_lng) -> dict:
        try:
            link = row.select_one("a.result-title, a.posting-title")
            if not link:
                return None

            url = link.get("href", "")
            if not url.startswith("http"):
                url = f"https://{region}.craigslist.org{url}"

            title = link.get_text(strip=True)

            price_el = row.select_one(".result-price, .priceinfo")
            price_text = price_el.get_text(strip=True) if price_el else ""
            price = int(re.sub(r"[^\d]", "", price_text)) if price_text else None

            year_match = re.search(r"\b(19[5-9]\d|20[0-2]\d)\b", title)
            year = int(year_match.group(1)) if year_match else None

            miles_match = re.search(r"(\d{1,3}[,\d]*)\s*(?:mi|miles|k)", title, re.I)
            mileage = None
            if miles_match:
                raw = re.sub(r"[^\d]", "", miles_match.group(1))
                mileage = int(raw) * (1000 if "k" in miles_match.group(0).lower() else 1)

            loc_el = row.select_one(".result-hood, .meta .maptag")
            location = loc_el.get_text(strip=True).strip("()") if loc_el else ""

            return self._listing(
                source_id=url.split("/")[-1].replace(".html", ""),
                url=url,
                year=year,
                make=None,
                model=None,
                mileage=mileage,
                price=price,
                city=location or region,
                raw={"title": title, "region": region},
            )
        except Exception:
            return None
