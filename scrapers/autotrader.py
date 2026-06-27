"""
AutoTrader scraper.
Uses AutoTrader's internal search API (same endpoint their website calls).
"""

import re
import json
from bs4 import BeautifulSoup
from .base import BaseScraper



class AutoTraderScraper(BaseScraper):
    name = "autotrader"
    BASE_URL = "https://www.autotrader.com/cars-for-sale/used-cars"

    def search(self, config: dict) -> list:
        listings = []
        page = 0

        while True:
            params = {
                "makeCodeList":   config["make"].upper()[:3],  # e.g. TOY
                "modelCodeList":  config["model"].upper(),
                "startYear":      config.get("year_min", 1990),
                "endYear":        config.get("year_max", 2027),
                "maxPrice":       config.get("price_max", 999999),
                "mileage":        config.get("mileage_max", 999999),
                "zip":            config["zip"],
                "searchRadius":   config.get("radius_mi", 75),
                "firstRecord":    page * 25,
                "numRecords":     25,
                "sortBy":         "derivedpriceASC",
                "listingType":    "USED",
            }
            if config.get("price_min"):
                params["minPrice"] = config["price_min"]

            resp = self._get(self.BASE_URL, params=params)
            if not resp:
                break

            soup = BeautifulSoup(resp.text, "html.parser")

            # AutoTrader embeds listing JSON in a <script> tag
            script = soup.find("script", string=re.compile(r"window.__BONNET_DATA__"))
            if not script:
                # Fall back to parsing the visible cards
                cards = soup.select("[data-cmp='srpListItem']")
                if not cards:
                    break
                for card in cards:
                    listing = self._parse_card(card, config)
                    if listing:
                        listings.append(listing)
            else:
                try:
                    raw = re.search(r"window\.__BONNET_DATA__\s*=\s*(\{.*?\});", script.string, re.DOTALL)
                    data = json.loads(raw.group(1))
                    results = (data.get("initialState", {})
                                   .get("referenceData", {})
                                   .get("listingCollection", {})
                                   .get("listings", []))
                    if not results:
                        break
                    for item in results:
                        listings.append(self._parse_json_item(item))
                except Exception:
                    break

            # Stop if we got fewer than a full page
            if len(soup.select("[data-cmp='srpListItem']")) < 25:
                break
            if len(listings) >= 200:   # safety cap
                break
            page += 1

        print(f"[autotrader] Found {len(listings)} listings")
        return listings

    def _parse_json_item(self, item: dict) -> dict:
        specs = item.get("specifications", {})
        price_info = item.get("pricingDetail", {})
        owner = item.get("owner", {})
        loc = owner.get("location", {})

        return self._listing(
            source_id   = str(item.get("id", "")),
            url         = "https://www.autotrader.com/cars-for-sale/vehicledetails.xhtml?listingId=" + str(item.get("id", "")),
            vin         = item.get("vin"),
            year        = specs.get("year"),
            make        = specs.get("make"),
            model       = specs.get("model"),
            trim        = specs.get("trim"),
            mileage     = specs.get("mileage"),
            price       = price_info.get("salePrice") or price_info.get("listPrice"),
            msrp        = price_info.get("msrp"),
            exterior_color = specs.get("exteriorColor"),
            city        = loc.get("city"),
            state       = loc.get("state"),
            zip         = loc.get("zip"),
            distance_mi = item.get("distanceToOwner"),
            image_url   = (item.get("images", {}).get("sources") or [{}])[0].get("src"),
            image_urls  = [(s.get("src") or "") for s in item.get("images", {}).get("sources", [])],
            raw         = item,
        )

    def _parse_card(self, card, config: dict) -> dict:
        """Fallback HTML parser for when JSON embed isn't found."""
        try:
            title = card.select_one("[data-cmp='vehicleInfo'] h2")
            price_el = card.select_one("[data-cmp='vehiclePricing'] .first-price")
            link_el = card.select_one("a[href*='listingId']")
            img_el = card.select_one("img")

            title_text = title.get_text(strip=True) if title else ""
            year_match = re.match(r"(\d{4})", title_text)
            year = int(year_match.group(1)) if year_match else None

            price_text = price_el.get_text(strip=True) if price_el else ""
            price = int(re.sub(r"[^\d]", "", price_text)) if price_text else None

            href = link_el["href"] if link_el else ""
            url = ("https://www.autotrader.com" + href) if href.startswith("/") else href

            return self._listing(
                url   = url,
                year  = year,
                make  = config.get("make"),
                model = config.get("model"),
                price = price,
                image_url = img_el["src"] if img_el else None,
                raw   = {"html_title": title_text},
            )
        except Exception:
            return None
