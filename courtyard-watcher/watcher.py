"""
Courtyard deal watcher.

Polls the Courtyard.io marketplace for new listings matching a search (default:
Rayquaza), compares each asking price to
  1. Courtyard's own Fair Market Value (FMV), and
  2. the PriceCharting price for the same card + grade,
and sends a Telegram alert when the discount is big enough.

Runs once per invocation (GitHub Actions calls it every few minutes).
State (which listings were already seen, PriceCharting cache) lives in state.json.

Usage:
  python watcher.py                 normal run
  python watcher.py --dry-run       print alerts instead of sending them
  python watcher.py --test-telegram send a test message and exit
"""

from __future__ import annotations

import argparse
import html
import json
import math
import os
import re
import sys
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus, urljoin

from bs4 import BeautifulSoup

try:
    from curl_cffi import requests as http  # browser-like TLS, avoids bot blocks

    def new_session():
        return http.Session(impersonate="chrome")

except ImportError:  # pragma: no cover - fallback for local testing
    import requests as http  # type: ignore

    def new_session():
        s = http.Session()
        s.headers["User-Agent"] = (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
        )
        return s


ROOT = Path(__file__).parent
CONFIG_PATH = ROOT / "config.json"
STATE_PATH = ROOT / "state.json"

COURTYARD_PAGE = "https://courtyard.io/marketplace"
COURTYARD_SEARCH = "https://api.courtyard.io/1/indexes/*/queries"
COURTYARD_INDEX = "marketplace_prod_recently_listed"  # newest listings first
COURTYARD_ASSET = "https://courtyard.io/asset/{}"
PRICECHARTING_SEARCH = "https://www.pricecharting.com/search-products?type=prices&q={}"

PC_CACHE_HOURS = 12
SEEN_KEEP_DAYS = 14


# --------------------------------------------------------------------------- #
# Config / state
# --------------------------------------------------------------------------- #

DEFAULT_CONFIG: dict[str, Any] = {
    "search_query": "rayquaza",
    "subject_must_contain": "rayquaza",
    "min_discount_pct": 15,
    "alert_when": "either",  # "either" | "both" | "fmv" | "pricecharting"
    "min_fmv_confidence": 0,
    "min_price_usd": 0,
    "max_price_usd": None,
    "graders": [],
    "min_grade": None,
    "languages": [],
    "use_pricecharting": True,
    "first_run_lookback_minutes": 30,
}


def load_config() -> dict[str, Any]:
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        user = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        cfg.update({k: v for k, v in user.items() if not k.startswith("_")})
    return cfg


def load_state() -> dict[str, Any]:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {}


def save_state(state: dict[str, Any]) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=1), encoding="utf-8")


# --------------------------------------------------------------------------- #
# Courtyard
# --------------------------------------------------------------------------- #

@dataclass
class Listing:
    listing_id: str
    asset_id: str
    title: str
    price_usd: float
    fmv_usd: float | None
    fmv_confidence: int | None
    grader: str
    grade: str
    subject: str
    card_number: str
    set_name: str
    language: str
    year: str
    listed_ts: int
    image_url: str | None
    pc: dict[str, Any] = field(default_factory=dict)

    @property
    def url(self) -> str:
        return COURTYARD_ASSET.format(self.asset_id)


def get_algolia_key(session) -> str:
    """Courtyard's public search key is embedded in the marketplace page."""
    env = os.environ.get("COURTYARD_ALGOLIA_KEY")
    if env:
        return env
    r = session.get(COURTYARD_PAGE, timeout=30)
    r.raise_for_status()
    m = re.search(r'algoliaApiKey\\*"\s*:\s*\\*"([A-Za-z0-9]{20,64})', r.text)
    if not m:
        raise RuntimeError(
            "Could not find Courtyard's search key on the marketplace page. "
            "The site may have changed."
        )
    return m.group(1)


def fetch_listings(session, key: str, query: str, hits: int = 100) -> list[dict]:
    body = {
        "requests": [
            {
                "indexName": COURTYARD_INDEX,
                "params": f"query={quote_plus(query)}&hitsPerPage={hits}"
                "&attributesToHighlight=&attributesToSnippet=",
            }
        ]
    }
    r = session.post(
        COURTYARD_SEARCH,
        json=body,
        headers={
            "x-algolia-api-key": key,
            "x-algolia-application-id": "courtyard",
            "origin": "https://courtyard.io",
            "referer": "https://courtyard.io/",
        },
        timeout=30,
    )
    r.raise_for_status()
    return r.json()["results"][0]["hits"]


