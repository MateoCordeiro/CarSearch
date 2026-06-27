"""Quick diagnostic: fetch one cardealerdb city page and show what links are in the raw HTML."""
import requests
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

url = "https://cardealerdb.com/in/TX/round-rock"
print(f"Fetching {url}...")
r = requests.get(url, headers=HEADERS, timeout=15)
print(f"Status: {r.status_code}  |  Content length: {len(r.text)} chars")

soup = BeautifulSoup(r.text, "html.parser")

all_links = soup.select("a[href]")
print(f"\nTotal <a> tags: {len(all_links)}")

go_links = soup.select("a[href*='/go/']")
print(f"Links containing '/go/': {len(go_links)}")

print("\nFirst 10 href values:")
for a in all_links[:10]:
    print(f"  {a.get('href','')[:80]}")

print("\nAll unique href patterns (first 60 chars):")
seen = set()
for a in all_links:
    h = a.get("href","")[:60]
    if h not in seen:
        seen.add(h)
        print(f"  {h}")
