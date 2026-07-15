"""
Offline tests for the deduplication engine (duplicates.py).

Cover the fuzzy-match rules (trim gate, both-dimension proximity, distinct-VIN
block), the VIN-guarded union-find (a VIN-less listing must never chain two
distinct physical cars into one group), and canonical selection (a leaked
payment-sized price must never become the visible listing).

Runs against a throwaway SQLite DB in a temp dir — the real DB is untouched.

Run:  python tests/test_duplicates.py       (standalone)
  or: pytest tests/                         (if pytest is installed)
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database

# Point the whole database layer at a temp file BEFORE anything opens a conn.
_tmpdir = tempfile.mkdtemp(prefix="bumperscraper-test-")
database.DB_PATH = os.path.join(_tmpdir, "test.db")
database.init_db()

from duplicates import run_deduplication, _is_fuzzy_match, _make_key

VIN1 = "1HGCM82633A004352"
VIN2 = "1HGCM82633A004353"


def _base(**kw):
    d = {"vin": None, "year": 2019, "make": "Honda", "model": "Civic",
         "trim": None, "mileage": 42000, "price": 18995}
    d.update(kw)
    return d


# ── _is_fuzzy_match unit cases ──
def test_fuzzy_match_rules():
    # identical VIN-less cars, both dimensions close → match
    assert _is_fuzzy_match(_base(), _base(mileage=42400, price=18800))
    # same price but 20k miles apart → DIFFERENT cars (old OR-rule merged these)
    assert not _is_fuzzy_match(_base(mileage=10000), _base(mileage=30000))
    # same mileage but $6k apart → different cars
    assert not _is_fuzzy_match(_base(price=12995), _base(price=18995))
    # only mileage comparable (one price missing) → mileage decides
    assert _is_fuzzy_match(_base(price=None), _base(mileage=42300))
    assert not _is_fuzzy_match(_base(price=None), _base(mileage=99000))
    # only price comparable → price decides
    assert _is_fuzzy_match(_base(mileage=None), _base(price=18700))
    # neither dimension comparable → never guess
    assert not _is_fuzzy_match(_base(mileage=None, price=None),
                               _base(mileage=None, price=None))
    # trim gate: clearly different trims block the match…
    assert not _is_fuzzy_match(_base(trim="LX"), _base(trim="Touring"))
    # …but a short trim inside a verbose one passes, and missing trim never blocks
    assert _is_fuzzy_match(_base(trim="SE"), _base(trim="SE Sport Utility"))
    assert _is_fuzzy_match(_base(trim="LX"), _base(trim=None))
    # distinct VINs are distinct cars, full stop
    assert not _is_fuzzy_match(_base(vin=VIN1), _base(vin=VIN2))
    # different year → no match
    assert not _is_fuzzy_match(_base(), _base(year=2020))


def test_make_key_bucketing():
    assert _make_key("Chevy") == _make_key("CHEVROLET")
    assert _make_key("VW") == _make_key("Volkswagen")
    assert _make_key("Mercedes") == _make_key("Mercedes-Benz")
    assert _make_key("Honda") != _make_key("Hyundai")


# ── end-to-end against a temp DB ──
def _insert(rows):
    conn = database.get_conn()
    conn.execute("DELETE FROM listings")
    conn.execute("DELETE FROM duplicate_groups")
    for i, r in enumerate(rows):
        conn.execute("""
            INSERT INTO listings (source, url, vin, year, make, model, trim,
                                  mileage, price, distance_mi, is_active)
            VALUES (?,?,?,?,?,?,?,?,?,?,1)
        """, (r.get("source", "dealer"), r.get("url", f"https://x.test/car{i}"),
              r.get("vin"), r.get("year"), r.get("make"), r.get("model"),
              r.get("trim"), r.get("mileage"), r.get("price"),
              r.get("distance_mi")))
    conn.commit()
    conn.close()


def _groups():
    """{frozenset(urls): match_method} for every duplicate group."""
    conn = database.get_conn()
    out = {}
    for gid, method in conn.execute(
            "SELECT id, match_method FROM duplicate_groups").fetchall():
        urls = {u for (u,) in conn.execute(
            "SELECT url FROM listings WHERE duplicate_group_id=?", (gid,))}
        if urls:
            out[frozenset(urls)] = method
    conn.close()
    return out


def test_vin_pass_unchanged():
    _insert([
        _base(vin=VIN1, url="https://a.test/1"),
        _base(vin=VIN1, url="https://b.test/1", price=19500),
        _base(vin=VIN2, url="https://c.test/1"),
    ])
    run_deduplication()
    groups = _groups()
    assert groups == {frozenset({"https://a.test/1", "https://b.test/1"}): "vin"}, groups


def test_transitive_vin_guard():
    # A carries VIN1, C carries VIN2, B is VIN-less and matches both.
    # The old loop could chain all three into one group; the union-find must
    # keep A+B together and refuse to absorb C.
    _insert([
        _base(vin=VIN1, url="https://a.test/1"),
        _base(vin=None, url="https://b.test/1", mileage=42100, price=18950),
        _base(vin=VIN2, url="https://c.test/1", mileage=42200, price=18900),
    ])
    run_deduplication()
    groups = _groups()
    # the two real cars must never share a group…
    assert not any({"https://a.test/1", "https://c.test/1"} <= g for g in groups), groups
    # …and B lands in exactly one group, with one of them
    assert sum("https://b.test/1" in g for g in groups) == 1, groups


def test_no_merge_same_price_different_mileage():
    # dealer lot with two identically-priced base trims — real inventory,
    # must both stay visible
    _insert([
        _base(url="https://a.test/1", mileage=10000, price=21999),
        _base(url="https://b.test/1", mileage=30000, price=21999),
    ])
    run_deduplication()
    assert _groups() == {}, _groups()
    conn = database.get_conn()
    visible = conn.execute(
        "SELECT COUNT(*) FROM listings WHERE is_duplicate=0").fetchone()[0]
    conn.close()
    assert visible == 2


def test_payment_leak_never_canonical():
    # same VIN listed twice; one row's "price" is a leaked $650 payment
    _insert([
        _base(vin=VIN1, url="https://a.test/1", price=650),
        _base(vin=VIN1, url="https://b.test/1", price=15000),
    ])
    run_deduplication()
    conn = database.get_conn()
    canonical = conn.execute(
        "SELECT url FROM listings WHERE is_duplicate=0").fetchall()
    conn.close()
    assert [u for (u,) in canonical] == ["https://b.test/1"], canonical


def test_fuzzy_group_canonical_cheapest_then_closest():
    _insert([
        _base(url="https://a.test/1", price=18995, distance_mi=20.0),
        _base(url="https://b.test/1", price=18995, distance_mi=5.0, mileage=42100),
    ])
    run_deduplication()
    conn = database.get_conn()
    canonical = [u for (u,) in conn.execute(
        "SELECT url FROM listings WHERE is_duplicate=0 AND duplicate_group_id IS NOT NULL")]
    conn.close()
    assert canonical == ["https://b.test/1"], canonical


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
