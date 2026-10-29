import json, os, re
from urllib.parse import quote_plus
from curl_cffi import requests
from bs4 import BeautifulSoup
s = requests.Session(impersonate="chrome")
out = "probe/out"; os.makedirs(out, exist_ok=True)
res = {}

# ---- Marktplaats: search, then read the full ad text of candidates
mp = {}
for q in ["evolving skies pokemon center", "evolving skies pokemon center elite trainer box", "evolving skies pc etb",
          "evolving skies elite trainer box", "evolving skies etb", "pokemon center etb jolteon", "pokemon center etb vaporeon"]:
    try:
        r = s.get("https://www.marktplaats.nl/lrp/api/search?query=" + quote_plus(q) +
                  "&l1CategoryId=1099&limit=100&offset=0&sortBy=PRICE&sortOrder=INCREASING", timeout=30)
        for l in r.json().get("listings", []):
            mp[l["itemId"]] = {k: l.get(k) for k in ("itemId", "title", "description", "priceInfo", "date", "vipUrl",
                                                      "attributes", "location", "sellerInformation", "categoryId")}
        print("mp", q, r.status_code, len(mp))
    except Exception as e:
        print("mp ERR", q, e)
cand = [l for l in mp.values() if re.search(r"evolving", l["title"], re.I)]
for l in cand[:60]:
    try:
        r = s.get("https://www.marktplaats.nl" + l["vipUrl"], timeout=30)
        soup = BeautifulSoup(r.text, "html.parser")
        d = soup.select_one('[class*="Description-description"], [data-collapsable="description"], #vip-ad-description')
        l["full_description"] = d.get_text("\n", strip=True)[:3000] if d else None
        if not l["full_description"]:
            m = re.search(r'"description"\s*:\s*"((?:[^"\\]|\\.){20,4000})"', r.text)
            l["full_description"] = json.loads('"' + m.group(1) + '"') if m else None
        l["vip_status"] = r.status_code
    except Exception as e:
        l["vip_status"] = f"ERR {e}"
res["marktplaats"] = list(mp.values())

# ---- Courtyard
try:
    page = s.get("https://courtyard.io/marketplace", timeout=30).text
    key = re.search(r'algoliaApiKey\\*"\s*:\s*\\*"([A-Za-z0-9]{20,64})', page).group(1)
    hits = []
    for q in ["evolving skies elite trainer box", "evolving skies pokemon center", "evolving skies"]:
        r = s.post("https://api.courtyard.io/1/indexes/*/queries", json={"requests": [{"indexName": "marketplace_prod_recently_listed",
                   "params": f"query={quote_plus(q)}&hitsPerPage=100&attributesToHighlight="}]},
                   headers={"x-algolia-api-key": key, "x-algolia-application-id": "courtyard", "origin": "https://courtyard.io"}, timeout=30)
        for h in r.json()["results"][0]["hits"]:
            hits.append({k: h.get(k) for k in ("objectID", "title", "price", "estimatedValueUsd", "collection", "metadata")})
    res["courtyard"] = hits; print("courtyard", len(hits))
except Exception as e:
    print("courtyard ERR", e)

# ---- eBay (cheapest first incl. shipping)
for dom in ["www.ebay.nl", "www.ebay.de", "www.ebay.com"]:
    try:
        r = s.get(f"https://{dom}/sch/i.html?_nkw=" + quote_plus("evolving skies pokemon center elite trainer box sealed") + "&_sop=15&LH_BIN=1&_ipg=120", timeout=30)
        soup = BeautifulSoup(r.text, "html.parser"); items = []
        for it in soup.select("li.s-item, li.s-card"):
            t = it.select_one(".s-item__title, .s-card__title"); p = it.select_one(".s-item__price, .s-card__price")
            a = it.select_one("a[href*='/itm/']"); sh = it.select_one(".s-item__shipping, .s-item__logisticsCost, [class*=shipping]")
            loc = it.select_one(".s-item__location, [class*=location]")
            if t and p and a:
                items.append({"title": t.get_text(" ", strip=True), "price": p.get_text(" ", strip=True),
                              "ship": sh.get_text(" ", strip=True) if sh else "", "loc": loc.get_text(" ", strip=True) if loc else "",
                              "url": a["href"].split("?")[0]})
        res["ebay_" + dom] = items; print("ebay", dom, r.status_code, len(items))
    except Exception as e:
        print("ebay ERR", dom, e)

# ---- Cardmarket
try:
    r = s.get("https://www.cardmarket.com/en/Pokemon/Products/Search?searchString=" + quote_plus("evolving skies pokemon center"), timeout=30)
    soup = BeautifulSoup(r.text, "html.parser")
    rows = [{"text": x.get_text(" | ", strip=True)[:300], "href": (x.select_one("a") or {}).get("href")} for x in soup.select("div.table-body > div.row, div[id^=productRow]")]
    res["cardmarket"] = {"status": r.status_code, "title": soup.title.get_text(strip=True) if soup.title else "", "rows": rows[:40]}
    print("cardmarket", r.status_code, len(rows))
except Exception as e:
    print("cardmarket ERR", e)

json.dump(res, open(f"{out}/search.json", "w"), ensure_ascii=False, indent=1)
