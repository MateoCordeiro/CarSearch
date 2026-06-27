"""
CarMax scraper.
CarMax has a public JSON API — clean and reliable.
"""

import re
from .base import BaseScraper


class CarMaxScraper(BaseScraper):
    name = "carmax"
    API_URL = "https://www.carmax.com/cars/api/search/run"

    def search(self, config: dict) -> list:
        listings = []
        skip = 0
        take = 24

        while True:
            params = {
                "uri":        f"/cars/{config['make']}/{config['model']}",
                "skip":       skip,
                "take":       take,
                "zipcode":    config["zip"],
                "radius":     config.get("radius_mi", 75),
                "year-min":   config.get("year_min", 1990),
                "year-max":   config.get("year_max", 2027),
                "price-max":  config.get("price_max", 0),
                "miles-max":  config.get("mileage_max", 0),
                "sort":       "price-asc",
            }
            if config.get("price_min"):
                params["price-min"] = config["price_min"]

            resp = self._get(self.API_URL, params=params,
                             headers={"Accept": "application/json"})
            if not resp:
                break

            try:
                data = resp.json()
            except Exception:
                break

            items = data.get("items", [])
            if not items:
                break

            for item in items:
                listings.append(self._parse_item(item, config))

            total = data.get("totalCount", 0)
            skip += take
            if skip >= total or skip >= 200:
                break

        print(f"[carmax] Found {len(listings)} listings")
        return listings

    def _parse_item(self, item: dict, config: dict) -> dict:
        stock_num = item.get("stockNumber", "")
        return self._listing(
            source_id   = str(stock_num),
            url         = f"https://www.carmax.com/car/{stock_num}",
            vin         = item.get("vin"),
            year        = item.get("year"),
            make        = item.get("make"),
            model       = item.get("model"),
            trim        = item.get("trim"),
            mileage     = item.get("mileage"),
            price       = item.get("price"),
            exterior_color = item.get("exteriorColor"),
            city        = item.get("storeCity"),
            state       = item.get("storeState"),
            distance_mi = item.get("distance"),
            image_url   = item.get("imageUrl") or item.get("heroImageUrl"),
            raw         = item,
        )
