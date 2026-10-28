import os
from urllib.parse import quote_plus
from curl_cffi import requests
s = requests.Session(impersonate="chrome")
out = "probe/out"; os.makedirs(out, exist_ok=True)
def grab(name, url):
    try:
        r = s.get(url, timeout=30); open(f"{out}/{name}", "w").write(r.text); print(name, r.status_code, len(r.text))
    except Exception as e: print(name, "ERR", e)
qs = ["elite trainer box", "elite trainer box pokemon center", "elite trainer box scarlet violet", "elite trainer box sword shield",
      "elite trainer box sun moon", "elite trainer box xy", "elite trainer box black white", "elite trainer box mega evolution",
      "elite trainer box evolving skies", "elite trainer box japanese"]
for i, q in enumerate(qs):
    grab(f"pc_q{i}.html", "https://www.pricecharting.com/search-products?type=prices&q=" + quote_plus(q))
for off in (0, 100, 200):
    grab(f"mp_etb_{off}.json", f"https://www.marktplaats.nl/lrp/api/search?query=elite%20trainer%20box&limit=100&offset={off}&sortBy=SORT_INDEX&sortOrder=DECREASING")
grab("mp_etb2.json", "https://www.marktplaats.nl/lrp/api/search?query=etb&limit=100&offset=0&sortBy=SORT_INDEX&sortOrder=DECREASING")
grab("mp_etb_l2.json", "https://www.marktplaats.nl/lrp/api/search?query=elite%20trainer%20box&l1CategoryId=1099&l2CategoryId=930&limit=100&offset=0&sortBy=SORT_INDEX&sortOrder=DECREASING")
