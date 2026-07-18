"""
discovery/merge.py — cross-source merge/dedupe for discovery candidates.

Two matching keys, checked in order:
  1. Canonical website — if two candidates resolve to the same host, they're
     the same dealer, full stop.
  2. Normalized name + zip5, rapidfuzz token_set_ratio >= NAME_ZIP_THRESHOLD —
     the fallback for the common case where a registry candidate (addresses
     only, no website) needs to line up with an OSM/Places candidate.

Conflict resolution per field, once a group is formed: registry candidates
win name/address/city/state/zip/phone (a state license record is the
ground truth for who-and-where); OSM/Places candidates win website/lat/lng
(registries never carry either); first non-empty value wins as the fallback.
"""
from rapidfuzz import fuzz

from discovery.base import Candidate, canonical_website, normalize_name, zip5

NAME_ZIP_THRESHOLD = 90

# Attribution priority when picking which source/source_id represents a
# merged group (used downstream as _ensure_dealer's #2 match key): OSM node
# ids are the most stable/granular identifier available, so OSM wins if
# present; Places next; state registries (no stable per-dealer id beyond a
# license number that can change) sort last. Not specified by the plan text —
# a filling-in-the-blank decision, recorded here and in docs/PROGRESS.md.
_SOURCE_RANK = {"osm": 0, "places": 1}


def _is_registry(candidate):
    return (candidate.source or "").startswith("registry")


def _source_rank(candidate):
    return _SOURCE_RANK.get(candidate.source, 2)


def _name_zip_match(a, b):
    za, zb = zip5(a.zip), zip5(b.zip)
    if not za or za != zb:
        return False
    na, nb = normalize_name(a.name), normalize_name(b.name)
    if not na or not nb:
        return False
    return fuzz.token_set_ratio(na, nb) >= NAME_ZIP_THRESHOLD


def _first_nonempty(members, attr):
    for m in members:
        v = getattr(m, attr)
        if v not in (None, ""):
            return v
    return None


def _resolve_group(members):
    registry_first = sorted(members, key=lambda m: 0 if _is_registry(m) else 1)
    other_first = sorted(members, key=lambda m: 1 if _is_registry(m) else 0)
    primary = min(members, key=_source_rank)

    return Candidate(
        name=_first_nonempty(registry_first, "name"),
        address=_first_nonempty(registry_first, "address"),
        city=_first_nonempty(registry_first, "city"),
        state=_first_nonempty(registry_first, "state"),
        zip=_first_nonempty(registry_first, "zip"),
        phone=_first_nonempty(registry_first, "phone"),
        website=_first_nonempty(other_first, "website"),
        lat=_first_nonempty(other_first, "lat"),
        lng=_first_nonempty(other_first, "lng"),
        source=primary.source,
        source_id=primary.source_id,
    )


def merge_candidates(candidates):
    """Merge a flat list of Candidates from any mix of sources into one
    Candidate per physical dealer. Order-independent; a candidate that
    matches nothing else passes through unchanged."""
    n = len(candidates)
    parent = list(range(n))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i, j):
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[ri] = rj

    # Pass 1: canonical website, O(n) dict grouping.
    by_website = {}
    for i, c in enumerate(candidates):
        w = canonical_website(c.website)
        if w:
            by_website.setdefault(w, []).append(i)
    for idxs in by_website.values():
        for i in idxs[1:]:
            union(idxs[0], i)

    # Pass 2: normalized name + zip5, bucketed by zip5 so this stays near-linear
    # instead of comparing every candidate against every other candidate.
    by_zip = {}
    for i, c in enumerate(candidates):
        z = zip5(c.zip)
        if z:
            by_zip.setdefault(z, []).append(i)
    for idxs in by_zip.values():
        for a in range(len(idxs)):
            for b in range(a + 1, len(idxs)):
                i, j = idxs[a], idxs[b]
                if find(i) != find(j) and _name_zip_match(candidates[i], candidates[j]):
                    union(i, j)

    groups = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)

    return [_resolve_group([candidates[i] for i in idxs]) for idxs in groups.values()]