def _num(x) -> float | None:
    try:
        f = float(x)
        return f if math.isfinite(f) else None
    except (TypeError, ValueError):
        return None


def parse_hit(h: dict) -> Listing | None:
    price = _num((h.get("price") or {}).get("amountUsd"))
    if price is None:
        ll = h.get("latestListing") or {}
        price = _num(((ll.get("price") or {}).get("amount") or {}).get("usd"))
    if price is None:
        return None

    md = h.get("metadata") or {}
    if isinstance(md, str):
        try:
            md = json.loads(md)
        except json.JSONDecodeError:
            md = {}
    cert = h.get("certification") or {}
    listing = h.get("latestListing") or {}
    asset_id = h.get("objectID", "")
    order_id = str(listing.get("orderId") or "")
    listed_ts = int(h.get("listedAt_timestamp") or listing.get("createdAt_timestamp") or 0)
    conf = h.get("estimatedValueConfidence")

    return Listing(
        # a relist (e.g. a price drop) gets a new order id, so it can alert again
        listing_id=order_id or f"{asset_id}:{price}:{listed_ts}",
        asset_id=asset_id,
        title=h.get("title", ""),
        price_usd=price,
        fmv_usd=_num(h.get("estimatedValueUsd")),
        fmv_confidence=int(conf) if str(conf or "").isdigit() else None,
        grader=(cert.get("agency") or md.get("Grader") or "").strip(),
        grade=(cert.get("grade") or md.get("Grade") or "").strip(),
        subject=(md.get("Title/Subject") or "").strip(),
        card_number=(md.get("Card Number") or h.get("assetNumber") or "").strip(),
        set_name=(md.get("Set") or h.get("set") or "").strip(),
        language=(md.get("Language") or "").strip(),
        year=str(md.get("Year") or h.get("year") or ""),
        listed_ts=listed_ts,
        image_url=h.get("imageUrl"),
    )


def grade_number(grade: str) -> float | None:
    m = re.match(r"\s*(\d+(?:\.\d+)?)", grade or "")
    return float(m.group(1)) if m else None


def passes_filters(l: Listing, cfg: dict) -> bool:
    needle = (cfg.get("subject_must_contain") or "").lower()
    if needle and needle not in (l.subject or l.title).lower():
        return False
    if l.price_usd < (cfg.get("min_price_usd") or 0):
        return False
    if cfg.get("max_price_usd") and l.price_usd > cfg["max_price_usd"]:
        return False
    graders = [g.upper() for g in cfg.get("graders") or []]
    if graders and l.grader.upper() not in graders:
        return False
    if cfg.get("min_grade") is not None:
        g = grade_number(l.grade)
        if g is None or g < float(cfg["min_grade"]):
            return False
    langs = [x.lower() for x in cfg.get("languages") or []]
    if langs and l.language.lower() not in langs:
        return False
    return True


# --------------------------------------------------------------------------- #
# PriceCharting
# --------------------------------------------------------------------------- #

def pc_grade_label(grader: str, grade: str) -> str | None:
    """Map a Courtyard grade (agency + '10 GEM MINT') to a PriceCharting row label."""
    n = grade_number(grade)
    if n is None:
        return None
    g = grader.upper().strip()
    words = grade.upper()
    if n == 10:
        if g == "CGC" and "PRISTINE" in words:
            return "CGC 10 Pristine"
        if g in ("BGS", "BECKETT") and "BLACK" in words:
            return "BGS 10 Black"
        if g == "BECKETT":
            g = "BGS"
        if g in ("PSA", "CGC", "BGS", "SGC", "TAG", "ACE"):
            return f"{g} 10"
        return None
    if n == 9.5:
        return "Grade 9.5"
    return f"Grade {int(n)}"  # PriceCharting buckets 8.5 with 8, 7.5 with 7, etc.


def parse_money(s: str) -> float | None:
    s = (s or "").replace(",", "").replace("$", "").strip()
    try:
        return float(s)
    except ValueError:
        return None


def parse_pc_product(page_html: str) -> dict[str, float]:
    soup = BeautifulSoup(page_html, "html.parser")
    prices: dict[str, float] = {}
    for tr in soup.select("#full-prices tr"):
        tds = tr.find_all("td")
        if len(tds) >= 2:
            val = parse_money(tds[1].get_text())
            if val is not None:
                prices[" ".join(tds[0].get_text().split())] = val
    return prices


