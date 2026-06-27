"""
CarGurus scraper.
CarGurus exposes a JSON API endpoint used by their search page.
"""

import json
import re
from .base import BaseScraper


class CarGurusScraper(BaseScraper):
    name = "cargurus"
    SEARCH_URL = "https://www.cargurus.com/Cars/searchResults.action"
    API_URL    = "https://www.cargurus.com/Cars/searchResults.action"

    def search(self, config: dict) -> list:
        listings = []

        # CarGurus uses a single-page JSON API
        params = {
            "zip":             config["zip"],
            "distance":        config.get("radius_mi", 75),
            "startYear":       config.get("year_min", 1990),
            "endYear":         config.get("year_max", 2027),
            "maxPrice":        config.get("price_max", 0),   # 0 = no max
            "maxMileage":      config.get("mileage_max", 0),
            "trim":            "",
            "showNegotiable":  "true",
            "sortDir":         "ASC",
            "sortType":        "PRICE",
            "listingType":     "",
            "zip":             config["zip"],
        }

        # First load the search page to get the entity IDs for make/model
        entity_ids = self._resolve_entity_ids(config)
        if entity_ids:
            params.update(entity_ids)

        resp = self._get(self.SEARCH_URL, params=params,
                         headers={"Accept": "application/json, text/javascript, */*",
                                  "X-Requested-With": "XMLHttpRequest"})
        if not resp:
            return listings

        try:
            # CarGurus returns HTML with embedded JSON
            data = self._extract_json(resp.text)
            if not data:
                return listings

            for item in data.get("listings", []):
                listings.append(self._parse_item(item, config))

        except Exception as e:
            print(f"[cargurus] Parse error: {e}")

        print(f"[cargurus] Found {len(listings)} listings")
        return listings

    def _resolve_entity_ids(self, config: dict) -> dict:
        """CarGurus needs numeric entity IDs for make and model."""
        try:
            make = config["make"].lower()
            model = config["model"].lower()

            # Their suggest endpoint is public
            resp = self._get(
                "https://www.cargurus.com/Cars/fetchCarDealersRecommendations.action",
                params={"q": f"{make} {model}"}
            )
            if resp:
                data = resp.json()
                results = data.get("carEntitySuggestions", [])
                for r in results:
                    if make in r.get("makeName", "").lower() and model in r.get("modelName", "").lower():
                        return {
                            "entitySelectingHelper.selectedEntity1": r.get("entityId"),
                        }
        except Exception:
            pass
        return {}

    def _extract_json(self, html: str) -> dict:
        """Pull the embedded JSON payload out of the HTML response."""
        # CarGurus embeds data in window.CARGURUS_LISTING_DATA = {...}
        match = re.search(r"window\.CARGURUS_LISTING_DATA\s*=\s*(\{.*?\});", html, re.DOTALL)
        if match:
            return json.loads(match.group(1))

        # Alternative: look for __SERVER_DATA__
        match = re.search(r"window\.__SERVER_DATA__\s*=\s*(\{.*?\});", html, re.DOTALL)
        if match:
            return json.loads(match.group(1))

        return {}

    def _parse_item(self, item: dict, config: dict) -> dict:
        listing_info = item.get("listing", item)
        dealer = listing_info.get("seller", {})

        price = listing_info.get("price") or listing_info.get("listingPrice")
        if isinstance(price, str):
            price = int(re.sub(r"[^\d]", "", price)) if price else None

        return self._listing(
            source_id   = str(listing_info.get("id", "")),
            url         = "https://www.cargurus.com/Cars/inventorylisting/viewDetailsFilterViewInventoryListing.action?zip="
                          + config["zip"] + "&showNegotiable=true&sortDir=ASC&distance="
                          + str(config.get("radius_mi", 75)) + "&listingId=" + str(listing_info.get("id", "")),
            vin         = listing_info.get("vin"),
            year        = listing_info.get("year"),
            make        = listing_info.get("makeName") or config.get("make"),
            model       = listing_info.get("modelName") or config.get("model"),
            trim        = listing_info.get("trimName"),
            mileage     = listing_info.get("mileage"),
            price       = price,
            exterior_color = listing_info.get("exteriorColorName"),
            city        = dealer.get("city"),
            state       = dealer.get("stateCode"),
            zip         = dealer.get("zip"),
            distance_mi = listing_info.get("distance"),
            image_url   = (listing_info.get("mainPictureUrl") or
                           listing_info.get("pictureUrl")),
            raw         = listing_info,
        )
