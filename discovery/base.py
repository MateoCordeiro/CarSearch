"""
discovery/base.py — shared Candidate schema, provider base, and normalizers
used by every discovery source (OSM, state registries, Places).

Nothing here talks to the network or the database; it's pure data shape +
string normalization so osm.py/registry.py/places.py/merge.py all agree on
what "the same dealer" means.
"""
import re
from dataclasses import dataclass


@dataclass
class Candidate:
    """One dealer as seen by one discovery source. `source` is a short tag
    ('osm', 'registry:TX', 'registry:FL', 'places'); `source_id` is whatever
    id that source uses internally (OSM node/way id, state license number,
    Places place_id)."""
    name: str = None
    address: str = None
    city: str = None
    state: str = None
    zip: str = None
    lat: float = None
    lng: float = None
    website: str = None
    phone: str = None
    source: str = None
    source_id: str = None


class DiscoveryProvider:
    """Base for a discovery source. Subclasses implement find(); a provider
    must never raise past its own find() call — return [] on total failure
    and let the caller (run_discovery) log it. One broken source must never
    block or crash a run that has other sources to try."""
    name = "base"

    def find(self, lat, lng, radius_mi):
        raise NotImplementedError


_SUFFIX_TOKENS = {"llc", "inc", "co", "lp", "ltd"}
_PUNCT_RE = re.compile(r"[^\w\s]")


def canonical_website(url):
    """Lowercase scheme+host, strip 'www.', drop path/query/fragment/port ->
    'https://{host}'. Returns None for empty/unparseable input. Every match,
    upsert, or comparison involving a website must go through this — it's
    the one place that decides what "the same site" means, so two code paths
    can never quietly disagree (the class of UNIQUE-collision bug the plan
    calls out)."""
    if not url:
        return None
    from urllib.parse import urlparse
    u = url.strip()
    if "://" not in u:
        u = "http://" + u
    host = urlparse(u).netloc.lower()
    host = re.sub(r"^www\.", "", host).split(":")[0]
    if not host or "." not in host:
        return None
    return f"https://{host}"


def normalize_name(name):
    """Casefold, strip punctuation, drop common corporate-suffix tokens
    (llc/inc/co/lp/ltd) for name+zip fuzzy matching. 'motors' is deliberately
    NOT stripped — it's part of plenty of real dealer names, not boilerplate."""
    if not name:
        return ""
    n = _PUNCT_RE.sub(" ", name.casefold())
    tokens = [t for t in n.split() if t not in _SUFFIX_TOKENS]
    return " ".join(tokens).strip()


def zip5(z):
    """First 5 digits of a ZIP, or "" if there aren't 5. Tolerates ZIP+4,
    stray whitespace, and non-string input from spotty source data."""
    if not z:
        return ""
    digits = "".join(c for c in str(z) if c.isdigit())
    return digits[:5] if len(digits) >= 5 else ""
