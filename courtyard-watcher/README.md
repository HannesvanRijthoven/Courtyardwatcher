# Courtyard Rayquaza deal watcher

Every 5 minutes this checks the newest Rayquaza listings on Courtyard.io and sends you a
Telegram message when one is listed well below its value. Each listing is compared to:

- **Courtyard's FMV** (shown on the listing), and
- **PriceCharting** for the same card and grade (PSA 10, CGC 10 Pristine, Grade 9, …).

By default you get an alert when *either* is at least 15% below. A message looks like:

```
🐉 2022 Silver Tempest #TG29 Rayquaza Vmax (PSA 10 GEM MINT)
💰 Listed: $110.00
📊 Courtyard FMV: $120.00 (−8%) · confidence 4/5
📈 PriceCharting PSA 10: $150.00 (−27%)
Open on Courtyard · PriceCharting
```

It runs free on GitHub Actions — nothing to host.

---

## Setup (about 10 minutes)

### 1. Make a Telegram bot
1. In Telegram, open a chat with **@BotFather** and send `/newbot`.
2. Pick a name and a username (must end in `bot`).
3. BotFather replies with a **token** like `123456789:AAH...`. Keep it.
4. Open a chat with your new bot and send it any message (e.g. "hi").

### 2. Find your chat ID
Open this in your browser (paste your token in):

```
https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates
```

Look for `"chat":{"id":123456789` — that number is your **chat ID**.
(If the page shows `"result":[]`, send your bot another message and refresh.)

### 3. Put the code on GitHub
1. Make a free account at github.com if you don't have one.
2. Click **+ → New repository**. Name it e.g. `courtyard-watcher`, choose **Public**
   (public = unlimited free minutes; your token stays secret either way), and create it.
3. Click **uploading an existing file** and drag in `watcher.py`, `config.json`,
   `requirements.txt` and `README.md`. Commit.
4. Add the workflow: **Add file → Create new file**, type the name
   `.github/workflows/watch.yml` (the slashes create the folders), paste the contents of
   `watch.yml`, and commit.

### 4. Add your secrets
In the repo: **Settings → Secrets and variables → Actions → New repository secret**. Add two:

| Name                 | Value                  |
|----------------------|------------------------|
| `TELEGRAM_BOT_TOKEN` | the token from step 1  |
| `TELEGRAM_CHAT_ID`   | the number from step 2 |

### 5. Test it
**Actions** tab → **Courtyard deal watcher** → **Run workflow**. After ~30 seconds,
open the run and the **Check Courtyard** step: you should see `Fetched 100 newest listings`
and a line per listing it checked. From then on it runs every 5 minutes by itself.

The first run only looks at the last 30 minutes of listings, so you won't get flooded.

---

## Changing what you get alerted on

Edit `config.json` on GitHub (click it → pencil icon → commit). The main settings:

| Setting | What it does |
|---|---|
| `min_discount_pct` | How far below value it must be. `15` = 15% under. |
| `alert_when` | `either` (FMV or PriceCharting), `both`, `fmv` or `pricecharting`. |
| `min_fmv_confidence` | Courtyard rates its FMV 1–5. `3` ignores shaky FMVs. |
| `max_price_usd` / `min_price_usd` | Price range, e.g. `500`. `null` = no limit. |
| `graders` | e.g. `["PSA"]`. Empty = all. |
| `min_grade` | e.g. `9`. `null` = any. |
| `languages` | e.g. `["English"]` or `["Japanese"]`. Empty = all. |
| `search_query` / `subject_must_contain` | Change both to watch a different Pokémon, e.g. `"charizard"`. |

---

## Good to know

- **Timing:** GitHub sometimes starts scheduled runs a few minutes late, so treat it as
  "within ~5–10 minutes", not instant.
- **Inactivity:** GitHub pauses scheduled workflows in a repo with no commits for 60 days.
  It emails you first; re-enable with one click in the Actions tab, or just tweak
  `config.json` now and then.
- **PriceCharting matching** is done by card name + number + set. If it can't find the
  card or grade, the alert still comes through on FMV and says so.
- **Unofficial:** this reads the same data the Courtyard and PriceCharting websites show.
  If either site changes its layout the watcher may stop; the Actions run will show an error.
- **Test without Telegram:** `python watcher.py --dry-run` prints alerts instead of sending;
  `python watcher.py --test-telegram` sends a test message.
