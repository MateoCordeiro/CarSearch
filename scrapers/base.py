"""
Base scraper class — all scrapers inherit from this.
Every scraper must implement: search(config) -> list[dict]
"""

import time
import random
import os
import re
import json
import hashlib
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


class _CachedResp:
    """Minimal stand-in for a curl_cffi Response served from the disk cache —
    exposes just what the scrapers use (.text / .status_code / .url / .json())."""
    def __init__(self, text, status_code, url):
        self.text = text
        self.status_code = status_code
        self.url = url

    def json(self):
        return json.loads(self.text)


class BaseScraper(ABC):
    name = "base"

    # (min, max) seconds to sleep before each request. Bulk classification runs
    # can lower this (e.g. (0.3, 0.8)) since they touch each domain only a few times.
    delay_range = (1.5, 3.5)
    timeout = 15

    # ── Optional on-disk HTTP cache (opt-in; default OFF so live scans stay
    # fresh). Turn on (cache_enabled=True) for VDP enrichment, fixture capture,
    # and dev re-runs so we don't refetch the same pages or hammer dealer sites.
    cache_enabled = False
    cache_ttl     = 86_400                                   # seconds
    cache_dir     = os.path.join("data", "http_cache")
    # When set to a directory, raw HTML of fetched pages is dumped there — used
    # to debug 'unsupported'/'blocked' dealers and to build test fixtures.
    debug_save_dir = None

    def __init__(self):
        self.session = requests.Session(impersonate="chrome")
        self.session.headers.update(HEADERS)

    # ── Disk cache helpers ────────────────────────────────────────
    def _cache_key(self, url, params):
        raw = url + ("?" + json.dumps(params, sort_keys=True) if params else "")
        return hashlib.sha256(raw.encode("utf-8", "ignore")).hexdigest()

    def _cache_load(self, url, params):
        if not self.cache_enabled:
            return None
        path = os.path.join(self.cache_dir, self._cache_key(url, params) + ".json")
        try:
            if time.time() - os.path.getmtime(path) > self.cache_ttl:
                return None
            with open(path, "r", encoding="utf-8") as f:
                d = json.load(f)
            return _CachedResp(d["text"], d["status_code"], d.get("url", url))
        except Exception:
            return None

    def _cache_store(self, url, params, resp):
        if not self.cache_enabled or resp is None:
            return
        try:
            os.makedirs(self.cache_dir, exist_ok=True)
            path = os.path.join(self.cache_dir, self._cache_key(url, params) + ".json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"text": resp.text, "status_code": resp.status_code,
                           "url": getattr(resp, "url", url)}, f)
        except Exception:
            pass

    def _save_debug(self, label, text):
        """Dump raw HTML for later inspection / fixtures (no-op unless
        debug_save_dir is set)."""
        if not self.debug_save_dir or not text:
            return
        try:
            os.makedirs(self.debug_save_dir, exist_ok=True)
            safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", label)[:120] or "page"
            with open(os.path.join(self.debug_save_dir, safe + ".html"),
                      "w", encoding="utf-8") as f:
                f.write(text)
        except Exception:
            pass

    def _get(self, url, params=None, **kwargs):
        """Polite GET with random delay to avoid getting blocked."""
        cached = self._cache_load(url, params)
        if cached is not None:
            return cached if cached.status_code < 400 else None
        time.sleep(random.uniform(*self.delay_range))
        try:
            r = self.session.get(url, params=params, timeout=self.timeout, **kwargs)
            r.raise_for_status()
            self._cache_store(url, params, r)
            return r
        except Exception as e:
            print(f"[{self.name}] Request failed: {e}")
            return None

    def _get_raw(self, url, params=None, **kwargs):
        """GET that returns the response WITHOUT raising on 4xx/5xx, so callers
        can inspect the status code (needed for per-dealer diagnostics).
        Returns the response, or None on a connection-level error."""
        cached = self._cache_load(url, params)
        if cached is not None:
            return cached
        time.sleep(random.uniform(*self.delay_range))
        try:
            r = self.session.get(url, params=params, timeout=self.timeout, **kwargs)
        except Exception as e:
            print(f"[{self.name}] Request error: {e}")
            return None
        if r is not None and r.status_code == 200:
            self._cache_store(url, params, r)
        return r

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
        cached = self._cache_load(url, params)
        if cached is not None:
            return cached
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
            if r.status_code == 200:
                self._cache_store(url, params, r)
            return r
        return None

    def _get_many(self, urls, workers=5):
        """Fetch many URLs concurrently with a small bounded thread pool; returns
        {url: response-or-None}. Each worker uses its OWN curl_cffi session
        (sessions aren't thread-safe). The disk cache is honored (cache hits skip
        the network and the delay). Keep `workers` modest/browser-like — these are
        often all ONE dealer's host, so this is the per-host politeness cap, not a
        license to flood. Used for the per-vehicle (VDP) fetch passes, which are
        otherwise the dominant cost (e.g. ~700 sequential VDP fetches ≈ 30 min)."""
        from concurrent.futures import ThreadPoolExecutor
        import threading
        if not urls:
            return {}
        local = threading.local()

        def _one(u):
            cached = self._cache_load(u, None)
            if cached is not None:
                return u, cached
            sess = getattr(local, "sess", None)
            if sess is None:
                sess = local.sess = requests.Session(impersonate="chrome")
                sess.headers.update(HEADERS)
            time.sleep(random.uniform(*self.delay_range))
            try:
                r = sess.get(u, timeout=self.timeout)
            except Exception:
                return u, None
            if r is not None and r.status_code == 200:
                self._cache_store(u, None, r)
            return u, r

        out = {}
        with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
            for u, r in ex.map(_one, list(urls)):
                out[u] = r
        return out

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

    # Payment/lease context around a dollar amount in free text. Both regexes
    # are applied ANCHORED at the amount's boundaries — the suffix must start
    # immediately after it ("$299/mo", "$1,500 down") and the prefix must end
    # immediately before it ("lease for $4,999") — so a "Leasing" link elsewhere
    # on the page can never poison a legitimate "$2,500" price.
    _PAY_AFTER_RE = re.compile(r"""
        ^[\s*†]*(?:
            /\s*(?:mo|mos|month|wk|week)\b
          | per\s+(?:month|week)\b
          | a\s+month\b
          | monthly\b
          | bi-?weekly\b
          | down\b
          | due\s+at\s+(?:signing|delivery)\b
          | apr\b
          | deposit\b
        )""", re.I | re.X)
    _PAY_BEFORE_RE = re.compile(r"""
        (?:
            leas(?:e|ing)(?:\s+(?:for|from|at|special))?
          | payments?\s+(?:of|from|as\s+low\s+as|starting\s+at)?
          | est(?:\.|imated)?\s*payments?
          | finance\s+(?:for|from)
          | as\s+low\s+as
          | down\s+payment(?:\s+of)?
          | money\s+down
          | drive\s+(?:home|away)\s+for
        )\s*:?\s*(?:only\s+|just\s+)?$""", re.I | re.X)

    @classmethod
    def _is_payment_context(cls, text, start, end, before=48, after=24):
        """True when the dollar amount at text[start:end] reads as a monthly/
        weekly payment, money-down, or lease figure rather than an asking price."""
        if cls._PAY_AFTER_RE.match(text[end:end + after]):
            return True
        return bool(cls._PAY_BEFORE_RE.search(text[max(0, start - before):start]))

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
