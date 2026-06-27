"""
Dealer scraper using cardealerdb.com to find dealerships,
then scraping ALL their inventory into the database.

Two public methods:
  crawl(zip_code, radius_mi)  — find dealers + store all inventory (no filtering)
  search(config)              — legacy shim, calls crawl()

cardealerdb.com URL structure:
  City list:   https://cardealerdb.com/in/{STATE}/{city-slug}
  Dealer page: https://cardealerdb.com/go/{STATE}/{city-slug}/{dealer-slug}/{id}
"""

import re
import json
import time
from urllib.parse import urljoin, urlparse
from bs4 import BeautifulSoup
from geopy.geocoders import Nominatim
from geopy.distance import geodesic
from .base import BaseScraper

CARDEALERDB = "https://cardealerdb.com"


def _money_to_int(text):
    """'$32,800' / '39,803 miles' → 32800 / 39803. None if no digits."""
    if text is None:
        return None
    if isinstance(text, (int, float)):
        return int(text)
    digits = re.sub(r"[^\d]", "", str(text))
    return int(digits) if digits else None


def _to_int(v):
    try:
        return int(v) if v is not None else None
    except (ValueError, TypeError):
        return None


def _bounded_int(v, lo, hi):
    """int(v) if it lands in [lo, hi], else None. Guards against junk numbers."""
    n = _to_int(v)
    return n if n is not None and lo <= n <= hi else None


_VIN_RE = re.compile(r"^[A-HJ-NPR-Z0-9]{17}$")


def _looks_like_vin(s):
    return bool(s) and bool(_VIN_RE.match(str(s).strip().upper()))


def _slugify(s):
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")


