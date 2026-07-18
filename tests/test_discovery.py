"""
Offline tests for the discovery package (docs/PLAN-discovery.md).

Currently covers plan step 2: discovery/base.py's normalizers and
discovery/merge.py's cross-source merge. Later steps (OSM fixtures, registry
fixtures, website-resolution chain, idempotent orchestrator run, stale-origin
recompute) get appended here as they land, per the plan's single
tests/test_discovery.py convention.

Run:  python tests/test_discovery.py        (standalone)
  or: pytest tests/                         (if pytest is installed)
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database

# Point the whole database layer at a temp file BEFORE anything opens a conn —
# same convention as tests/test_duplicates.py. Only the OSM reverse-ZIP-fill
# tests below actually touch the DB.
_tmpdir = tempfile.mkdtemp(prefix="bumperscraper-test-")
database.DB_PATH = os.path.join(_tmpdir, "test.db")
database.init_db()

from discovery.base import Candidate, canonical_website, normalize_name, zip5
from discovery.merge import merge_candidates
from discovery.osm import candidates_from_payload, nearest_zip
from discovery.registry import (_tx_rows_from_sheet, _parse_fl_table,
                                 states_in_radius, RegistryProvider)
from discovery.websites import resolve_website, should_attempt, _is_blocked, _ddg_search
from discovery.places import PlacesBudget, search_website

FIXTURES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "discovery")


def _seed_zip_coords():
    conn = database.get_conn()
    conn.execute(
        "INSERT OR REPLACE INTO zip_coords (zip, lat, lng, city, state) VALUES (?,?,?,?,?)",
        ("78664", 30.5083, -97.6789, "Round Rock", "TX"),
    )
    conn.commit()
    conn.close()


_seed_zip_coords()


def _load_osm_fixture():
    with open(os.path.join(FIXTURES_DIR, "osm_sample.json")) as f:
        return json.load(f)


# ── canonical_website ──
def test_canonical_website_normalizes():
    assert canonical_website("http://www.Example.com/inventory?x=1") == "https://example.com"
    assert canonical_website("https://example.com") == "https://example.com"
    assert canonical_website("example.com") == "https://example.com"
    assert canonical_website("WWW.EXAMPLE.COM") == "https://example.com"
    assert canonical_website("example.com:8080/path") == "https://example.com"


def test_canonical_website_rejects_junk():
    assert canonical_website(None) is None
    assert canonical_website("") is None
    assert canonical_website("not a url, no dot") is None


def test_canonical_website_www_and_bare_agree():
    # the exact case the plan calls out: two spellings of the same site must
    # collapse to one key, or the UNIQUE-collision bug comes right back.
    assert canonical_website("www.roundrocktoyota.com") == canonical_website("roundrocktoyota.com")


# ── normalize_name ──
def test_normalize_name_strips_suffixes_keeps_motors():
    assert normalize_name("Round Rock Toyota, LLC") == "round rock toyota"
    assert normalize_name("ABC Motors Inc.") == "abc motors"
    assert normalize_name("Smith Auto Co") == "smith auto"


def test_normalize_name_empty():
    assert normalize_name(None) == ""
    assert normalize_name("") == ""


# ── zip5 ──
def test_zip5_variants():
    assert zip5("78664") == "78664"
    assert zip5("78664-1234") == "78664"
    assert zip5("786") == ""
    assert zip5(None) == ""
    assert zip5(78664) == "78664"


# ── merge_candidates: domain collapse ──
def test_merge_domain_collapse():
    a = Candidate(name="Round Rock Toyota", website="https://roundrocktoyota.com",
                  zip="78664", source="osm", source_id="node/1")
    b = Candidate(name="Round Rock Toyota Inc", website="http://www.RoundRockToyota.com/",
                  zip="78664", source="registry:TX", source_id="TX-DLR-001")
    merged = merge_candidates([a, b])
    assert len(merged) == 1
    assert merged[0].website == "https://roundrocktoyota.com"


# ── merge_candidates: name+zip collapse (no website on one side) ──
def test_merge_name_zip_collapse_no_website():
    a = Candidate(name="Georgetown Mazda", website="https://georgetownmazda.com",
                  lat=30.6, lng=-97.7, zip="78628", source="osm", source_id="node/2")
    b = Candidate(name="Georgetown Mazda LLC", address="123 Main St",
                  zip="78628", phone="512-555-0100", source="registry:TX",
                  source_id="TX-DLR-002")
    merged = merge_candidates([a, b])
    assert len(merged) == 1
    m = merged[0]
    # registry wins address/phone (osm candidate had neither)
    assert m.address == "123 Main St"
    assert m.phone == "512-555-0100"
    # osm wins website/lat/lng
    assert m.website == "https://georgetownmazda.com"
    assert m.lat == 30.6


# ── merge_candidates: distinct dealers never collapse ──
def test_merge_distinct_survive():
    a = Candidate(name="Georgetown Mazda", website="https://georgetownmazda.com",
                  zip="78628", source="osm", source_id="node/2")
    b = Candidate(name="Georgetown Honda", website="https://georgetownhonda.com",
                  zip="78628", source="osm", source_id="node/3")
    c = Candidate(name="Some Auto Repair Shop", zip="10001", source="osm", source_id="node/4")
    merged = merge_candidates([a, b, c])
    assert len(merged) == 3


# ── merge_candidates: source attribution priority (osm > places > registry) ──
def test_merge_source_attribution_prefers_osm():
    a = Candidate(name="Test Motors", zip="99999", source="registry:FL", source_id="FL-1")
    b = Candidate(name="Test Motors", website="https://testmotors.example",
                  zip="99999", source="osm", source_id="node/9")
    merged = merge_candidates([a, b])
    assert len(merged) == 1
    assert merged[0].source == "osm"
    assert merged[0].source_id == "node/9"


# ── merge_candidates: transitive collapse across 3+ sources for one dealer ──
def test_merge_transitive_three_sources():
    a = Candidate(name="Central Ford", website="https://centralford.example",
                  zip="73301", source="osm", source_id="node/5")
    b = Candidate(name="Central Ford", zip="73301", address="1 Ford Way",
                  source="registry:TX", source_id="TX-9")
    c = Candidate(name="Central Ford", website="https://www.centralford.example",
                  zip="73301", source="places", source_id="place-1")
    merged = merge_candidates([a, b, c])
    assert len(merged) == 1
    assert merged[0].address == "1 Ford Way"


# ── discovery/osm.py: fixture payload -> Candidates ──
def test_osm_node_with_website_and_addr_tags():
    payload = _load_osm_fixture()
    conn = database.get_conn()
    candidates = candidates_from_payload(payload, conn)
    conn.close()
    rrt = next(c for c in candidates if c.name == "Round Rock Toyota")
    assert rrt.website == "https://www.roundrocktoyota.com"
    assert rrt.zip == "78664"
    assert rrt.city == "Round Rock"
    assert rrt.source == "osm"
    assert rrt.source_id == "node/1001"
    assert rrt.address == "100 S I-35"


def test_osm_way_uses_center_and_contact_website():
    payload = _load_osm_fixture()
    conn = database.get_conn()
    candidates = candidates_from_payload(payload, conn)
    conn.close()
    gm = next(c for c in candidates if c.name == "Georgetown Mazda")
    assert gm.website == "https://georgetownmazda.example"
    assert gm.lat == 30.505 and gm.lng == -97.675
    assert gm.source_id == "way/2002"


def test_osm_reverse_zip_fill_for_tagless_element():
    payload = _load_osm_fixture()
    conn = database.get_conn()
    candidates = candidates_from_payload(payload, conn)
    conn.close()
    nwm = next(c for c in candidates if c.name == "No Website Motors")
    assert nwm.website is None
    # this element has no addr:* tags at all -> filled from the seeded zip_coords row
    assert nwm.zip == "78664"
    assert nwm.city == "Round Rock"
    assert nwm.state == "TX"


def test_nearest_zip_returns_none_far_from_any_seeded_row():
    conn = database.get_conn()
    result = nearest_zip(conn, 61.2, -149.9)  # Anchorage — nowhere near the one seeded TX zip
    conn.close()
    assert result is None


# ── discovery/registry.py: TX (.xls row transform) ──
# Column order/names mirror the real TxDMV GDN sheet exactly (verified live
# 2026-07-18 — see docs/PROGRESS.md), so this exercises the real shape
# without needing a binary .xls fixture.
TX_HEADER = ["County", "LicenseNumber", "LicenseStatus", "LicenseExpDate", "BusinessName",
             "DBAName", "AddressType", "PhysicalAddress", "PhysAddressTwo", "City", "State",
             "Zip", "MailingAddress", "MailAddressTwo", "MailingCity", "MailingState",
             "MailingZip", "Phone", "BusinessEmail", "LicenseType", "ActiveDate",
             "DealerType", "BondCompany"]


def _tx_row(**overrides):
    base = {
        "County": "Williamson", "LicenseNumber": "P100001", "LicenseStatus": "Active",
        "LicenseExpDate": "10/31/2027", "BusinessName": "Test Motors LLC", "DBAName": "Test Motors",
        "AddressType": "Physical", "PhysicalAddress": "1 Main St", "PhysAddressTwo": "",
        "City": "Georgetown", "State": "Texas", "Zip": "78628-1234", "MailingAddress": "",
        "MailAddressTwo": "", "MailingCity": "", "MailingState": "", "MailingZip": "",
        "Phone": "(512) 555-0100", "BusinessEmail": "test@example.com",
        "LicenseType": "Motor Vehicle", "ActiveDate": "01/01/2020", "DealerType": "",
        "BondCompany": "Test Bonding Co",
    }
    base.update(overrides)
    return [base[h] for h in TX_HEADER]


def test_tx_rows_active_motor_vehicle_kept():
    rows = _tx_rows_from_sheet(TX_HEADER, [_tx_row()])
    assert len(rows) == 1
    r = rows[0]
    assert r["name"] == "Test Motors LLC"
    assert r["dba"] == "Test Motors"
    assert r["license_no"] == "P100001"
    assert r["zip"] == "78628-1234"


def test_tx_rows_expired_excluded():
    assert _tx_rows_from_sheet(TX_HEADER, [_tx_row(LicenseStatus="Expired")]) == []


def test_tx_rows_irrelevant_license_type_excluded():
    # trailers/motorcycles share the same TxDMV system but aren't car dealers
    assert _tx_rows_from_sheet(TX_HEADER, [_tx_row(LicenseType="Motorcycle")]) == []


def test_tx_rows_pending_renewal_kept():
    rows = _tx_rows_from_sheet(TX_HEADER, [_tx_row(LicenseStatus="Active - Pending Renewal")])
    assert len(rows) == 1


def test_tx_rows_address_line_two_concatenated():
    rows = _tx_rows_from_sheet(TX_HEADER, [_tx_row(PhysAddressTwo="Suite 200")])
    assert rows[0]["address"] == "1 Main St Suite 200"


# ── discovery/registry.py: FL (real HTML table structure) ──
def test_fl_table_parses_real_structure():
    with open(os.path.join(FIXTURES_DIR, "fl_dealers_sample.html")) as f:
        html = f.read()
    rows = _parse_fl_table(html)
    assert len(rows) == 3
    by_name = {r["name"]: r for r in rows}
    fh = by_name["F & H AUTO SALES INC."]
    assert fh["zip"] == "33063"
    assert fh["city"] == "MARGATE"
    assert fh["license_no"] == "1152312-1"
    assert fh["email"] == "fhautosales19@gmail.com"


# ── discovery/registry.py: states_in_radius + RegistryProvider ──
def test_states_in_radius_finds_seeded_state_excludes_far_point():
    conn = database.get_conn()
    conn.execute("INSERT OR REPLACE INTO zip_coords (zip, lat, lng, city, state) VALUES (?,?,?,?,?)",
                 ("70115", 29.9143, -90.0801, "New Orleans", "LA"))
    conn.commit()
    conn.close()
    assert "LA" in states_in_radius(29.9143, -90.0801, 10)
    assert "LA" not in states_in_radius(30.5083, -97.6789, 15)  # Round Rock TX, nowhere near LA


def test_registry_provider_find_uses_snapshot_and_radius_filter():
    conn = database.get_conn()
    conn.execute("INSERT OR REPLACE INTO zip_coords (zip, lat, lng, city, state) VALUES (?,?,?,?,?)",
                 ("78628", 30.6333, -97.6772, "Georgetown", "TX"))
    # a snapshot row inserted directly gets captured_at=now via the column
    # default, so find() sees a fresh snapshot and skips the network fetch.
    conn.execute(
        "INSERT INTO state_registry (state, license_no, name, dba, address, city, zip, "
        "phone, email, license_type) VALUES "
        "('TX','P999','Test Motors LLC','Test Motors','1 Main St','Georgetown','78628',"
        "'555-1212','','Motor Vehicle')"
    )
    conn.commit()
    conn.close()

    candidates = RegistryProvider().find(30.5083, -97.6789, 15)  # Round Rock, 15mi
    assert len(candidates) == 1
    c = candidates[0]
    assert c.name == "Test Motors"  # DBA preferred over the corporate/BusinessName
    assert c.source == "registry:TX"
    assert c.source_id == "P999"
    assert c.zip == "78628"


# ── discovery/websites.py: resolve_website chain (stubbed, no network) ──
def _raising_stub(*a, **k):
    raise AssertionError("this stub must not be called")


PLACES_ON = {"sources": {"places": True}, "google_places_key": "test-key",
             "web_search_fallback": False}
PLACES_OFF = {"sources": {"places": False}, "google_places_key": "test-key",
              "web_search_fallback": False}
DDG_ON = {"sources": {"places": False}, "google_places_key": "",
          "web_search_fallback": True}
DDG_OFF = {"sources": {"places": False}, "google_places_key": "",
           "web_search_fallback": False}


def test_resolve_website_prefers_existing_osm_tag_website():
    c = Candidate(name="Round Rock Toyota", website="https://roundrocktoyota.com",
                  source="osm", source_id="node/1")
    website, source = resolve_website(c, DDG_ON, places_search=_raising_stub, ddg_search=_raising_stub)
    assert website == "https://roundrocktoyota.com"
    assert source == "osm-tag"


def test_resolve_website_existing_website_non_osm_source_is_merge_fill():
    c = Candidate(name="Test Motors", website="https://testmotors.example",
                  source="places", source_id="place-1")
    website, source = resolve_website(c, DDG_ON, places_search=_raising_stub, ddg_search=_raising_stub)
    assert website == "https://testmotors.example"
    assert source == "merge-fill"


def test_resolve_website_garbage_existing_website_falls_through_to_places():
    # regression: a garbage/unparseable website tag value (real OSM data
    # does contain junk like "yes") used to be treated as "already
    # resolved" (since it's truthy and not on the blocklist) and returned
    # (None, "osm-tag") without ever trying Places/DDG.
    c = Candidate(name="Test Motors", city="Austin", state="TX", lat=30.5, lng=-97.6,
                  website="yes", source="osm", source_id="node/1")
    website, source = resolve_website(
        c, PLACES_ON, places_search=lambda *a, **k: "https://testmotors.example",
        places_budget=PlacesBudget(5),
    )
    assert website == "https://testmotors.example"
    assert source == "places"


def test_resolve_website_blocklisted_existing_falls_through_to_places():
    c = Candidate(name="Test Motors", city="Austin", state="TX", lat=30.5, lng=-97.6,
                  website="https://www.facebook.com/testmotors", source="osm", source_id="node/2")
    website, source = resolve_website(
        c, PLACES_ON,
        places_search=lambda *a, **k: "https://testmotors.example",
        places_budget=PlacesBudget(5),
    )
    assert website == "https://testmotors.example"
    assert source == "places"


def test_resolve_website_places_disabled_skips_places_stub():
    c = Candidate(name="Test Motors", city="Austin", state="TX", lat=30.5, lng=-97.6)
    website, source = resolve_website(c, PLACES_OFF, places_search=_raising_stub,
                                       ddg_search=_raising_stub, places_budget=PlacesBudget(5))
    assert (website, source) == (None, None)


def test_resolve_website_places_result_canonicalized():
    c = Candidate(name="Test Motors", city="Austin", state="TX", lat=30.5, lng=-97.6)
    website, source = resolve_website(
        c, PLACES_ON,
        places_search=lambda *a, **k: "http://www.Dealer-Example.com/inventory?x=1",
        places_budget=PlacesBudget(5),
    )
    assert website == "https://dealer-example.com"
    assert source == "places"


def test_resolve_website_ddg_rejected_low_name_domain_score():
    c = Candidate(name="Round Rock Toyota", city="Round Rock", state="TX")
    website, source = resolve_website(
        c, DDG_ON, ddg_search=lambda *a, **k: "https://totallyunrelatedsite.example",
    )
    assert (website, source) == (None, None)


def test_resolve_website_ddg_accepted_good_name_domain_score():
    c = Candidate(name="Round Rock Toyota", city="Round Rock", state="TX")
    website, source = resolve_website(
        c, DDG_ON, ddg_search=lambda *a, **k: "https://roundrocktoyota.com/some/path",
    )
    assert website == "https://roundrocktoyota.com"
    assert source == "web-search"


def test_resolve_website_web_search_disabled_skips_ddg_stub():
    c = Candidate(name="Round Rock Toyota", city="Round Rock", state="TX")
    website, source = resolve_website(c, DDG_OFF, ddg_search=_raising_stub)
    assert (website, source) == (None, None)


def test_resolve_website_gives_up_returns_none_none():
    c = Candidate(name="Nobody Motors", city="Nowhere", state="TX")
    website, source = resolve_website(c, DDG_OFF)
    assert (website, source) == (None, None)


def test_is_blocked_matches_domain_and_subdomain_not_unrelated():
    assert _is_blocked("https://www.facebook.com/somedealer")
    assert _is_blocked("https://m.facebook.com/somedealer")
    assert not _is_blocked("https://roundrocktoyota.com")
    assert not _is_blocked(None)


def test_should_attempt_recent_check_blocks_reattempt():
    from datetime import datetime, timedelta
    now = datetime.utcnow()
    recent = (now - timedelta(days=5)).isoformat()
    assert should_attempt(recent, refresh_days=30, now=now) is False


def test_should_attempt_old_check_allows_reattempt():
    from datetime import datetime, timedelta
    now = datetime.utcnow()
    old = (now - timedelta(days=40)).isoformat()
    assert should_attempt(old, refresh_days=30, now=now) is True


def test_should_attempt_no_prior_check_allows_attempt():
    assert should_attempt(None) is True


# ── discovery/places.py: search_website (stubbed HTTP, no network/key) ──
class _FakePlacesResp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def test_places_budget_spend_and_exhaustion():
    b = PlacesBudget(cap=2)
    assert b.spend() is True
    assert b.spend() is True
    assert b.spend() is False
    assert b.exhausted is True


def test_search_website_no_api_key_never_calls_post():
    result = search_website(None, "Test Motors", "Austin", "TX", 30.5, -97.6,
                             PlacesBudget(5), _post=_raising_stub)
    assert result is None


def test_search_website_exhausted_budget_never_calls_post():
    exhausted = PlacesBudget(0)
    result = search_website("key", "Test Motors", "Austin", "TX", 30.5, -97.6,
                             exhausted, _post=_raising_stub)
    assert result is None


def test_search_website_accepts_result_within_proximity():
    payload = {"places": [{"websiteUri": "https://testmotors.example",
                            "location": {"latitude": 30.5, "longitude": -97.6}}]}
    result = search_website("key", "Test Motors", "Austin", "TX", 30.5, -97.6,
                             PlacesBudget(5), _post=lambda *a, **k: _FakePlacesResp(payload))
    assert result == "https://testmotors.example"


def test_search_website_rejects_result_too_far():
    payload = {"places": [{"websiteUri": "https://testmotors.example",
                            "location": {"latitude": 31.5, "longitude": -98.5}}]}
    result = search_website("key", "Test Motors", "Austin", "TX", 30.5, -97.6,
                             PlacesBudget(5), _post=lambda *a, **k: _FakePlacesResp(payload))
    assert result is None


def test_search_website_missing_coords_does_not_spend_budget():
    # regression: budget used to be spent BEFORE the lat/lng check, wasting
    # a slot on a candidate that could never complete a real lookup anyway.
    b = PlacesBudget(5)
    result = search_website("key", "Test Motors", "Austin", "TX", None, None, b, _post=_raising_stub)
    assert result is None
    assert b.calls_made == 0


def test_resolve_website_nameless_candidate_gives_up_without_searching():
    # regression: a nameless candidate used to reach places_search/ddg_search
    # with name=None, producing a "None <city> <state>" query.
    c = Candidate(name=None, city="Austin", state="TX", lat=30.5, lng=-97.6, source="osm")
    website, source = resolve_website(c, PLACES_ON, places_search=_raising_stub,
                                       ddg_search=_raising_stub, places_budget=PlacesBudget(5))
    assert (website, source) == (None, None)


class _FakeDdgResp:
    def __init__(self, html):
        self.text = html

    def raise_for_status(self):
        pass


def test_ddg_search_skips_sponsored_ad_picks_organic_result():
    # Real markup shape + this exact failure mode confirmed live 2026-07-18
    # (see docs/PROGRESS.md): a real search for "Round Rock Toyota" put a
    # PAID AD for a different dealer (Toyota of Cedar Park) first on the
    # page, ahead of the organic result__a for the dealer actually searched
    # for. A naive "first result__a on the page" grab returns the ad's
    # tracking-redirect link, not the dealer's own site.
    html = '''
    <div class="result results_links results_links_deep result--ad ">
      <a class="result__a" href="https://duckduckgo.com/y.js?ad_domain=toyotaofcedarpark.com">Toyota of Cedar Park (Ad)</a>
    </div>
    <div class="result results_links results_links_deep web-result ">
      <a class="result__a" href="https://www.roundrocktoyota.com/">Round Rock Toyota</a>
    </div>
    '''
    result = _ddg_search("Round Rock Toyota", "Round Rock", "TX",
                          _post=lambda *a, **k: _FakeDdgResp(html), _sleep=lambda s: None)
    assert result == "https://www.roundrocktoyota.com/"


def test_ddg_search_all_ads_no_organic_returns_none():
    html = '''
    <div class="result results_links results_links_deep result--ad ">
      <a class="result__a" href="https://duckduckgo.com/y.js?ad_domain=somewhere.com">Ad</a>
    </div>
    '''
    result = _ddg_search("Nobody Motors", "Nowhere", "TX",
                          _post=lambda *a, **k: _FakeDdgResp(html), _sleep=lambda s: None)
    assert result is None


def test_ddg_search_no_results_returns_none():
    html = '<html><body>no results here</body></html>'
    result = _ddg_search("Nobody Motors", "Nowhere", "TX",
                          _post=lambda *a, **k: _FakeDdgResp(html), _sleep=lambda s: None)
    assert result is None


def test_search_website_empty_results_returns_none():
    result = search_website("key", "Test Motors", "Austin", "TX", 30.5, -97.6,
                             PlacesBudget(5), _post=lambda *a, **k: _FakePlacesResp({"places": []}))
    assert result is None


if __name__ == "__main__":
    fns = [(n, f) for n, f in sorted(globals().items())
           if n.startswith("test_") and callable(f)]
    passed = failed = 0
    for name, fn in fns:
        try:
            fn()
            print(f"PASS  {name}"); passed += 1
        except AssertionError as e:
            print(f"FAIL  {name}: {e}"); failed += 1
        except Exception as e:
            print(f"ERROR {name}: {type(e).__name__}: {e}"); failed += 1
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
