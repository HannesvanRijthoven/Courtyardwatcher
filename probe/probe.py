import json, os, time
from curl_cffi import requests
s = requests.Session(impersonate="chrome")
out = "probe/out"; os.makedirs(out, exist_ok=True)
def grab(name, url, **kw):
    try:
        r = s.get(url, timeout=30, **kw)
        open(f"{out}/{name}", "w").write(r.text)
        print(name, r.status_code, len(r.text), r.url)
    except Exception as e:
        print(name, "ERR", e)
grab("mp_search.json", "https://www.marktplaats.nl/lrp/api/search?query=elite%20trainer%20box&limit=100&offset=0&sortBy=SORT_INDEX&sortOrder=DECREASING")
grab("mp_search_cat.json", "https://www.marktplaats.nl/lrp/api/search?query=etb&limit=30&offset=0&sortBy=SORT_INDEX&sortOrder=DECREASING")
for p in range(1, 4):
    grab(f"pc_search_{p}.html", f"https://www.pricecharting.com/search-products?type=prices&q=elite+trainer+box&page={p}")
grab("pc_search_pc.html", "https://www.pricecharting.com/search-products?type=prices&q=pokemon+center+elite+trainer+box")
grab("pc_console.html", "https://www.pricecharting.com/console/pokemon-evolving-skies")
grab("pc_product.html", "https://www.pricecharting.com/game/pokemon-evolving-skies/elite-trainer-box")
grab("ecb.xml", "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml")
grab("vinted_home.html", "https://www.vinted.nl/")
grab("vinted_api.json", "https://www.vinted.nl/api/v2/catalog/items?search_text=elite%20trainer%20box&order=newest_first&per_page=20")
