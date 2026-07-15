# 🚗 BumperScraper

**Every lot. Every listing. Your radius.**

A local, single-user used-car search engine. Scrapes dealer websites directly
into SQLite, deduplicates cross-listed cars, and serves a dark-mode dashboard
at `http://localhost:5000`. Flask + plain JS, no external services, no
accounts — everything runs and stays on your machine.

## Quick start

```bat
start.bat                                 REM installs deps, bootstraps, starts the server
```

Or manually on a cold machine:

```bat
python bootstrap.py                       REM schema + ZIP geocoder (~2MB download, once)
python tx_directory.py round-rock austin  REM dealer directory for your area (no args = all of TX, slow)
python app.py
```

Then in the dashboard's **Dealer Scraping** tab, run in order:
**Find dealers in radius** → **Classify** → **Scan inventory**. Set your ZIP
and radius in `config.json` (or the UI) first.

## How it works

Directory of TX dealers (`tx_directory`) + offline ZIP geocoder
(`zip_coords`) → discover dealers in radius → classify each site's platform
(Dealer.com, Dealer Inspire, generic embedded-JSON, sitemap) → scrape
inventory with `curl_cffi` Chrome impersonation → diff against the DB (new /
sold) → VIN + fuzzy dedup → dashboard. Per-dealer data-quality scoring flags
thin or broken scrapes automatically.

## Development

```bat
python tests/test_extractors.py    REM offline golden-fixture extractor tests
python metrics.py --json           REM KPI report (coverage, completeness, quality)
```

Requires Python 3.13+ (developed on 3.14) on Windows; dependencies are
pure-Python / abi3 wheels only (`requirements.txt`).
