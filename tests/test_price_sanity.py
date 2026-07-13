"""
Offline unit tests for payment/lease price rejection and MSRP handling.

A monthly payment ("$299/mo"), money-down ("$1,500 down"), or lease figure
("lease for $4,999") must never be stored as a car's asking price — and a
plain "$2,500" must never be rejected. These lock in that behaviour.

Run:  python tests/test_price_sanity.py      (standalone)
  or: pytest tests/                          (if pytest is installed)
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scrapers.base import BaseScraper
from scrapers.dealers import DealerScraper

S = DealerScraper()
_DOLLAR_RE = re.compile(r'\$\s*(\d{1,3}(?:,\d{3})+)')


def _payment_ctx(text):
    """Run _is_payment_context on the first $X,XXX amount in text."""
    m = _DOLLAR_RE.search(text)
    assert m, f"no dollar amount in {text!r}"
    return BaseScraper._is_payment_context(text, m.start(), m.end())


# ── _is_payment_context: reject matrix ──
def test_payment_context_rejects():
    rejected = [
        "$2,199/mo with approved credit",
        "$1,299 /month for 48 months",
        "$2,500 per month",
        "$1,099 a month",
        "$1,500 down and drive today",
        "$2,500 due at signing",
        "$3,999 due at delivery",
        "lease for $4,999",
        "Lease special: $2,999",
        "payments from $2,199",
        "payment of $1,099",
        "est. payment $8,995",
        "estimated payment: $1,415",
        "finance for $9,999",
        "as low as $1,999",
        "down payment of $3,500",
        "$2,000 deposit required",
        "drive home for $1,995",
    ]
    for text in rejected:
        assert _payment_ctx(text), f"should reject: {text!r}"


# ── _is_payment_context: accept matrix (no false positives) ──
def test_payment_context_accepts():
    accepted = [
        "$2,500",
        "Sale Price $8,995",
        "Price: $6,500",
        "2015 Honda Civic — $9,200 — 88,000 miles",
        # 'Lease' far away on the page must not poison the price (anchoring)
        "Leasing available on select models. Great cars. Call now! Price $7,995 today",
        "Downtown Motors is proud to offer this car for $6,500",
        "$8,995 Sale Price was $10,995",
    ]
    for text in accepted:
        assert not _payment_ctx(text), f"should accept: {text!r}"


# ── _fields_from_segment: picks the real price past an adjacent payment ──
def test_fields_from_segment_skips_payment():
    seg = ('<a href="/inventory/used-2019-honda-accord">2019 Honda Accord</a>'
           '<span>$2,199/mo</span> ... <span>Sale Price $18,995</span>'
           '<div>Mileage 42,113</div>')
    f = S._fields_from_segment(seg)
    assert f.get("price") == 18995, f"price={f.get('price')}"
    assert f.get("skipped_payment_prices") == [2199], f
    assert f.get("mileage") == 42113


def test_fields_from_segment_only_payment_yields_no_price():
    seg = "<span>Estimated payment: $1,415/mo for 60 months</span>"
    f = S._fields_from_segment(seg)
    assert "price" not in f, f
    assert f.get("skipped_payment_prices") == [1415], f


def test_fields_from_segment_plain_price_untouched():
    f = S._fields_from_segment("<span>$6,500</span> 92,414 miles")
    assert f.get("price") == 6500
    assert "skipped_payment_prices" not in f


# ── _pick_price: payment-named sibling key rejection ──
def test_pick_price_rejects_payment_sibling():
    # generic `price` actually holds the monthly figure → rejected
    o = {"price": 599, "monthly_payment": 599, "vin": "X"}
    assert S._pick_price(o, S._PRICE_KEYS) is None
    # real price differs from the payment figure → kept
    o = {"price": 24999, "monthly_payment": 399}
    assert S._pick_price(o, S._PRICE_KEYS) == 24999
    # first whitelisted key poisoned, later clean key wins
    o = {"internetPrice": 599, "leasePrice": 599, "sellingPrice": 21500}
    assert S._pick_price(o, S._PRICE_KEYS) == 21500
    # bounds check (previously missing in _extract_json_inventory's path)
    assert S._pick_price({"price": 5}, S._PRICE_KEYS) is None
    assert S._pick_price({"price": 99_999_999}, S._PRICE_KEYS) is None


# ── JSON-LD: lease offers never provide the price ──
def test_jsonld_lease_offer_rejected():
    lease_type = {"name": "2024 Honda CR-V EX", "vin": "19XFC2F59KE000001",
                  "offers": {"@type": "LeaseOffer", "price": 389}}
    f = S._jsonld_to_fields(lease_type)
    assert f.get("price") is None, f

    monthly_spec = {"name": "2024 Honda CR-V EX",
                    "offers": {"@type": "Offer", "price": 429,
                               "priceSpecification": {"unitText": "MONTH"}}}
    f = S._jsonld_to_fields(monthly_spec)
    assert f.get("price") is None, f

    # list of offers: skip the lease one, take the real sale offer
    both = {"name": "2024 Honda CR-V EX",
            "offers": [{"@type": "LeaseOffer", "price": 389},
                       {"@type": "Offer", "price": 31995,
                        "availability": "InStock"}]}
    f = S._jsonld_to_fields(both)
    assert f.get("price") == 31995, f

    # plain sale offer still parses
    plain = {"name": "2018 Toyota Camry SE",
             "offers": {"@type": "Offer", "price": 17495}}
    assert S._jsonld_to_fields(plain).get("price") == 17495


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