def parse_pc_search(page_html: str) -> list[dict[str, str]]:
    soup = BeautifulSoup(page_html, "html.parser")
    rows = []
    for tr in soup.select("table#games_table tbody tr"):
        a = tr.select_one("td.title a")
        if not a:
            continue
        console = tr.select_one("td.console")
        rows.append(
            {
                "name": " ".join(a.get_text().split()),
                "set": " ".join(console.get_text().split()) if console else "",
                "href": a.get("href", ""),
            }
        )
    return rows


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9 ]+", " ", s.lower())


def _tokens(s: str) -> set[str]:
    stop = {"pokemon", "the", "and", "of", "card", "cards", "tcg"}
    return {t for t in _norm(s).split() if t not in stop}


def _norm_number(s: str) -> str:
    s = re.sub(r"[^A-Z0-9]", "", (s or "").upper())
    return re.sub(r"(?<![0-9])0+(?=[0-9])", "", s)  # SWSH029 -> SWSH29, 007 -> 7


def pick_pc_row(rows: list[dict], l: Listing) -> dict | None:
    want_no = _norm_number(l.card_number)
    want_set = _tokens(l.set_name)
    want_subject = _tokens(l.subject)
    japanese = l.language.lower() == "japanese"
    best, best_score = None, 0.0
    for r in rows:
        m = re.search(r"#\s*([A-Za-z0-9\-]+)", r["name"])
        if not m or (want_no and _norm_number(m.group(1)) != want_no):
            continue
        name_tok = _tokens(r["name"])
        if want_subject and not (want_subject & name_tok):
            continue
        set_tok = _tokens(r["set"])
        score = len(want_set & set_tok) / max(1, len(want_set | set_tok)) + 0.1
        if ("japanese" in set_tok) != japanese:
            score -= 0.5
        if score > best_score:
            best, best_score = r, score
    return best


def pricecharting_lookup(session, l: Listing, cache: dict) -> dict | None:
    """Returns {'url': ..., 'prices': {label: usd}} or None, cached for a while."""
    key = "|".join([_norm(l.subject), _norm_number(l.card_number), _norm(l.set_name), l.language.lower()])
    hit = cache.get(key)
    if hit and time.time() - hit.get("ts", 0) < PC_CACHE_HOURS * 3600:
        return hit.get("data")

    data = None
    try:
        q = f"{l.subject} {l.card_number}".strip()
        if l.language.lower() == "japanese":
            q += " japanese"
        r = session.get(PRICECHARTING_SEARCH.format(quote_plus(q)), timeout=30)
        r.raise_for_status()
        prices = parse_pc_product(r.text)
        url = str(r.url).split("?")[0]
        if not prices:  # several results: pick the best match, then open it
            row = pick_pc_row(parse_pc_search(r.text), l)
            if row:
                url = urljoin("https://www.pricecharting.com", row["href"]).split("?")[0]
                r2 = session.get(url, timeout=30)
                r2.raise_for_status()
                prices = parse_pc_product(r2.text)
        if prices:
            data = {"url": url, "prices": prices}
    except Exception as e:  # never let PriceCharting break the Courtyard check
        print(f"  PriceCharting lookup failed for {l.title!r}: {e}", file=sys.stderr)
        return None

    cache[key] = {"ts": time.time(), "data": data}
    return data


# --------------------------------------------------------------------------- #
# Deal logic + Telegram
# --------------------------------------------------------------------------- #

def discount(price: float, ref: float | None) -> float | None:
    if not ref or ref <= 0:
        return None
    return (1 - price / ref) * 100


def evaluate(l: Listing, cfg: dict) -> tuple[bool, float | None, float | None]:
    thr = float(cfg.get("min_discount_pct", 15))
    fmv_ok_conf = (l.fmv_confidence or 0) >= int(cfg.get("min_fmv_confidence") or 0)
    d_fmv = discount(l.price_usd, l.fmv_usd) if fmv_ok_conf else None
    d_pc = discount(l.price_usd, l.pc.get("price"))
    hit_fmv = d_fmv is not None and d_fmv >= thr
    hit_pc = d_pc is not None and d_pc >= thr
    mode = cfg.get("alert_when", "either")
    ok = {
        "either": hit_fmv or hit_pc,
        "both": hit_fmv and hit_pc,
        "fmv": hit_fmv,
        "pricecharting": hit_pc,
    }.get(mode, hit_fmv or hit_pc)
    return ok, d_fmv, d_pc


def fmt_usd(x: float | None) -> str:
    return "n/a" if x is None else f"${x:,.2f}"


def fmt_disc(d: float | None) -> str:
    if d is None:
        return ""
    return f" ({'−' if d >= 0 else '+'}{abs(d):.0f}%)"