class DealerScraper(BaseScraper):
    name = "dealer"

    def __init__(self):
        super().__init__()
        self._geo = Nominatim(user_agent="car-search-app-v2")
        self._coord_cache = {}

    # ── Public entry points ───────────────────────────────────

    def crawl(self, zip_code: str, radius_mi: int = 50) -> list:
        """Find all dealerships within radius and store their full inventory.
        No make/model filtering — stores every car found.
        Returns list of all raw listing dicts saved."""
        city, state, origin = self._zip_to_city_state(zip_code)
        if not city or not state:
            print(f"[dealer] Could not geocode ZIP {zip_code}")
            return []

        print(f"[dealer] Crawling dealers near {city}, {state} (radius {radius_mi}mi)")

        dealers = self._find_dealers_cardealerdb(city, state, origin, radius_mi)
        print(f"[dealer] Found {len(dealers)} dealerships")

        if not dealers:
            return []

        from database import upsert_dealership, upsert_listing, update_dealer_scrape_status
        all_listings = []

        for dealer in dealers:
            dealer_id = upsert_dealership(dealer)
            dealer["id"] = dealer_id

            if not dealer.get("website"):
                update_dealer_scrape_status(dealer_id, "none", "unsupported", 0,
                                            "no website on file")
                continue

            listings, platform, status, note = self._scrape_inventory(dealer)
            for l in listings:
                l["dealership_id"] = dealer_id
                upsert_listing(l)          # persist as we go so progress survives errors
            all_listings.extend(listings)
            update_dealer_scrape_status(dealer_id, platform, status, len(listings), note)
            print(f"  [{status:11}] {dealer['name'][:38]:38} {len(listings):4} ({platform})")

        print(f"[dealer] Total inventory stored: {len(all_listings)}")
        return all_listings

    def search(self, config: dict) -> list:
        """Called by the legacy search pipeline. Triggers a crawl."""
        return self.crawl(
            zip_code=config.get("zip", ""),
            radius_mi=config.get("radius_mi", 50),
        )

    # ── cardealerdb.com ───────────────────────────────────────

    def _zip_to_city_state(self, zip_code):
        _ZIP_FALLBACK = {
            "78664": ("Round Rock", "TX", (30.5083, -97.6789)),
            "78681": ("Round Rock", "TX", (30.5083, -97.6789)),
            "78701": ("Austin",     "TX", (30.2672, -97.7431)),
            "78750": ("Austin",     "TX", (30.4200, -97.7900)),
        }

        queries = [
            f"{zip_code}, USA",
            {"postalcode": zip_code, "countrycodes": "us"},
        ]
        for q in queries:
            try:
                time.sleep(0.3)
                loc = self._geo.geocode(q, exactly_one=True, addressdetails=True)
                if loc:
                    addr  = loc.raw.get("address", {})
                    city  = (addr.get("city") or addr.get("town") or
                             addr.get("village") or addr.get("county") or
                             loc.address.split(",")[0]).strip()
                    state = (addr.get("state_code") or
                             self._state_name_to_abbr(addr.get("state", "")))
                    if city and state:
                        print(f"[dealer] ZIP {zip_code} → {city}, {state}")
                        return city, state, (loc.latitude, loc.longitude)
            except Exception as e:
                print(f"[dealer] Geocode attempt failed: {e}")
                continue

        if zip_code in _ZIP_FALLBACK:
            city, state, coords = _ZIP_FALLBACK[zip_code]
            print(f"[dealer] ZIP {zip_code} → {city}, {state} (fallback)")
            return city, state, coords

        print(f"[dealer] Could not geocode ZIP {zip_code}")
        return None, None, (None, None)

    def _city_to_slug(self, city):
        return re.sub(r"[^a-z0-9]+", "-", city.lower()).strip("-")

    _STATE_CITIES = {
        "TX": ["Austin","Round Rock","Georgetown","Cedar Park","Pflugerville","Kyle","Buda",
               "San Marcos","Leander","Hutto","Taylor","San Antonio","New Braunfels","Seguin",
               "Lockhart","Bastrop","Elgin","Manor","Lakeway","Bee Cave","Waco","Temple","Killeen"],
        "CA": ["Los Angeles","San Diego","San Jose","San Francisco","Fresno","Sacramento",
               "Long Beach","Oakland","Bakersfield","Anaheim","Riverside","Stockton"],
        "FL": ["Jacksonville","Miami","Tampa","Orlando","St. Petersburg","Hialeah","Tallahassee",
               "Fort Lauderdale","Cape Coral","Pembroke Pines","Hollywood","Gainesville"],
        "NY": ["New York","Buffalo","Rochester","Yonkers","Syracuse","Albany","New Rochelle",
               "Mount Vernon","Schenectady","Utica","White Plains","Troy"],
    }

    def _find_dealers_cardealerdb(self, city, state, origin, radius_mi):
        visited  = set()
        dealers  = []

        # Seed: start city + known cities in state filtered by radius
        candidate_cities = [(city, state)]
        for known_city in self._STATE_CITIES.get(state, []):
            if known_city.lower() != city.lower():
                candidate_cities.append((known_city, state))

        queue = []
        for c, s in candidate_cities:
            if c.lower() == city.lower():
                queue.append((c, s))
                continue
            if origin[0]:
                coords = self._geocode_city(c, s)
                if coords and geodesic(origin, coords).miles <= radius_mi:
                    queue.append((c, s))
            else:
                queue.append((c, s))

        print(f"[dealer] Searching {len(queue)} cities near {city}, {state}")

        for c_name, c_state in queue:
            slug = self._city_to_slug(c_name)
            key  = f"{c_state}/{slug}"
            if key in visited:
                continue
            visited.add(key)

            url  = f"{CARDEALERDB}/in/{c_state}/{slug}"
            resp = self._get(url)
            if not resp or resp.status_code != 200:
                continue

            soup = BeautifulSoup(resp.text, "html.parser")

            # Dealer links are relative ("go/TX/{city}/{slug}/{id}") on the live
            # site, so match the path segment rather than requiring "/go/".
            seen_hrefs = set()
            for link in soup.select("a[href]"):
                m = re.search(r"(?:^|/)(go/[^\s\"']+)", link.get("href", ""))
                if not m:
                    continue
                href = f"{CARDEALERDB}/{m.group(1)}"
                if href in seen_hrefs:
                    continue
                seen_hrefs.add(href)
                detail = self._fetch_dealer_detail(href, c_name, c_state)
                if detail:
                    dealers.append(detail)

            if len(dealers) >= 100:
                break

        return dealers

    def _fetch_dealer_detail(self, url, city, state):
        resp = self._get(url)
        if not resp or resp.status_code != 200:
            return None

        soup = BeautifulSoup(resp.text, "html.parser")

        h1   = soup.select_one("h1")
        name = h1.get_text(strip=True) if h1 else ""
        if not name:
            return None

        body = soup.get_text(" ", strip=True)

        phone_m = re.search(r"Tel:\s*([\d\s\-\(\)\.]+)", body)
        phone   = phone_m.group(1).strip() if phone_m else ""

        website = ""
        for a in soup.select("a[href^='http']"):
            href = a.get("href", "")
            if "cardealerdb.com" not in href and "google" not in href:
                website = href
                break

        addr_m   = re.search(
            re.escape(name) + r"\s+([\w\s\.]+?)\s+" + re.escape(city) + r"\s+(\d{5})",
            body, re.I
        )
        address  = addr_m.group(1).strip() if addr_m else ""
        zip_code = addr_m.group(2) if addr_m else ""

        return {
            "name":        name,
            "address":     address,
            "city":        city,
            "state":       state,
            "zip":         zip_code,
            "phone":       phone,
            "website":     website,
            "source_url":  url,
            "distance_mi": None,
        }

    # ── Inventory scraping (no filtering — store everything) ──

    INVENTORY_PATHS = [
        "/inventory", "/used-inventory", "/pre-owned", "/used-cars",
        "/vehicles", "/used-vehicles", "/search", "/used",
        "/new-inventory", "/new-vehicles", "/new-cars",
    ]

    # Dealer.com (DDC) platform — used by most franchise dealers.
    # Inventory pages embed full vehicle JSON in DDC.WS.state['ws-inv-data'].
    DDC_PATHS = ["/used-inventory/index.htm", "/new-inventory/index.htm"]
    MAX_PAGES_PER_DEALER = 30

    # The BeautifulSoup/soupsieve HTML-card fallback parser can trigger a FATAL,
    # uncatchable CPython 3.14 crash (soupsieve css_match "Executing a cache") that
    # aborts an entire multi-dealer scan mid-run. It currently yields 0 kept
    # listings (every active row comes from a structured extractor), so it is
    # disabled by default. Flip to True only on a Python/soupsieve build where the
    # crash is fixed.
    ENABLE_HTML_FALLBACK = False

    # Pages we try to load to detect a platform. Order matters: the DDC
    # index.htm form is listed first so Dealer.com sites are caught cheaply.
    # Bare "/inventory" (no trailing slash) is included for Next.js/SPA sites
    # (e.g. Alpha One Motors) whose inventory lives there and 404s on the
    # trailing-slash form.
    LANDING_PATHS = [
        "/used-inventory/index.htm", "/used-vehicles/", "/used-inventory/",
        "/inventory/", "/inventory", "/used-cars/", "/used-cars",
        "/cars-for-sale", "/vehicles/",
    ]

    def _scrape_inventory(self, dealer):
        """Scrape all inventory from a dealer's website. No make/model filtering.

        Returns (listings, platform, status, note) so the caller can record
        per-dealer diagnostics:
          platform — 'dealer.com' | 'dealer_inspire' | 'generic' | 'unknown'
          status   — 'ok' | 'empty' | 'unreachable' | 'unsupported'
        """
        base = dealer.get("website", "").rstrip("/")
        if not base:
            return [], "none", "unsupported", "no website on file"

        reached      = False
        detected     = None        # platform recognised even if 0 vehicles parsed
        seen_status  = []          # HTTP codes seen on landing-page attempts
        conn_fails   = 0           # consecutive connection-level errors

        for path in self.LANDING_PATHS:
            resp = self._get_raw(base + path)

            if resp is None:
                conn_fails += 1
                if conn_fails >= 2:    # dead domain — stop hammering timeouts
                    break
                continue
            conn_fails = 0

            code = resp.status_code
            # A WAF block on one path means every path will block — short-circuit.
            if code in (403, 406, 429):
                return [], "unknown", "blocked", \
                    f"HTTP {code} — bot/WAF block (Imperva/Cloudflare); scrapable on a clean IP"
            if code != 200:
                seen_status.append(code)
                continue
            if len(resp.text) < 2000:
                continue
            reached = True
            html = resp.text

            # 1) Dealer.com (DDC) — embedded vehicle JSON
            if "ws-inv-data" in html or "DDC.WS.state" in html:
                detected = "dealer.com"
                listings, complete, total = self._scrape_ddc_inventory(base, dealer)
                if listings:
                    note = f"{len(listings)} vehicles via DDC embed"
                    if not complete:
                        note += f" (PARTIAL — got {len(listings)} of ~{total}; some pages failed)"
                    return listings, "dealer.com", "ok", note

            # 2) Dealer Inspire — Cars Commerce search API
            if "SEARCH_SERVICE" in html and "carscommerce" in html.lower():
                detected = "dealer_inspire"
                listings = self._scrape_dealerinspire(html, dealer)
                if listings:
                    return listings, "dealer_inspire", "ok", f"{len(listings)} vehicles via Cars Commerce API"
                return [], "dealer_inspire", "empty", "Dealer Inspire detected but API returned 0 vehicles"

            # 3) Generic. Many generic platforms paginate and default to ~20 per
            #    page via a ?limit param — grab one big page so we don't miss
            #    inventory (DealerFire etc.). Sites cap the limit to their own
            #    inventory, so a high number returns everything they have.
            gen_html, gen_url = html, base + path
            sep  = "&" if "?" in path else "?"
            bigger = self._get_raw(base + path + f"{sep}limit=1000")
            if bigger and bigger.status_code == 200 and len(bigger.text) > len(gen_html):
                gen_html, gen_url = bigger.text, base + path + f"{sep}limit=1000"

            # Run the structured (regex-based) extractors and keep the richest
            # result by total data captured.
            best, best_how, best_score = None, None, -1
            for label, fn in [
                ("next-data",       lambda: self._extract_nextdata_inventory(gen_html, gen_url, dealer)),
                ("inline-JSON",     lambda: self._extract_inline_vehicle_json(gen_html, gen_url, dealer)),
                ("vehicleDetails",  lambda: self._extract_vehicledetails_inventory(gen_html, gen_url, dealer)),
                ("JSON-LD",         lambda: self._extract_jsonld_inventory(gen_html, gen_url, dealer)),
                ("embedded",        lambda: self._extract_json_inventory(gen_html, dealer)),
            ]:
                got = fn() or []
                if got:
                    score = self._completeness_score(got)
                    if score > best_score:
                        best, best_how, best_score = got, label, score

            # HTML card parser is the LAST resort: it parses the whole page with
            # BeautifulSoup/soupsieve, which is slow and — on Python 3.14 — can
            # fatally crash on multi-MB pages. Only use it when structured
            # extraction came up short, and never on very large pages.
            if (self.ENABLE_HTML_FALLBACK and (not best or len(best) < 3)
                    and len(gen_html) < 800_000):
                got = self._parse_html_inventory(gen_html, gen_url, dealer)
                if got:
                    score = self._completeness_score(got)
                    if score > best_score:
                        best, best_how, best_score = got, "HTML", score

            if best:
                # Backfill mileage/vin/price the winning extractor missed but the
                # HTML cards still carry (e.g. carsforsale.com JSON-LD has no mileage).
                self._enrich_listings(best, gen_html, gen_url)
                return best, "generic", "ok", f"{len(best)} vehicles via generic parser ({best_how})"

        # ── Fallback: the SRP loaded but inventory is JS-rendered with no
        #    embedded data. Many such sites still expose per-vehicle pages via a
        #    sitemap, and those VDPs usually carry JSON-LD / Next.js data. Only
        #    attempt this when the domain actually responded (reached), so dead
        #    domains aren't slowed down chasing sitemaps that won't exist.
        if reached:
            sm = self._scrape_via_sitemap(base, dealer)
            if sm:
                return sm, "sitemap", "ok", f"{len(sm)} vehicles via sitemap + VDP parse"

        # ── No inventory parsed — classify the outcome for the report ──
        if detected == "dealer.com":
            return [], "dealer.com", "empty", "DDC site detected but no used inventory parsed"
        if reached:
            return [], "unknown", "unsupported", \
                "inventory page loaded but vehicles are JS-rendered / platform unrecognized"
        if seen_status and all(s == 404 for s in seen_status):
            return [], "unknown", "unsupported", "inventory pages 404 — non-standard URL scheme"
        if seen_status:
            return [], "unknown", "blocked", f"HTTP {seen_status[0]} on inventory pages"
        return [], "unknown", "unreachable", "connection failed — dead domain / DNS / timeout"

    # ── Dealer Inspire (Cars Commerce search API) ─────────────

    def _scrape_dealerinspire(self, srp_html, dealer) -> list:
        """Dealer Inspire sites load inventory from Cars Commerce's search API.
        The per-dealer apiUrl/ccid/apiKey are embedded in window.SEARCH_SERVICE."""
        cfg = self._extract_search_service(srp_html)
        if not cfg:
            return []

        api = cfg["apiUrl"].rstrip("/") + f"/api/v1/listings/{cfg['ccid']}/search"
        headers = {"Content-Type": "application/json", "Accept": "application/json",
                   "x-api-key": cfg["apiKey"]}

        results, page, total = [], 1, None
        while page <= self.MAX_PAGES_PER_DEALER:
            resp = self._post_retry(api, json={"page": page}, headers=headers)
            if not resp:
                break
            try:
                data = resp.json().get("data", {})
            except Exception:
                break
            vehicles = data.get("listings") or []
            if total is None:
                total = data.get("total_vehicle_count")
            if not vehicles:
                break
            for v in vehicles:
                results.append(self._di_vehicle_to_listing(v, dealer))
            if total is not None and len(results) >= total:
                break
            page += 1
        return results

    def _extract_search_service(self, html):
        m = re.search(r"var\s+SEARCH_SERVICE\s*=\s*(\{.*?\})\s*;", html, re.DOTALL)
        if not m:
            return None
        try:
            cfg = json.loads(m.group(1))
        except Exception:
            return None
        if cfg.get("apiUrl") and cfg.get("ccid") and cfg.get("apiKey"):
            return cfg
        return None

    def _di_vehicle_to_listing(self, v, dealer):
        pricing = v.get("pricing") or {}
        price   = (pricing.get("price") or pricing.get("internet_price")
                   or pricing.get("our_price"))
        styles  = v.get("styles") or {}
        mech    = v.get("mechanical") or {}
        media   = v.get("media") or {}
        imgs    = media.get("images") or []
        return self._listing(
            source_id      = str(v.get("vin") or v.get("stock") or ""),
            url            = v.get("vdp_url") or dealer.get("website"),
            vin            = v.get("vin"),
            year           = _to_int(v.get("year")),
            make           = v.get("make"),
            model          = v.get("model"),
            trim           = v.get("trim"),
            mileage        = _to_int(v.get("mileage")),
            price          = _to_int(price) if price else None,
            exterior_color = styles.get("exterior_color"),
            transmission   = mech.get("transmission"),
            city           = dealer.get("city"),
            state          = dealer.get("state"),
            zip            = dealer.get("zip"),
            image_url      = imgs[0] if imgs else None,
            raw            = {"type": v.get("type"), "stock": v.get("stock"),
                              "is_certified": v.get("is_certified"),
                              "dealer": dealer.get("name")},
        )

    def _scrape_ddc_inventory(self, base, dealer):
        """Paginate a Dealer.com site's inventory pages (?start=N).

        Returns (listings, complete, expected_total). `complete=False` means a
        page fetch failed mid-pagination (after retries) or the per-dealer page
        cap was hit before the known total — i.e. the dealer is only partially
        scraped. Previously a single transient failure silently truncated the
        dealer with no signal; the caller now records it in the scrape note."""
        all_results   = []
        pages_fetched = 0
        complete      = True
        expected      = 0

        for path in self.DDC_PATHS:
            start = 0
            total = None
            while pages_fetched < self.MAX_PAGES_PER_DEALER:
                params = {"start": start} if start else None
                resp   = self._get_retry(base + path, params=params)
                if not resp or resp.status_code != 200:
                    # survived retries and still failed — flag partial only if we
                    # know there were more vehicles left to fetch on this path
                    if total is not None and start < total:
                        complete = False
                    break
                pages_fetched += 1

                batch, total, page_size = self._extract_ddc_page(resp.text, base, dealer)
                if batch is None:          # not a Dealer.com page — stop trying
                    return all_results, complete, expected
                if total is not None and start == 0:
                    expected += total
                all_results.extend(batch)

                start += page_size or len(batch) or 24
                if not batch or (total is not None and start >= total):
                    break
            else:
                # loop ended because the page cap was hit, not a natural finish
                if total is not None and start < total:
                    complete = False

        return all_results, complete, expected

    def _extract_ddc_page(self, html, base, dealer):
        """Parse one DDC inventory page.
        Returns (listings, total_count, page_size), or (None, None, None)
        if the page is not a Dealer.com inventory page."""
        m = re.search(
            r"DDC\.WS\.state\['ws-inv-data'\]\['[^']+'\]\s*=\s*", html
        )
        if not m:
            return None, None, None
        try:
            data, _ = json.JSONDecoder().raw_decode(html[m.end():])
            wis     = data["WIS"]
            page    = wis.get("pageInfo", {})
            vehicles = wis.get("inventory", [])
        except Exception:
            return None, None, None

        results = []
        for v in vehicles:
            if v.get("isPlaceholder"):
                continue
            attrs = {a.get("name"): a.get("value")
                     for a in v.get("attributes", []) if a.get("name")}

            link = v.get("link", "")
            url  = urljoin(base + "/", link) if link else dealer.get("website")

            imgs    = v.get("images") or []
            img_url = imgs[0].get("uri") if imgs and isinstance(imgs[0], dict) else None

            results.append(self._listing(
                source_id      = str(v.get("vin") or v.get("stockNumber") or v.get("uuid", "")),
                url            = url,
                vin            = v.get("vin"),
                year           = _to_int(v.get("year")),
                make           = v.get("make"),
                model          = v.get("model"),
                trim           = v.get("trim"),
                mileage        = _money_to_int(attrs.get("odometer")),
                price          = self._ddc_price(v.get("pricing", {})),
                exterior_color = attrs.get("exteriorColor"),
                transmission   = attrs.get("transmission"),
                city           = dealer.get("city"),
                state          = dealer.get("state"),
                zip            = dealer.get("zip"),
                image_url      = img_url,
                raw            = {"condition": v.get("condition"),
                                  "stockNumber": v.get("stockNumber"),
                                  "bodyStyle": v.get("bodyStyle"),
                                  "dealer": dealer.get("name")},
            ))
        return results, page.get("totalCount"), page.get("pageSize")

    @staticmethod
    def _ddc_price(pricing):
        """Pick the sale price out of a DDC pricing block."""
        for p in pricing.get("dprice") or []:
            if p.get("typeClass") == "salePrice":
                val = _money_to_int(p.get("value"))
                if val:
                    return val
        return _money_to_int(pricing.get("retailPrice"))

    def _extract_json_inventory(self, html, dealer) -> list:
        patterns = [
            r"window\.__INITIAL_STATE__\s*=\s*(\{.*?\});",
            r"window\.DDC\.dataLayer\s*=\s*(\{.*?\});",
            r"var\s+inventoryData\s*=\s*(\[.*?\]);",
            r'"vehicles"\s*:\s*(\[.*?\])',
            r'"inventory"\s*:\s*(\[.*?\])',
        ]
        for pattern in patterns:
            m = re.search(pattern, html, re.DOTALL)
            if not m:
                continue
            try:
                data = json.loads(m.group(1))
                vehicles = (
                    data.get("inventory") or data.get("vehicles") or
                    data.get("items") or (data if isinstance(data, list) else [])
                )
                if not vehicles:
                    continue
                results = []
                for v in vehicles:
                    imgs    = v.get("images") or []
                    img_url = imgs[0].get("url") if imgs and isinstance(imgs[0], dict) else v.get("imageUrl", "")
                    results.append(self._listing(
                        source_id      = str(v.get("vin") or v.get("stockNumber") or v.get("id", "")),
                        url            = v.get("url") or v.get("detailUrl") or dealer.get("website"),
                        vin            = v.get("vin"),
                        year           = v.get("year"),
                        make           = v.get("make"),
                        model          = v.get("model"),
                        trim           = v.get("trim"),
                        mileage        = v.get("mileage") or v.get("miles"),
                        price          = v.get("price") or v.get("internetPrice") or v.get("sellingPrice"),
                        exterior_color = v.get("exteriorColor") or v.get("color"),
                        city           = dealer.get("city"),
                        state          = dealer.get("state"),
                        zip            = dealer.get("zip"),
                        image_url      = img_url,
                        raw            = v,
                    ))
                return results
            except Exception:
                continue
        return []

    # ── Next.js embedded inventory (__NEXT_DATA__) ────────────────
    # Modern React/Next.js dealer sites (e.g. Alpha One Motors) render the SRP
    # client-side but ship the full inventory as server data inside
    #   <script id="__NEXT_DATA__" type="application/json">{...}</script>
    # under props.pageProps. No browser needed — the data is right there.

    _NEXT_PRICE_KEYS   = ["listing_price", "sellingPrice", "internetPrice",
                          "our_price", "sale_price", "price", "askingPrice"]
    _NEXT_MILEAGE_KEYS = ["mileage", "miles", "odometer"]
    _NEXT_COLOR_KEYS   = ["exterior_color", "exteriorColor", "ext_color", "color"]
    _NEXT_STOCK_KEYS   = ["stockno", "stock", "stockNumber", "stock_number"]
    _NEXT_URL_KEYS     = ["details_url", "vdp_url", "url", "link"]

    def _extract_nextdata_inventory(self, html, page_url, dealer) -> list:
        m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.DOTALL)
        if not m:
            return []
        try:
            data = json.loads(m.group(1))
        except Exception:
            return []

        vehicles = self._collect_nextdata_vehicles(data)
        if not vehicles:
            return []

        results, seen = [], set()
        for v in vehicles:
            make  = v.get("make")
            model = v.get("model")
            raw_vin = str(v.get("vin") or v.get("VIN") or "").strip().upper()
            if not (make or raw_vin):
                continue
            if self._is_sold(v):          # drop sold / sale-pending units
                continue

            stock = next((v.get(k) for k in self._NEXT_STOCK_KEYS if v.get(k)), None)
            key   = raw_vin or stock or v.get("details_url")
            if not key or key in seen:
                continue
            seen.add(key)

            price = next((_bounded_int(v.get(k), 100, 10_000_000)
                          for k in self._NEXT_PRICE_KEYS
                          if _bounded_int(v.get(k), 100, 10_000_000)), None)
            mileage = next((_bounded_int(v.get(k), 0, 2_000_000)
                            for k in self._NEXT_MILEAGE_KEYS
                            if _bounded_int(v.get(k), 0, 2_000_000)), None)
            color = next((v.get(k) for k in self._NEXT_COLOR_KEYS if v.get(k)), None)
            url   = next((v.get(k) for k in self._NEXT_URL_KEYS if v.get(k)), None)
            url   = urljoin(page_url, url) if url else \
                    f"{dealer.get('website','').rstrip('/')}/inventory#{raw_vin or stock}"

            results.append(self._listing(
                source_id      = raw_vin or str(stock or ""),
                url            = url,
                vin            = raw_vin if _looks_like_vin(raw_vin) else None,
                year           = _bounded_int(v.get("year"), 1900, 2100),
                make           = make,
                model          = model,
                trim           = v.get("trim"),
                mileage        = mileage,
                price          = price,
                exterior_color = color,
                transmission   = v.get("transmission") or v.get("trans"),
                city           = dealer.get("city"),
                state          = dealer.get("state"),
                zip            = dealer.get("zip"),
                image_url      = self._first_nextdata_image(v),
                raw            = {"stock": stock, "via": "next-data",
                                  "raw_vin": raw_vin, "dealer": dealer.get("name")},
            ))
        return results

    def _collect_nextdata_vehicles(self, data):
        """Find the largest list of vehicle-like dicts anywhere in a parsed
        __NEXT_DATA__ tree. A dict is 'vehicle-like' if it has a vin or a
        year+make+model (or make+model+price) combo."""
        best = []

        def is_vehicle(d):
            if not isinstance(d, dict):
                return False
            keys = {k.lower() for k in d.keys()}
            return ("vin" in keys or {"year", "make", "model"} <= keys
                    or {"make", "model", "price"} <= keys)

        def walk(o, depth=0):
            nonlocal best
            if depth > 12:
                return
            if isinstance(o, list):
                vs = [x for x in o if is_vehicle(x)]
                if len(vs) > len(best):
                    best = vs
                for x in o:
                    walk(x, depth + 1)
            elif isinstance(o, dict):
                for val in o.values():
                    walk(val, depth + 1)

        walk(data)
        return best

    @staticmethod
    def _first_nextdata_image(v):
        for k in ("vdp_hero_image", "image_url", "image", "thumbnail"):
            val = v.get(k)
            if isinstance(val, str) and val.startswith("http"):
                return val
        iu = v.get("image_urls") or v.get("images")
        if isinstance(iu, list) and iu:
            first = iu[0]
            if isinstance(first, dict):
                return first.get("url") or first.get("uri") or first.get("src")
            if isinstance(first, str) and first.startswith("http"):
                return first
        if isinstance(iu, str):
            mm = re.search(r'https?://[^"\s,]+', iu)
            if mm:
                return mm.group(0)
        return None

    @staticmethod
    def _completeness_score(listings):
        """Score an extractor's output to pick the best for a generic site.
        Uses the ABSOLUTE total of valuable fields captured across all listings,
        so finding many vehicles with good-enough data beats finding one rich
        vehicle. (All extractors already require make-or-vin, so counts are real.)"""
        if not listings:
            return 0
        keys = ("vin", "price", "mileage", "trim", "year", "image_url", "make", "model")
        return sum(1 for l in listings for k in keys if l.get(k) not in (None, "", 0))

    # ── Sold / availability detection ─────────────────────────────
    # Independents (e.g. DealerCarSearch sites like m1atx.com) leave SOLD units in
    # their inventory feed, flagged only by a status field. Without filtering them
    # they'd show as for-sale forever: the VIN never disappears from a scrape, so
    # scan_inventory's sold-diff never fires. Dropping them at parse time means the
    # next scan also deactivates any already-stored copy (its VIN is now absent).
    _SOLD_WORDS = ("sold", "sale pending", "salepending", "pending", "soldout",
                   "out of stock", "outofstock", "discontinued")

    @classmethod
    def _is_sold(cls, obj):
        """True if a structured vehicle object is marked sold / sale-pending."""
        status = str(obj.get("status") or obj.get("vehicleStatus")
                     or obj.get("availability") or "").strip().lower()
        if any(w in status for w in cls._SOLD_WORDS):
            return True
        for k in ("isSold", "sold"):
            v = obj.get(k)
            if v is True or str(v).strip().lower() in ("true", "1", "yes"):
                return True
        return False

    # ── Inline vehicle JSON (vin-keyed objects pushed into JS) ────
    # Many platforms (e.g. DealerFire's `VehicleObject_<id>` dataLayer pushes)
    # embed a full vehicle object inline. Richest generic source — has price.

    # Price fields in rough priority order (first non-zero wins).
    _PRICE_KEYS = ["internetPrice", "sellingPrice", "finalPrice", "salePrice",
                   "ourPrice", "our_price", "askingPrice", "price",
                   "originalPrice", "listPrice"]
    _MILEAGE_KEYS = ["mileage", "miles", "odometer"]
    _COLOR_KEYS   = ["exteriorColor", "exterior_color", "ext_color", "color"]
    _STOCK_KEYS   = ["stockNumber", "stock", "stockNo", "stock_number"]
    _IMAGE_KEYS   = ["image", "imageUrl", "image_url", "photo", "thumbnail"]
    _TRANS_KEYS   = ["transmissionDescription", "transmission", "transmission_type",
                     "transmissionType", "trans"]
    # A vehicle object's own detail-page (VDP) URL, in priority order. Preferred
    # over href-matching / synthetic anchors — these are the real links the
    # dealer's own embedded data carries (e.g. austineautos.com ships `vdp`).
    _VDP_FIELD_KEYS = ("vdp", "vdp_url", "vdpUrl", "vdpURL", "seoUrl", "seo_url",
                       "details_url", "detailsUrl", "detail_url", "detailUrl",
                       "vehicleUrl", "vehicle_url", "vehicle_detail_url",
                       "url", "link")   # bare url/link last — least specific

    def _extract_inline_vehicle_json(self, html, page_url, dealer) -> list:
        objs = self._find_vin_json_objects(html)
        if not objs:
            return []

        vdp_links = re.findall(
            r'href="([^"]*(?:/vehicle-details/|/vehicle/|/used[-/]|/new[-/]|/inventory/)[^"#?]*)"',
            html, re.I)
        vdp_links = list(dict.fromkeys(vdp_links))
        vin_imgs  = self._vin_image_map(html)
        # JSON-LD reliably carries the image (keyed by the same VIN via mpn),
        # so use it to fill images the inline objects omit.
        for node in self._collect_jsonld_vehicles(html):
            f = self._jsonld_to_fields(node)
            if f.get("vin") and f.get("image"):
                vin_imgs.setdefault(f["vin"], f["image"])

        results, seen = [], set()
        for o in objs:
            vin = str(o.get("vin", "")).strip().upper()
            if not _looks_like_vin(vin) or vin in seen:
                continue

            make  = o.get("make")
            model = o.get("model")
            if not (make or model):
                continue
            if self._is_sold(o):          # drop sold / sale-pending units
                continue
            seen.add(vin)

            price = next((_bounded_int(o.get(k), 100, 10_000_000)
                          for k in self._PRICE_KEYS
                          if _bounded_int(o.get(k), 100, 10_000_000)), None)
            mileage = next((_bounded_int(o.get(k), 0, 2_000_000)
                            for k in self._MILEAGE_KEYS
                            if _bounded_int(o.get(k), 0, 2_000_000)), None)
            color = next((o.get(k) for k in self._COLOR_KEYS if o.get(k)), None)
            stock = next((o.get(k) for k in self._STOCK_KEYS if o.get(k)), None)
            trans = next((o.get(k) for k in self._TRANS_KEYS if o.get(k)), None)
            image = next((o.get(k) for k in self._IMAGE_KEYS if o.get(k)), None)
            if isinstance(image, list):
                image = image[0] if image else None
            if not image:
                image = vin_imgs.get(vin)   # match a page image URL by VIN

            name = " ".join(str(x) for x in
                            [o.get("year"), make, model, o.get("trim")] if x)
            # 1) Prefer the vehicle object's OWN detail-page URL when present — the
            #    most reliable real VDP link (many platforms ship it inline).
            own = next((o.get(k) for k in self._VDP_FIELD_KEYS
                        if isinstance(o.get(k), str) and o.get(k).strip()), None)
            if own:
                url = urljoin(page_url, own.strip())
            else:
                # 2) Otherwise match a real VDP href on the page; this returns a
                #    synthetic /inventory#vin anchor if none is found.
                url = self._match_vdp_url({"vin": vin, "name": name, "stock": stock},
                                          vdp_links, page_url, dealer)
                # 3) On a JS/SPA inventory page with no real hrefs, build the VDP
                #    from the listing id for DealerCarSearch-style sites (accountId
                #    + listingId), which serve VDPs at /{listingId}/{year-make-model}
                #    — a bare /{listingId} also resolves, so the slug is cosmetic.
                listing_id = next((o.get(k) for k in ("listingId", "listing_id", "id")
                                   if o.get(k)), None)
                if "#" in url and listing_id and o.get("accountId"):
                    vslug = re.sub(r"-+", "-", "-".join(
                        str(p).strip() for p in (o.get("year"), make, model) if p
                    ).replace(" ", "-")).strip("-")
                    vbase = dealer.get("website", page_url).rstrip("/")
                    url = f"{vbase}/{listing_id}/{vslug}" if vslug else f"{vbase}/{listing_id}"

            results.append(self._listing(
                source_id      = vin,
                url            = url,
                vin            = vin,
                year           = _bounded_int(o.get("year"), 1900, 2100),
                make           = make,
                model          = model,
                trim           = o.get("trim"),
                mileage        = mileage,
                price          = price,
                exterior_color = color,
                transmission   = trans,
                city           = dealer.get("city"),
                state          = dealer.get("state"),
                zip            = dealer.get("zip"),
                image_url      = image,
                raw            = {"stock": stock, "via": "inline-json",
                                  "isNew": o.get("isNew"), "dealer": dealer.get("name")},
            ))
        return results

    def _vin_image_map(self, html):
        """Map VIN → image URL by scanning image URLs that embed a 17-char VIN
        in their path (common on cdn-ds.com and similar dealer CDNs)."""
        m = {}
        for url in re.findall(r'https?://[^"\'\s\\]+\.(?:jpg|jpeg|png|webp)[^"\'\s\\]*', html, re.I):
            vm = re.search(r'[A-HJ-NPR-Z0-9]{17}', url)
            if vm and _looks_like_vin(vm.group(0)):
                m.setdefault(vm.group(0).upper(), url)
        return m

    def _find_vin_json_objects(self, html):
        """Find every brace-balanced JSON object that contains a VIN key.
        Walks outward from each `"vin":"..."` to the enclosing { }."""
        objs, used = [], set()
        for m in re.finditer(r'"vin"\s*:\s*"[A-HJ-NPR-Z0-9]{11,17}"', html):
            start = html.rfind("{", 0, m.start())
            if start < 0 or start in used:
                continue
            depth, end = 0, None
            for i in range(start, min(len(html), start + 12000)):
                ch = html[i]
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        end = i + 1
                        break
            if end:
                used.add(start)
                try:
                    objs.append(json.loads(html[start:end]))
                except Exception:
                    continue
        return objs

    # ── vehicleDetails(...) handler (Vue/HomeNet dealer templates) ──
    # Some Vue-rendered sites embed each car in a click handler:
    #   vehicleDetails($event, "VIN", "URL", {msrp,yourPrice,sellingPrice,type,title})
    # The static HTML scatters the visible card, but this call has everything.

    _VD_RE = re.compile(
        r'vehicleDetails\(\$event,\s*"([A-HJ-NPR-Z0-9]{17})"\s*,\s*"([^"]+)"\s*,\s*\{([^}]*)\}',
        re.S)

    def _extract_vehicledetails_inventory(self, html, page_url, dealer) -> list:
        results, seen = [], set()
        for m in self._VD_RE.finditer(html):
            vin = m.group(1).upper()
            if vin in seen or not _looks_like_vin(vin):
                continue
            seen.add(vin)
            url = m.group(2).replace("&amp;", "&")
            obj = m.group(3)

            def field(key):
                fm = re.search(key + r"\s*:\s*[`\"']([^`\"']*)[`\"']", obj)
                return fm.group(1) if fm else None

            title = field("title") or ""
            price = _bounded_int(field("sellingPrice") or field("yourPrice")
                                 or field("msrp"), 100, 10_000_000)

            year = make = model = trim = None
            tp = title.split()
            if tp and tp[0].isdigit():
                year = _bounded_int(tp[0], 1900, 2100); tp = tp[1:]
            if tp:
                make = tp[0]
            if len(tp) >= 2:
                model = tp[1]
            if len(tp) > 2:
                trim = " ".join(tp[2:])

            if not (make or vin):
                continue
            results.append(self._listing(
                source_id      = vin,
                url            = urljoin(page_url, url),
                vin            = vin,
                year           = year,
                make           = make,
                model          = model,
                trim           = trim,
                price          = price,
                city           = dealer.get("city"),
                state          = dealer.get("state"),
                zip            = dealer.get("zip"),
                raw            = {"via": "vehicleDetails", "type": field("type"),
                                  "dealer": dealer.get("name")},
            ))
        return results

    # ── JSON-LD structured data (schema.org Vehicle/Car/Product) ──
    # Universal across many platforms (DealerFire, DealerOn VDPs, WordPress
    # plugins, independents). Listing pages embed one node per vehicle.

    def _extract_jsonld_inventory(self, html, page_url, dealer) -> list:
        nodes = self._collect_jsonld_vehicles(html)
        if not nodes:
            return []

        # VDP links to attach a real URL to each vehicle (matched by name slug).
        vdp_links = re.findall(
            r'href="([^"]*(?:/vehicle-details/|/vehicle/|/used[-/]|/new[-/]|/inventory/)[^"#?]*)"',
            html, re.I)
        vdp_links = list(dict.fromkeys(vdp_links))   # de-dupe, keep order

        results, seen = [], set()
        for node in nodes:
            v = self._jsonld_to_fields(node)
            if not (v.get("make") or v.get("vin")):
                continue
            if self._is_sold(v):          # drop sold / out-of-stock units
                continue
            # prefer the node's own canonical URL; else match an href / synthesize
            url = (urljoin(page_url, v["own_url"]) if v.get("own_url")
                   else self._match_vdp_url(v, vdp_links, page_url, dealer))
            if url in seen:
                continue
            seen.add(url)
            results.append(self._listing(
                source_id      = v.get("vin") or v.get("stock") or "",
                url            = url,
                vin            = v.get("vin"),
                year           = v.get("year"),
                make           = v.get("make"),
                model          = v.get("model"),
                trim           = v.get("trim"),
                mileage        = v.get("mileage"),
                price          = v.get("price"),
                exterior_color = v.get("color"),
                transmission   = v.get("transmission"),
                city           = dealer.get("city"),
                state          = dealer.get("state"),
                zip            = dealer.get("zip"),
                image_url      = v.get("image"),
                raw            = {"stock": v.get("stock"), "via": "json-ld",
                                  "dealer": dealer.get("name")},
            ))
        return results

    def _collect_jsonld_vehicles(self, html):
        """Return every schema.org Vehicle/Car/Product node found in <script
        type=application/ld+json> blocks (handles @graph and nested arrays)."""
        nodes = []

        def walk(o):
            if isinstance(o, dict):
                t = o.get("@type", "")
                types = t if isinstance(t, list) else [t]
                if any(x in ("Vehicle", "Car", "Product", "IndividualProduct") for x in types):
                    nodes.append(o)
                for val in o.values():
                    walk(val)
            elif isinstance(o, list):
                for val in o:
                    walk(val)

        for block in re.findall(
            r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>',
            html, re.DOTALL | re.I):
            try:
                walk(json.loads(block.strip()))
            except Exception:
                continue
        return nodes

    def _jsonld_to_fields(self, n):
        name = (n.get("name") or n.get("description") or "").strip()

        # make
        brand = n.get("brand") or n.get("manufacturer")
        make  = brand.get("name") if isinstance(brand, dict) else brand
        # model
        model = n.get("model")
        model = model.get("name") if isinstance(model, dict) else model
        # year
        year = _bounded_int(n.get("vehicleModelDate") or n.get("modelDate")
                            or n.get("productionDate") or n.get("releaseDate"), 1900, 2100)

        # Fill gaps from the "YEAR MAKE MODEL TRIM" name string.
        parts = name.split()
        if parts and not year:
            year = _bounded_int(parts[0], 1900, 2100)
        name_after_year = parts[1:] if (parts and parts[0].isdigit()) else parts
        if not make and name_after_year:
            make = name_after_year[0]
        if not model and len(name_after_year) >= 2:
            model = name_after_year[1]
        # trim = whatever's left in the name after year/make/model
        trim = None
        if name_after_year and make and model:
            tail = name_after_year[2:]
            trim = " ".join(tail) if tail else None

        # vin — proper field first, then mpn/sku if they look like a VIN
        vin = (n.get("vehicleIdentificationNumber") or n.get("vin")
               or (n.get("mpn") if _looks_like_vin(n.get("mpn")) else None)
               or (n.get("sku") if _looks_like_vin(n.get("sku")) else None))
        if vin and not _looks_like_vin(vin):
            vin = None
        vin = vin.upper() if vin else None

        # price — offers may be dict or list; price/lowPrice
        price = None
        availability = None
        offers = n.get("offers")
        if isinstance(offers, list):
            offers = offers[0] if offers else {}
        if isinstance(offers, dict):
            price = _bounded_int(offers.get("price") or offers.get("lowPrice"), 100, 10_000_000)
            availability = offers.get("availability")

        # mileage — mileageFromOdometer may be dict {value,unitCode} or number
        mileage = None
        odo = n.get("mileageFromOdometer")
        if isinstance(odo, dict):
            mileage = _bounded_int(odo.get("value"), 0, 2_000_000)
        elif odo is not None:
            mileage = _bounded_int(odo, 0, 2_000_000)

        # image — str or list
        image = n.get("image")
        if isinstance(image, list):
            image = image[0] if image else None
        if isinstance(image, dict):
            image = image.get("url")

        # transmission — schema.org uses vehicleTransmission (str or QuantitativeValue)
        trans = n.get("vehicleTransmission")
        if isinstance(trans, dict):
            trans = trans.get("name") or trans.get("value")

        # the node's own canonical page URL (schema.org uses url / @id /
        # mainEntityOfPage); strip a trailing #fragment so it points at the VDP
        # and isn't mistaken for a synthetic anchor.
        own_url = (n.get("url") or n.get("@id") or n.get("mainEntityOfPage")
                   or (offers.get("url") if isinstance(offers, dict) else None))
        if isinstance(own_url, dict):
            own_url = own_url.get("@id") or own_url.get("url")
        own_url = own_url.split("#")[0].strip() if isinstance(own_url, str) else None

        return {"year": year, "make": make, "model": model, "trim": trim,
                "vin": vin, "price": price, "mileage": mileage,
                "color": n.get("color"), "image": image, "transmission": trans,
                "stock": n.get("sku") or n.get("mpn"), "name": name,
                "availability": availability, "own_url": own_url or None}

    def _match_vdp_url(self, v, vdp_links, page_url, dealer):
        """Find the real listing URL for a JSON-LD vehicle by matching its VIN
        or name-slug against the page's VDP links; fall back to a synthetic key."""
        vin = (v.get("vin") or "").lower()
        slug = _slugify(v.get("name"))
        for href in vdp_links:
            h = href.lower()
            if vin and vin in h:
                return urljoin(page_url, href)
        if slug:
            for href in vdp_links:
                if slug and slug in _slugify(href):
                    return urljoin(page_url, href)
        # unique synthetic URL so the upsert (keyed on url) doesn't collide
        base = dealer.get("website", page_url).rstrip("/")
        return f"{base}/inventory#{v.get('vin') or v.get('stock') or slug}"

    # ── HTML field enrichment (soupsieve-free) ────────────────────
    # Structured extractors (JSON-LD, inline-JSON) routinely OMIT fields the
    # visible HTML still carries — most often mileage and VIN (e.g. carsforsale.com
    # SRP JSON-LD has no mileage; many independents' generic feeds drop it too).
    # Instead of trusting a single "winning" extractor, sweep the raw HTML for
    # those fields per vehicle card and backfill any listing that's missing them,
    # matched by VDP url (or VIN). Pure regex — never touches BeautifulSoup, so it
    # can't hit the Python-3.14 soupsieve crash. This is the general guard against
    # "the data was on the page but we didn't capture it".

    _ENRICH_LINK_RE = re.compile(
        r'href="([^"]*/(?:details|vehicle-details|vehicle|inventory|used|new|cars?|vin)[-/][^"#?]*)"',
        re.I)
    # number that looks like a real odometer reading (1–7 digits, optional commas)
    _MILEAGE_RE  = re.compile(r'Mileage\b\D{0,80}?(\d[\d,]{2,})', re.I)
    _MILES_RE    = re.compile(r'\b(\d[\d,]{2,})\s*(?:miles|mi)\b', re.I)
    _VIN_LABEL_RE = re.compile(r'VIN\b\D{0,30}?([A-HJ-NPR-Z0-9]{17})', re.I)
    _PRICE_RE    = re.compile(r'\$\s*(\d{1,3}(?:,\d{3})+)')

    @staticmethod
    def _url_key(url):
        """Stable, host-agnostic match key for a listing/VDP url: the lowercased
        path without a trailing slash (so an SRP-relative href and the stored
        absolute url collapse to the same key)."""
        if not url:
            return None
        try:
            path = urlparse(url if "://" in url else "http://x/" + url.lstrip("/")).path
        except Exception:
            return None
        return path.lower().rstrip("/") or None

    def _fields_from_segment(self, seg):
        """Pull mileage / vin / price out of one vehicle-card HTML fragment.
        Anchored on the literal labels ('Mileage', 'VIN', '$') so it doesn't grab
        unrelated numbers; every value is range-checked before it's trusted."""
        f = {}
        m = self._MILEAGE_RE.search(seg) or self._MILES_RE.search(seg)
        if m:
            mi = _bounded_int(_money_to_int(m.group(1)), 100, 1_000_000)
            if mi:
                f["mileage"] = mi
        vm = self._VIN_LABEL_RE.search(seg)
        vin = vm.group(1) if vm else next(
            (t for t in re.findall(r'[A-HJ-NPR-Z0-9]{17}', seg) if _looks_like_vin(t)), None)
        if vin and _looks_like_vin(vin):
            f["vin"] = vin.upper()
        pm = self._PRICE_RE.search(seg)
        if pm:
            p = _bounded_int(_money_to_int(pm.group(1)), 500, 10_000_000)
            if p:
                f["price"] = p
        return f

    def _html_field_map(self, html, page_url):
        """Map {url_key: {mileage,vin,price}} by slicing the HTML between
        consecutive VDP links — each slice is one vehicle card."""
        links = [(m.start(), m.group(1)) for m in self._ENRICH_LINK_RE.finditer(html)]
        out = {}
        for i, (pos, href) in enumerate(links):
            end = links[i + 1][0] if i + 1 < len(links) else min(len(html), pos + 4000)
            fields = self._fields_from_segment(html[pos:end])
            if not fields:
                continue
            key = self._url_key(urljoin(page_url, href))
            if key:
                out.setdefault(key, {}).update(fields)
        return out

    # ── Year/make/model from the VDP url slug ─────────────────────
    # Some platforms hand an extractor the url but not the vehicle's identity
    # (e.g. mazdageorgetown.com's vehicleDetails() call has no title, but the url
    # is /viewdetails/used/<vin>/2019-bmw-x3-sport-utility). The slug reliably
    # carries year-make-model, so parse it as a fallback.
    _MULTIWORD_MAKES = {"land rover", "alfa romeo", "aston martin", "mercedes benz",
                        "rolls royce"}
    _DRIVETRAIN = {"awd", "fwd", "rwd", "4wd", "2wd", "4x4", "4x2"}
    _BODY_SUFFIXES = ("sport utility", "4dr car", "2dr car", "3dr car", "5dr car",
                      "4dr suv", "crew cab", "extended cab", "quad cab", "mega cab",
                      "regular cab", "double cab", "king cab", "access cab",
                      "passenger van", "cargo van", "convertible", "coupe", "sedan",
                      "hatchback", "wagon", "pickup", "minivan", "van", "suv", "car")

    @staticmethod
    def _case_name(s):
        """Re-case a slug-derived name: short/numeric tokens upper (BMW, X3, CX-30,
        F-150), longer words capitalised (Bronco Sport, Grand Cherokee)."""
        out = []
        for tok in re.split(r'([ -])', s):
            if tok in (" ", "-"):
                out.append(tok)
            elif len(tok) <= 3 or any(c.isdigit() for c in tok):
                out.append(tok.upper())
            else:
                out.append(tok.capitalize())
        return "".join(out)

    def _ymm_from_url(self, url):
        """Parse {year, make, model} from a VDP url slug, or {} if none found."""
        if not url:
            return {}
        year, after = None, ""
        for seg in urlparse(url).path.lower().split("/"):
            # year must be a delimited token, not digits embedded in a vin/stock#
            ym = re.search(r'(?:^|[-_])(19[5-9]\d|20[0-3]\d)(?:[-_]|$)', seg)
            if ym:
                year, after = int(ym.group(1)), seg[ym.end(1):]
                break
        if year is None:
            return {}
        words = [w for w in re.split(r'[^a-z0-9]+', after) if w]
        while words and ((words[-1].isdigit() and len(words[-1]) >= 5)
                         or _looks_like_vin(words[-1])):     # trailing id / vin
            words.pop()
        if not words:
            return {"year": year}
        make, mi = words[0], 1
        if len(words) > 1 and f"{words[0]} {words[1]}" in self._MULTIWORD_MAKES:
            make, mi = f"{words[0]} {words[1]}", 2
        rest = " ".join(w for w in words[mi:] if w not in self._DRIVETRAIN)
        for suf in sorted(self._BODY_SUFFIXES, key=len, reverse=True):
            if rest == suf or rest.endswith(" " + suf):
                rest = rest[: len(rest) - len(suf)].strip()
                break
        out = {"year": year, "make": self._case_name(make)}
        if rest:
            mt = rest.split()
            model = mt[0]
            for t in mt[1:]:
                model += ("-" if (t.isdigit() or len(t) <= 2) else " ") + t
            out["model"] = self._case_name(model)
        return out

    def _enrich_listings(self, listings, html, page_url, whole_page=False):
        """Backfill fields an extractor missed but the page still holds:
          • year/make/model — from the listing's url slug
          • mileage/vin/price — from the HTML card text (regex, no soupsieve)
        SRP (whole_page=False): match each listing to its card by url/VIN.
        VDP (whole_page=True): the page is ONE vehicle — fill all from one sweep."""
        def fill_ymm(listing, src_url):
            if listing.get("make") and listing.get("model") and listing.get("year"):
                return
            ymm = self._ymm_from_url(src_url)
            for fld in ("year", "make", "model"):
                if listing.get(fld) in (None, "", 0) and ymm.get(fld):
                    listing[fld] = ymm[fld]

        if whole_page:
            src = self._fields_from_segment(html)
            for l in listings:
                fill_ymm(l, l.get("url") or page_url)
                for fld in ("mileage", "vin", "price"):
                    if l.get(fld) in (None, "", 0) and src.get(fld):
                        l[fld] = src[fld]
            return

        fmap = self._html_field_map(html, page_url)
        by_vin = {v["vin"]: v for v in fmap.values() if v.get("vin")} if fmap else {}
        for l in listings:
            fill_ymm(l, l.get("url"))
            src = fmap.get(self._url_key(l.get("url"))) if fmap else None
            if not src and l.get("vin"):
                src = by_vin.get(l["vin"])
            if not src:
                continue
            for fld in ("mileage", "vin", "price"):
                if l.get(fld) in (None, "", 0) and src.get(fld):
                    l[fld] = src[fld]

    def _parse_html_inventory(self, html, base_url, dealer) -> list:
        soup    = BeautifulSoup(html, "html.parser")
        results = []

        card_selectors = [
            "div.vehicle-card", "div.inventory-item", "div.vehicle-item",
            "article.vehicle", "[class*='vehicle-card']", "li.vehicle",
            "[class*='inventory-card']", "[class*='listing-item']",
            "[class*='vehicle-listing']", "[class*='srp-vehicle']",
            "[itemtype*='Vehicle']", "[class*='vehicle-box']",
        ]
        # Pick the selector that yields the most cards (avoids matching a single
        # wrapper element and missing the real grid).
        cards = []
        for sel in card_selectors:
            found = soup.select(sel)
            if len(found) > len(cards):
                cards = found

        seen_urls = set()
        for card in cards[:150]:
            try:
                title_el = card.select_one("h2, h3, h4, .vehicle-title, [class*='title']")
                price_el = card.select_one(".price, .vehicle-price, [class*='price']")
                link_el  = card.select_one("a[href]")
                img_el   = card.select_one("img")
                mile_el  = card.select_one("[class*='mileage'], [class*='miles'], [class*='odometer']")

                title_text = title_el.get_text(" ", strip=True) if title_el else ""
                price_text = price_el.get_text(" ", strip=True) if price_el else ""
                card_text  = card.get_text(" ", strip=True)

                price   = _bounded_int(_money_to_int(price_text) if re.search(r"\d{4,}", price_text) else None, 100, 10_000_000)
                if not price:   # fallback: first "$NN,NNN" in the card text
                    pm = re.search(r"\$\s?([1-9]\d{0,2}(?:,\d{3})+)", card_text)
                    if pm:
                        price = _bounded_int(_money_to_int(pm.group(1)), 100, 10_000_000)

                # mileage: dedicated element first, else "12,345 miles" in text
                mileage = None
                if mile_el:
                    mileage = _bounded_int(_money_to_int(mile_el.get_text(strip=True)), 0, 2_000_000)
                if not mileage:
                    mm = re.search(r"([\d,]{3,})\s*(?:miles|mi)\b", card_text, re.I)
                    if mm:
                        mileage = _bounded_int(_money_to_int(mm.group(1)), 0, 2_000_000)

                href = link_el["href"] if link_el else ""
                if href and not href.startswith("http"):
                    href = urljoin(base_url, href)

                # VIN: "VIN: xxxx" in text, a bare 17-char token, or in the href
                vin = None
                vm = re.search(r"VIN[\s:#]*([A-HJ-NPR-Z0-9]{17})", card_text, re.I)
                if vm and _looks_like_vin(vm.group(1)):
                    vin = vm.group(1).upper()
                if not vin:
                    for tok in re.findall(r"[A-HJ-NPR-Z0-9]{17}", card_text + " " + href):
                        if _looks_like_vin(tok):
                            vin = tok.upper(); break

                # Trim noise (stock/VIN/price) off the title before splitting.
                clean_title = re.split(r"\b(?:Stock|VIN|Mileage|Miles)\b|\$|\|",
                                       title_text, 1, flags=re.I)[0].strip()
                year_m = re.search(r"\b(19[5-9]\d|20[0-2]\d)\b", clean_title)
                year   = int(year_m.group(1)) if year_m else None

                # Split "2021 Toyota Tacoma TRD Sport" → make/model/trim
                make = model = trim = None
                tp = clean_title.split()
                if year and len(tp) >= 3:
                    make  = tp[1]
                    model = tp[2]
                    trim  = " ".join(tp[3:]) or None

                img_url = None
                if img_el:
                    img_url = img_el.get("src") or img_el.get("data-src") or img_el.get("data-lazy-src")

                # transmission: look for a keyword in the card text
                trans = None
                tm = re.search(r"\b(manual|automatic|cvt|dual[\s-]?clutch|automated manual)\b",
                               card_text, re.I)
                if tm:
                    trans = tm.group(1)

                url = href or f"{base_url}#{vin or _slugify(title_text)}"
                if url in seen_urls:
                    continue
                seen_urls.add(url)

                if not (make or vin):     # skip rows with no usable identity
                    continue

                results.append(self._listing(
                    source_id = vin or "",
                    url       = url,
                    vin       = vin,
                    year      = year,
                    make      = make,
                    model     = model,
                    trim      = trim,
                    price     = price,
                    mileage   = mileage,
                    transmission = trans,
                    city      = dealer.get("city"),
                    state     = dealer.get("state"),
                    zip       = dealer.get("zip"),
                    image_url = img_url,
                    raw       = {"html_title": title_text, "via": "html-card",
                                 "dealer": dealer.get("name")},
                ))
            except Exception:
                continue

        return results

    # ── Sitemap → VDP → structured-data fallback ──────────────────
    # For JS-rendered SRPs that embed nothing usable: most dealer sites still
    # publish a sitemap of every vehicle-detail page (VDP), and individual VDPs
    # almost always carry JSON-LD / Next.js data even when the listing grid does
    # not. Pull VDP URLs from the sitemap(s), then parse each page.

    _VDP_URL_RE = re.compile(r"/(vehicle|vehicle-details|vdp|inventory|used|new|cars?|vin)[-/]", re.I)

    def _scrape_via_sitemap(self, base, dealer, max_vdps=120) -> list:
        vdp_urls = self._sitemap_vdp_urls(base)
        if not vdp_urls:
            return []

        results, seen = [], set()
        for u in vdp_urls[:max_vdps]:
            resp = self._get_raw(u)
            if not resp or resp.status_code != 200:
                continue
            got = (self._extract_jsonld_inventory(resp.text, u, dealer)
                   or self._extract_nextdata_inventory(resp.text, u, dealer)
                   or self._extract_inline_vehicle_json(resp.text, u, dealer))
            for l in got:
                # This is a single VDP page, so its url IS the canonical listing
                # link — use it whenever the per-page extractor could only
                # synthesize an /inventory#vin anchor.
                if "#" in (l.get("url") or ""):
                    l["url"] = u
            # A VDP is one vehicle — backfill year/make/model (from the url slug)
            # and mileage/vin/price (from the page HTML) that the parse missed.
            self._enrich_listings(got, resp.text, u, whole_page=True)
            for l in got:
                key = l.get("vin") or l.get("url")
                if key and key not in seen:
                    seen.add(key)
                    results.append(l)
        return results

    def _sitemap_vdp_urls(self, base) -> list:
        """Collect candidate VDP URLs from robots.txt-declared and well-known
        sitemaps, following one level of <sitemapindex> nesting."""
        sitemaps = []
        rb = self._get_raw(base + "/robots.txt")
        if rb and rb.status_code == 200:
            sitemaps += re.findall(r"(?im)^\s*sitemap:\s*(\S+)", rb.text)
        sitemaps += [base + p for p in
                     ("/sitemap.xml", "/sitemap_index.xml", "/sitemap-index.xml",
                      "/vehicle-sitemap.xml", "/inventory-sitemap.xml")]

        seen_sm, locs = set(), []
        queue = list(dict.fromkeys(sitemaps))
        while queue and len(seen_sm) < 25:
            sm = queue.pop(0)
            if sm in seen_sm:
                continue
            seen_sm.add(sm)
            resp = self._get_raw(sm)
            if not resp or resp.status_code != 200 or "<" not in resp.text:
                continue
            found = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", resp.text, re.I)
            if "<sitemapindex" in resp.text.lower():
                queue += [u for u in found if u.lower().endswith(".xml")]
            else:
                locs += found

        return list(dict.fromkeys(u for u in locs if self._VDP_URL_RE.search(u)))

    # ── Helpers ───────────────────────────────────────────────

    def _geocode_city(self, city, state):
        key = f"{city},{state}"
        if key in self._coord_cache:
            return self._coord_cache[key]
        try:
            time.sleep(0.5)
            loc = self._geo.geocode(f"{city}, {state}, USA", exactly_one=True)
            if loc:
                result = (loc.latitude, loc.longitude)
                self._coord_cache[key] = result
                return result
        except Exception:
            pass
        return None

    _STATE_ABBRS = {
        "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
        "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
        "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID",
        "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
        "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
        "massachusetts": "MA", "michigan": "MI", "minnesota": "MN", "mississippi": "MS",
        "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
        "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY",
        "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK",
        "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC",
        "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT",
        "vermont": "VT", "virginia": "VA", "washington": "WA", "west virginia": "WV",
        "wisconsin": "WI", "wyoming": "WY",
    }

    def _state_name_to_abbr(self, name):
        return self._STATE_ABBRS.get(name.lower(), "")
