"""
Base scraper class — all scrapers inherit from this.
Every scraper must implement: search(config) -> list[dict]
"""

import time
import random
from abc import ABC, abstractmethod

# curl_cffi impersonates a real Chrome TLS fingerprint — plain `requests`
# gets 403/406 from sites behind Cloudflare/Akamai bot management
# (dealer websites, CarMax, CarGurus). It sets browser headers itself.
from curl_cffi import requests

HEADERS = {
    "Accept-Language": "en-US,en;q=0.9",
}

# A car listing priced below this is almost certainly a parse error (a monthly
# payment, deposit, doc fee, or $0 placeholder) rather than a real asking price.
# Such values are dropped to None so they can't pollute results or win the
# "cheapest" tiebreak during deduplication.
MIN_SANE_PRICE = 500
MAX_SANE_PRICE = 10_000_000


class BaseScraper(ABC):
    name = "base"

    # (min, max) seconds to sleep before each request. Bulk classification runs
    # can lower this (e.g. (0.3, 0.8)) since they touch each domain only a few times.
    delay_range = (1.5, 3.5)
    timeout = 15

    def __init__(self):
        self.session = requests.Session(impersonate="chrome")
        self.session.headers.update(HEADERS)

    def _get(self, url, params=None, **kwargs):
        """Polite GET with random delay to avoid getting blocked."""
        time.sleep(random.uniform(*self.delay_range))
        try:
            r = self.session.get(url, params=params, timeout=self.timeout, **kwargs)
            r.raise_for_status()
            return r
        except Exception as e:
            print(f"[{self.name}] Request failed: {e}")
            return None

    def _get_raw(self, url, params=None, **kwargs):
        """GET that returns the response WITHOUT raising on 4xx/5xx, so callers
        can inspect the status code (needed for per-dealer diagnostics).
        Returns the response, or None on a connection-level error."""
        time.sleep(random.uniform(*self.delay_range))
        try:
            return self.session.get(url, params=params, timeout=self.timeout, **kwargs)
        except Exception as e:
            print(f"[{self.name}] Request error: {e}")
            return None

    # Status codes worth retrying — transient throttling / gateway hiccups.
    _RETRY_CODES = (429, 500, 502, 503, 504)

    def _get_retry(self, url, params=None, retries=2, **kwargs):
        """GET with retries on transient failures (connection error / 429 / 5xx).
        Returns the response (any final status) or None only if every attempt
        failed at the connection level.

        Use this for PAGINATED crawls: a single flaky page must not silently
        truncate a dealer's inventory (the old `if not resp: break` did exactly
        that). On a clean 4xx (e.g. real 404 past the last page) it returns
        immediately so the caller can stop normally."""
        for attempt in range(retries + 1):
            time.sleep(random.uniform(*self.delay_range))
            try:
                r = self.session.get(url, params=params, timeout=self.timeout, **kwargs)
            except Exception as e:
                if attempt < retries:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                print(f"[{self.name}] GET failed after {retries + 1} tries: {e}")
                return None
            if r.status_code in self._RETRY_CODES and attempt < retries:
                time.sleep(2.0 * (attempt + 1))
                continue
            return r
        return None

    def _post_retry(self, url, json=None, headers=None, retries=2, **kwargs):
        """POST with the same transient-failure retry policy as `_get_retry`.
        Used by paginated JSON inventory APIs (Dealer Inspire / Cars Commerce)."""
        for attempt in range(retries + 1):
            time.sleep(random.uniform(0.4, 1.0))
            try:
                r = self.session.post(url, json=json, headers=headers, timeout=20, **kwargs)
            except Exception as e:
                if attempt < retries:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                print(f"[{self.name}] POST failed after {retries + 1} tries: {e}")
                return None
            if r.status_code in self._RETRY_CODES and attempt < retries:
                time.sleep(2.0 * (attempt + 1))
                continue
            return r
        return None

    def _post(self, url, json=None, headers=None, delay=(0.4, 1.0), **kwargs):
        """Polite POST. Used for JSON inventory APIs (shorter delay — these hit
        vendor CDNs/APIs, not the dealer's own server)."""
        time.sleep(random.uniform(*delay))
        try:
            r = self.session.post(url, json=json, headers=headers, timeout=20, **kwargs)
            r.raise_for_status()
            return r
        except Exception as e:
            print(f"[{self.name}] POST failed: {e}")
            return None

    @abstractmethod
    def search(self, config: dict) -> list:
        """
        config keys: make, model, year_min, year_max, price_min, price_max,
                     mileage_max, zip, radius_mi
        Returns list of listing dicts (see database.upsert_listing for schema).
        """
        pass

    @staticmethod
    def _sane_price(price):
        """Drop implausible prices (payments/fees/$0) to None. See MIN_SANE_PRICE."""
        if price is None:
            return None
        try:
            price = int(price)
        except (ValueError, TypeError):
            return None
        return price if MIN_SANE_PRICE <= price <= MAX_SANE_PRICE else None

    def _listing(self, **kwargs) -> dict:
        """Helper to build a consistently shaped listing dict."""
        return {
            "source":        self.name,
            "source_id":     kwargs.get("source_id"),
            "url":           kwargs.get("url"),
            "vin":           kwargs.get("vin"),
            "year":          kwargs.get("year"),
            "make":          kwargs.get("make"),
            "model":         kwargs.get("model"),
            "trim":          kwargs.get("trim"),
            "exterior_color":kwargs.get("exterior_color"),
            "transmission":  kwargs.get("transmission"),
            "mileage":       kwargs.get("mileage"),
            "price":         self._sane_price(kwargs.get("price")),
            "msrp":          kwargs.get("msrp"),
            "city":          kwargs.get("city"),
            "state":         kwargs.get("state"),
            "zip":           kwargs.get("zip"),
            "distance_mi":   kwargs.get("distance_mi"),
            "image_url":     kwargs.get("image_url"),
            "image_urls":    kwargs.get("image_urls", []),
            "raw":           kwargs.get("raw", {}),
        }
