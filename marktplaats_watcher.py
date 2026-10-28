"""
Marktplaats ETB deal watcher.

Checks new Marktplaats ads for English Pokémon Elite Trainer Boxes, works out
which set the ad is for, compares the asking price with the sealed price on
PriceCharting (converted from USD to EUR) and sends a Telegram alert when the
discount is big enough.

The PriceCharting ETB catalogue is rebuilt once a day. Settings are in the
"marktplaats" section of config.json; state lives in mp_state.json.

Usage:
  python marktplaats_watcher.py              normal run
  python marktplaats_watcher.py --dry-run    print alerts instead of sending
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import time
import unicodedata
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus, urljoin

from bs4 import BeautifulSoup

from watcher import new_session, parse_money, send_telegram

ROOT = Path(__file__).parent
CONFIG_PATH = ROOT / "config.json"
STATE_PATH = ROOT / "mp_state.json"

MP_SEARCH = (
    "https://www.marktplaats.nl/lrp/api/search?query={q}"
    "&l1CategoryId=1099&l2CategoryId=930"  # Hobby en Vrije tijd > Pokémon
    "&limit=100&offset=0&sortBy=SORT_INDEX&sortOrder=DECREASING"
)
MP_BASE = "https://www.marktplaats.nl"
PC_SEARCH = "https://www.pricecharting.com/search-products?type=prices&q={}"
ECB_RATES = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"

# PriceCharting search stops at 100 results, so search in slices and merge.
PC_QUERIES = [
    "elite trainer box",
    "pokemon elite trainer box",
    "elite trainer box pokemon center",
    "elite trainer box plus",
    "elite trainer box scarlet violet",
    "elite trainer box sword shield",
    "elite trainer box sun moon",
    "elite trainer box xy",
    "elite trainer box mega evolution",
    "elite trainer box evolving skies",
]
CATALOGUE_HOURS = 24
SEEN_KEEP_DAYS = 30

DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "search_terms": ["elite trainer box", "etb"],
    "min_discount_pct": 30,
    "min_price_eur": 20,
    "max_price_eur": None,
    "include_bids": True,
    "usd_to_eur_fallback": 0.92,
}

# Series names that are also a set (base set). An ad title often starts with
# the series ("Scarlet & Violet Prismatic Evolutions"), so these only count
# when no other set name is in the title.
SERIES = {"scarlet violet", "sword shield", "sun moon", "xy", "mega evolution", "black white"}

# Extra ways people write a set name (normalised: lowercase, no accents, & removed).
ALIASES = {
    "scarlet violet 151": ["151", "sv151", "sv 151"],
    "30th celebration": ["30th", "30 th", "30th anniversary", "30 jaar", "30 jarig", "30 years", "30e verjaardag"],
    "prismatic evolutions": ["prismatic", "sv8 5", "sv 8 5"],
    "evolving skies": ["evolving sky"],
    "crown zenith": ["crown zenit"],
    "celebrations": ["25th anniversary", "25th celebration", "25 jaar"],
    "go": ["pokemon go"],
    "xy": ["xy base", "x y"],
    "scarlet violet": ["scarlet and violet base", "sv base"],
    "sword shield": ["sword and shield base", "swsh base"],
    "mega evolution": ["mega evolutions"],
}
# Set names that are too generic to match on their own (only via aliases).
ALIAS_ONLY = {"go"}
# Plural set names also match singular ("Destined Rival ETB"), except these.
NO_SINGULAR = {"evolutions", "celebrations"}

# Ads to ignore (normalised text, whole words).
JUNK = [
    r"cases?", r"display", r"acryl\w*", r"acrylic", r"protector\w*", r"hoes", r"sleeves?",
    r"lege", r"leeg", r"empty", r"geopend", r"opened", r"open", r"unsealed",
    r"zonder (?:hits|kaarten|boosters?|packs?|inhoud|promo)",
    r"gezocht", r"zoek\w*", r"wtb", r"ruil\w*", r"inkoop", r"buying",
    r"pre ?order\w*", r"preorder\w*", r"voorverkoop", r"reservering",
    r"bundle", r"booster bundle", r"mini tins?", r"tins?", r"blister\w*", r"lot", r"collectie",
    r"booster ?box\w*", r"bulk", r"kaart", r"kaarten", r"promos?", r"etbs", r"etb s",
    r"psa", r"cgc", r"bgs", r"graded", r"inhoud", r"replica", r"fake", r"proxy",
    # not English
    r"nl", r"nederlands\w*", r"dutch", r"duits\w*", r"german\w*", r"deutsch\w*", r"frans\w*",
    r"french", r"francais", r"spaans\w*", r"spanish", r"italiaans\w*", r"italian\w*",
    r"japans\w*", r"japanese", r"jp", r"jpn", r"chinees", r"chinese", r"korea\w*", r"thai",
]
JUNK_RE = re.compile(r"\b(?:" + "|".join(JUNK) + r")\b")
MULTI_RE = re.compile(
    r"\b(\d+)\s*(?:stuks|st|pcs|x)\b(?!\s*(?:beschikbaar|op voorraad))|\bx\s*\d+\b|\bset van\b"
    r"|\b([2-9]|\d\d)\s+(?:etb|elite)"
)
# "ETB + booster box", "ETB & tins" (but not "Scarlet & Violet")
COMBO_RE = re.compile(r"[&+](?!\s*(?:violet|shield|moon|white|y\b))", re.I)
PC_RE = re.compile(r"\b(?:pokemon ?center|pc|pokemoncenter)\b")


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    s = s.replace("&", " ").replace("'", "")
    return " ".join(re.sub(r"[^a-z0-9]+", " ", s).split())


# --------------------------------------------------------------------------- #
# Config / state
# --------------------------------------------------------------------------- #

def load_config() -> dict[str, Any]:
    cfg = dict(DEFAULTS)
    if CONFIG_PATH.exists():
        user = json.loads(CONFIG_PATH.read_text(encoding="utf-8")).get("marktplaats") or {}
        cfg.update({k: v for k, v in user.items() if not k.startswith("_")})
    return cfg


def load_state() -> dict[str, Any]:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state: dict[str, Any]) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=1), encoding="utf-8")


# --------------------------------------------------------------------------- #
# PriceCharting ETB catalogue
# --------------------------------------------------------------------------- #

@dataclass
class Product:
    set_name: str      # "Scarlet & Violet 151"
    title: str         # "Elite Trainer Box [Pokemon Center]"
    price_usd: float
    url: str

    @property
    def set_key(self) -> str:
        return norm(self.set_name)

    @property
    def variant(self) -> str:
        """Variant words without the generic bits, e.g. 'koraidon', 'iron leaves'."""
        v = norm(self.title).replace("elite trainer box", " ")
        v = re.sub(r"\b(?:pokemon center|plus|first print|mega)\b", " ", v)
        return " ".join(v.split())

    @property
    def is_pc(self) -> bool:
        return "pokemon center" in norm(self.title)

    @property
    def is_plus(self) -> bool:
        return " plus" in norm(self.title)


def parse_pc_rows(page: str) -> list[Product]:
    soup = BeautifulSoup(page, "html.parser")
    out = []
    for tr in soup.select("table#games_table tbody tr"):
        a, c, p = tr.select_one("td.title a"), tr.select_one("td.console"), tr.select_one("td.price")
        if not (a and c and p):
            continue
        title, console = " ".join(a.get_text().split()), " ".join(c.get_text().split())
        price = parse_money(p.get_text())
        if not price or not title.lower().startswith("elite trainer box"):
            continue
        if re.search(r"&|bundle|costco|tin\b|case\b", title, re.I):
            continue
        if not console.lower().startswith("pokemon ") or re.search(
            r"japanese|chinese|korean|thai|indonesian|german|french|italian|spanish", console, re.I
        ):
            continue
        out.append(Product(console[len("Pokemon "):], title, price, urljoin("https://www.pricecharting.com", a["href"])))
    return out


def build_catalogue(session) -> list[dict]:
    seen: dict[str, Product] = {}
    for q in PC_QUERIES:
        try:
            r = session.get(PC_SEARCH.format(quote_plus(q)), timeout=30)
            r.raise_for_status()
            for p in parse_pc_rows(r.text):
                seen[p.url] = p
        except Exception as e:
            print(f"PriceCharting search {q!r} failed: {e}", file=sys.stderr)
    return [p.__dict__ for p in seen.values()]


def get_catalogue(session, state: dict) -> list[Product]:
    cat = state.get("catalogue") or {}
    if not cat.get("items") or time.time() - cat.get("ts", 0) > CATALOGUE_HOURS * 3600:
        items = build_catalogue(session)
        if len(items) >= 50:  # don't replace a good catalogue with a broken one
            cat = {"ts": time.time(), "items": items}
            state["catalogue"] = cat
            print(f"Rebuilt PriceCharting catalogue: {len(items)} ETBs")
        elif not cat.get("items"):
            raise RuntimeError(f"PriceCharting catalogue looks broken ({len(items)} ETBs found)")
    return [Product(**p) for p in cat["items"]]


def get_usd_to_eur(session, state: dict, fallback: float) -> float:
    fx = state.get("fx") or {}
    if time.time() - fx.get("ts", 0) > 12 * 3600:
        try:
            r = session.get(ECB_RATES, timeout=20)
            r.raise_for_status()
            for el in ET.fromstring(r.content).iter():
                if el.get("currency") == "USD":
                    fx = {"ts": time.time(), "usd_to_eur": 1 / float(el.get("rate"))}
                    state["fx"] = fx
        except Exception as e:
            print(f"Exchange rate lookup failed, using last known: {e}", file=sys.stderr)
    return fx.get("usd_to_eur") or fallback


# --------------------------------------------------------------------------- #
# Matching an ad to a product
# --------------------------------------------------------------------------- #

def set_phrases(set_key: str) -> list[str]:
    phrases = [] if set_key in ALIAS_ONLY else [set_key]
    if set_key.endswith("s") and set_key not in NO_SINGULAR and set_key not in ALIAS_ONLY:
        phrases.append(set_key[:-1])
    return phrases + ALIASES.get(set_key, [])


def find_set(text: str, set_keys: set[str]) -> tuple[str | None, str]:
    """Returns (set_key, reason). None when no set or more than one set is named."""
    hits = []  # (start, end, set_key)
    for key in set_keys:
        for ph in set_phrases(key):
            for m in re.finditer(rf"\b{re.escape(ph)}\b", text):
                hits.append((m.start(), m.end(), key))
    # a match inside a longer one doesn't count ("evolutions" in "prismatic evolutions")
    hits = [h for h in hits if not any(o[0] <= h[0] and h[1] <= o[1] and (o[1] - o[0]) > (h[1] - h[0]) for o in hits)]
    keys = {h[2] for h in hits}
    if keys - SERIES:
        keys -= SERIES
    if "30th celebration" in keys:  # "Celebrations ETB 30th" is the 30th anniversary set
        keys.discard("celebrations")
    if not keys:
        return None, "no set recognised"
    if len(keys) > 1:
        return None, "several sets named: " + ", ".join(sorted(keys))
    return keys.pop(), ""


def match_product(title: str, catalogue: list[Product]) -> tuple[Product | None, str]:
    t = norm(title)
    if not re.search(r"\belite trainer\b|\betb\b|\betbs\b", t):
        return None, "not an ETB"
    set_key, why = find_set(t, {p.set_key for p in catalogue})
    if not set_key:
        return None, why
    cands = [p for p in catalogue if p.set_key == set_key]
    wants_pc = bool(PC_RE.search(t))
    cands = [p for p in cands if p.is_pc == wants_pc] or ([] if wants_pc else cands)
    if not cands:
        return None, "no Pokémon Center version on PriceCharting"
    cands = [p for p in cands if p.is_plus == bool(re.search(r"\bplus\b", t))] or cands
    words = set(t.split())
    named = [p for p in cands if p.variant and set(p.variant.split()) <= words]
    if named:
        cands = named
    elif set_key in SERIES:
        # "Mega Evolution Delta Reign ETB": a series name plus a set PriceCharting
        # doesn't know yet. Only trust the base set when the box is named.
        return None, "only a series name; set unclear"
    # several variants and the ad doesn't say which: compare with the cheapest,
    # so you only get alerts that are a deal whichever version it is
    best = min(cands, key=lambda p: p.price_usd)
    return best, ("" if len(cands) == 1 else "variant not stated, compared with cheapest")


# "zonder acryl case" / "incl. acrylic" is still just an ETB
ACRYL_EXTRA_RE = re.compile(r"\b(?:zonder|without|geen|incl\w*|met|with|plus)\s+(?:een\s+)?acryl\w*(?:\s+(?:case|hoes|protector))?")


def junk_reason(title: str) -> str | None:
    t = ACRYL_EXTRA_RE.sub(" ", norm(title))
    m = JUNK_RE.search(t)
    if m:
        return f"skip word '{m.group(0)}'"
    m = MULTI_RE.search(t)
    if m and (not m.group(1) or int(m.group(1)) > 1):
        return "multiple items"
    if COMBO_RE.search(title):
        return "combined with other items"
    return None


# --------------------------------------------------------------------------- #
# Marktplaats
# --------------------------------------------------------------------------- #

@dataclass
class Ad:
    item_id: str
    title: str
    price_eur: float | None
    price_type: str
    url: str
    image: str | None
    condition: str
    city: str
    seller: str

    @property
    def key(self) -> str:  # a price change counts as a new ad
        return f"{self.item_id}:{self.price_eur}"


def parse_ad(l: dict) -> Ad:
    pi = l.get("priceInfo") or {}
    cents = pi.get("priceCents") or 0
    imgs = l.get("imageUrls") or []
    img = imgs[0] if imgs else None
    if img and img.startswith("//"):
        img = "https:" + img
    cond = next((a.get("value") for a in l.get("attributes", []) if a.get("key") == "condition"), "")
    return Ad(
        item_id=l["itemId"],
        title=l.get("title", ""),
        price_eur=cents / 100 if cents else None,
        price_type=pi.get("priceType", ""),
        url=urljoin(MP_BASE, l.get("vipUrl", "")),
        image=img,
        condition=cond or "",
        city=(l.get("location") or {}).get("cityName", ""),
        seller=(l.get("sellerInformation") or {}).get("sellerName", ""),
    )


def fetch_ads(session, terms: list[str]) -> list[Ad]:
    ads: dict[str, Ad] = {}
    for term in terms:
        try:
            r = session.get(MP_SEARCH.format(q=quote_plus(term)), timeout=30)
            r.raise_for_status()
            for l in r.json().get("listings", []):
                ad = parse_ad(l)
                ads.setdefault(ad.item_id, ad)
        except Exception as e:
            print(f"Marktplaats search {term!r} failed: {e}", file=sys.stderr)
    return list(ads.values())


# --------------------------------------------------------------------------- #
# Alerts
# --------------------------------------------------------------------------- #

def eur(x: float) -> str:
    return f"€{x:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def build_message(ad: Ad, p: Product, ref_eur: float, disc: float, rate: float, note: str) -> str:
    e = html.escape
    price_label = "Bieden vanaf" if ad.price_type == "MIN_BID" else "Vraagprijs"
    lines = [
        f"📦 <b>Marktplaats: {e(ad.title)}</b>",
        "",
        f"💰 {price_label}: <b>{eur(ad.price_eur)}</b>",
        f"📈 PriceCharting {e(p.set_name)} {e(p.title)}: {eur(ref_eur)} (${p.price_usd:,.2f}) → −{disc:.0f}%",
    ]
    if note:
        lines.append(f"ℹ️ {e(note)}")
    if disc >= 70:
        lines.append("⚠️ Suspiciously cheap: check for a placeholder price or a scam")
    extra = " · ".join(x for x in [ad.condition, ad.city, ad.seller] if x)
    if extra:
        lines.append(f"📍 {e(extra)}")
    lines += ["", f'<a href="{e(ad.url)}">Open on Marktplaats</a> · <a href="{e(p.url)}">PriceCharting</a>']
    return "\n".join(lines)


def run(dry_run: bool = False, verbose: bool = False) -> int:
    cfg = load_config()
    if not cfg.get("enabled", True):
        print("Marktplaats watcher disabled in config.json")
        return 0
    state = load_state()
    first_run = "seen" not in state
    seen: dict[str, int] = state.get("seen", {})
    now = int(time.time())

    session = new_session()
    catalogue = get_catalogue(session, state)
    rate = get_usd_to_eur(session, state, float(cfg.get("usd_to_eur_fallback") or 0.92))
    ads = fetch_ads(session, cfg.get("search_terms") or DEFAULTS["search_terms"])
    print(f"Fetched {len(ads)} ads, catalogue {len(catalogue)} ETBs, 1 USD = €{rate:.4f}")
    if first_run:
        print("First run: remembering current ads without alerting")

    thr = float(cfg.get("min_discount_pct", 30))
    ok_types = {"FIXED"} | ({"MIN_BID"} if cfg.get("include_bids", True) else set())
    alerts = 0
    for ad in ads:
        if ad.key in seen:
            continue
        seen[ad.key] = now
        if first_run:
            continue

        def skip(why: str) -> None:
            if verbose:
                print(f"  skip  {why:45.45}  {ad.title[:70]}")

        if ad.price_type not in ok_types or not ad.price_eur:
            skip(f"no usable price ({ad.price_type})")
            continue
        if ad.price_eur < float(cfg.get("min_price_eur") or 0):
            skip("below min price")
            continue
        if cfg.get("max_price_eur") and ad.price_eur > float(cfg["max_price_eur"]):
            skip("above max price")
            continue
        why = junk_reason(ad.title)
        if why:
            skip(why)
            continue
        p, note = match_product(ad.title, catalogue)
        if not p:
            skip(note)
            continue
        ref = p.price_usd * rate
        disc = (1 - ad.price_eur / ref) * 100
        hit = disc >= thr
        print(f"  {'ALERT' if hit else 'ok   '} {eur(ad.price_eur):>10} vs {eur(ref):>10} ({-disc:+.0f}%)  "
              f"{p.set_name} {p.title}  <- {ad.title[:60]}")
        if hit:
            msg = build_message(ad, p, ref, disc, rate, note)
            if dry_run:
                print("-" * 60 + "\n" + msg + "\n" + "-" * 60)
            else:
                send_telegram(msg, ad.image)
            alerts += 1

    keep_after = now - SEEN_KEEP_DAYS * 86400
    state["seen"] = {k: v for k, v in seen.items() if v >= keep_after}
    state["last_run"] = now
    save_state(state)
    print(f"Done. {alerts} alert(s).")
    if os.environ.get("GITHUB_ACTIONS"):  # one-line summary on the run's page
        print(f"::notice title=Marktplaats::{len(ads)} ads checked, {len(catalogue)} ETBs in catalogue, "
              f"{alerts} alert(s){' (first run, just remembering ads)' if first_run else ''}")
    return alerts


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="print alerts instead of sending")
    ap.add_argument("-v", "--verbose", action="store_true", help="show why ads were skipped")
    args = ap.parse_args()
    run(dry_run=args.dry_run, verbose=args.verbose)


if __name__ == "__main__":
    main()