def build_message(l: Listing, d_fmv: float | None, d_pc: float | None) -> str:
    e = html.escape
    lines = [
        f"🐉 <b>{e(l.title)}</b>",
        "",
        f"💰 Listed: <b>{fmt_usd(l.price_usd)}</b>",
    ]
    conf = f" · confidence {l.fmv_confidence}/5" if l.fmv_confidence else ""
    lines.append(f"📊 Courtyard FMV: {fmt_usd(l.fmv_usd)}{fmt_disc(d_fmv)}{conf}")
    if l.pc.get("price") is not None:
        lines.append(f"📈 PriceCharting {e(l.pc['label'])}: {fmt_usd(l.pc['price'])}{fmt_disc(d_pc)}")
    elif l.pc.get("note"):
        lines.append(f"📈 PriceCharting: {e(l.pc['note'])}")
    links = [f'<a href="{e(l.url)}">Open on Courtyard</a>']
    if l.pc.get("url"):
        links.append(f'<a href="{e(l.pc["url"])}">PriceCharting</a>')
    lines += ["", " · ".join(links)]
    return "\n".join(lines)


def send_telegram(text: str, image_url: str | None = None) -> None:
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    base = f"https://api.telegram.org/bot{token}"
    s = new_session()
    if image_url and len(text) <= 1000:
        r = s.post(
            f"{base}/sendPhoto",
            data={"chat_id": chat_id, "photo": image_url, "caption": text, "parse_mode": "HTML"},
            timeout=30,
        )
        if r.status_code == 200:
            return
    r = s.post(
        f"{base}/sendMessage",
        data={"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": "false"},
        timeout=30,
    )
    if r.status_code != 200:
        raise RuntimeError(f"Telegram error {r.status_code}: {r.text[:300]}")


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def run(dry_run: bool = False) -> int:
    cfg = load_config()
    state = load_state()
    first_run = not state
    seen: dict[str, int] = state.get("seen", {})
    pc_cache: dict = state.get("pc_cache", {})
    now = int(time.time())

    session = new_session()
    key = get_algolia_key(session)
    hits = fetch_listings(session, key, cfg["search_query"])
    print(f"Fetched {len(hits)} newest listings for {cfg['search_query']!r}")

    cutoff = now - int(cfg.get("first_run_lookback_minutes", 30)) * 60 if first_run else 0
    alerts = 0
    for h in hits:
        l = parse_hit(h)
        if not l or l.listing_id in seen:
            continue
        seen[l.listing_id] = now
        if l.listed_ts and l.listed_ts < cutoff:
            continue  # first run: don't flood you with old listings
        if not passes_filters(l, cfg):
            continue

        if cfg.get("use_pricecharting", True):
            label = pc_grade_label(l.grader, l.grade)
            pc = pricecharting_lookup(session, l, pc_cache)
            if pc and label:
                l.pc = {"url": pc["url"], "label": label, "price": pc["prices"].get(label)}
                if l.pc["price"] is None:
                    l.pc["note"] = f"no {label} price"
            elif pc:
                l.pc = {"url": pc["url"], "note": f"grade {l.grader} {l.grade} not mapped"}
            else:
                l.pc = {"note": "card not found"}

        ok, d_fmv, d_pc = evaluate(l, cfg)
        print(
            f"  {'ALERT' if ok else 'skip '} {fmt_usd(l.price_usd):>10}  "
            f"fmv{fmt_disc(d_fmv) or ' n/a'}  pc{fmt_disc(d_pc) or ' n/a'}  {l.title[:80]}"
        )
        if ok:
            msg = build_message(l, d_fmv, d_pc)
            if dry_run:
                print("-" * 60 + "\n" + msg + "\n" + "-" * 60)
            else:
                send_telegram(msg, l.image_url)
            alerts += 1

    # prune old state so it stays small
    keep_after = now - SEEN_KEEP_DAYS * 86400
    state["seen"] = {k: v for k, v in seen.items() if v >= keep_after}
    state["pc_cache"] = {
        k: v for k, v in pc_cache.items() if now - v.get("ts", 0) < PC_CACHE_HOURS * 3600
    }
    state["last_run"] = now
    save_state(state)
    print(f"Done. {alerts} alert(s).")
    return alerts


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="print alerts instead of sending")
    ap.add_argument("--test-telegram", action="store_true", help="send a test message and exit")
    args = ap.parse_args()
    if args.test_telegram:
        send_telegram("✅ Courtyard watcher is connected. You'll get deals here.")
        print("Test message sent.")
        return
    run(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
