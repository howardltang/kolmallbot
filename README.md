# KoL Mall Bot

A trading bot for [Kingdom of Loathing](https://www.kingdomofloathing.com) that monitors mall prices and automatically buys and sells items based on configured price ranges and snipe rules. Includes a Flask web UI.

---

## Features

- **Auto Monitor** — Continuously watches item prices; buys when price drops below a minimum, lists when price rises above a maximum
- **Snipe Mode** — Detects and acts on mispriced listings using three prioritised scenarios (see below)
- **Parallel mall searches** — All items are fetched simultaneously each cycle via a thread pool, minimising latency
- **Priority buy queue** — Within a cycle, purchases are executed highest-estimated-profit first
- **Own-store repricing** — When the bot's own listing is identified as snipeable, it removes and re-lists at the better price instead of buying from itself
- **Undercutting** — Configurable undercut percentage so range-rule listings are priced just below market
- **Meat balance check** — Before buying, checks available meat and buys as many units as affordable up to the configured quantity
- **Inventory browser** — View all tradeable inventory items with cached mall prices and quantities
- **Store viewer** — View current mall store listings with live price lookup and inline price editing
- **Item cache** — Persists item names, tradeability, and autosell values to disk to avoid redundant API calls
- **Price data recording** — Saves the top-10 mall listings for each snipe item every monitor cycle to `price_data.jsonl` for historical analysis

---

## Requirements

- Python 3.8+
- `requests`
- `beautifulsoup4`
- `flask`

```bash
pip install requests beautifulsoup4 flask
```

---

## Setup

1. Clone or copy the `mallbot/` directory.
2. Copy `config.example.json` to `config.json`:
   ```bash
   cp config.example.json config.json
   ```
3. Edit `config.json` and fill in your KoL username and password (or leave blank to be prompted at login).
4. Optionally configure `settings` (request delay, markup %, default stock quantity).

### config.json structure

```json
{
  "credentials": {
    "username": "your_username",
    "password": "your_password"
  },
  "settings": {
    "request_delay_seconds": 1.0,
    "mall_markup_percent": 5,
    "default_stock_quantity": 1
  },
  "price_ranges": {
    "194": {
      "item_id": 194,
      "name": "Mr. Accessory",
      "min_price": 500000,
      "max_price": 1000000,
      "buy_qty": 1
    }
  },
  "snipe_items": {
    "194": {
      "item_id": 194,
      "name": "Mr. Accessory",
      "snipe_threshold": 700000
    }
  }
}
```

---

## Running the Web UI

```bash
python web_mallbot.py
```

Then open [http://localhost:8080](http://localhost:8080) in your browser.

### Web UI panels

| Panel | Description |
|---|---|
| **List by Price** | Browse tradeable inventory with cached/refreshed mall prices |
| **Auto Monitor** | Start the price monitoring loop; configure interval, undercut %, and snipe items |
| **View My Store** | See current store listings; check live mall prices and update prices inline |

---

## Price Ranges

Price ranges define buy/sell behaviour for a specific item:

| Field | Description |
|---|---|
| `min_price` | Buy the item when the mall price drops below this value |
| `max_price` | List the item when the mall price rises above this value |
| `buy_qty` | Maximum units to buy per trigger (capped by available meat) |

Ranges can be added via the **Price Ranges** section in the Auto Monitor panel. If a name search matches multiple items, the bot lists the matches and their IDs so you can select the correct one.

---

## Snipe Mode

Snipe items are evaluated every monitor cycle independently of price ranges. Three scenarios are checked in priority order:

### Scenario M — Mislisting (highest priority)
> P1 ≤ 50% of P2

Buy the entire cheapest listing regardless of quantity, relist at P2 − 1,000.

### Scenario A
> All of the cheapest N listings (up to 4 stores) are each ≥ `snipe_threshold` below listing N+1, combined quantity ≤ 4 (or ≤ 5 for a single listing)

Buy/reprice all N listings, relist at P(N+1) − 1,000.

### Scenario B
> P1 qty ≤ 2, P2 − P1 ≥ threshold × 0.7, P2 qty ≤ 3, P3 − P1 ≥ threshold

Buy/reprice P1 only, relist at P2 − 1,000.

The default `snipe_threshold` is 700,000 meat and is configurable per item. When the bot's own store appears in the buy list, the listing is repriced rather than purchased.

---

## Auto Monitor behaviour

1. Fetches mall listings for all range-rule items and snipe items **in parallel**.
2. Evaluates snipe opportunities (Scenario M → A → B).
3. Evaluates range rules (buy below `min_price`, sell above `max_price`).
4. Executes all buys in **descending estimated-profit order**, then all sells.
5. Records top-10 listings for each snipe item to `price_data.jsonl`.
6. Sleeps for the configured interval (default 60 s), then repeats.

Verbose mode (default off) suppresses routine "no opportunity" messages; buy and list events are always printed with a timestamp.

---

## Files

| File | Description |
|---|---|
| `mallbot.py` | Core bot logic: KoL session, item cache, mall/store functions |
| `web_mallbot.py` | Flask web UI and background job runner |
| `templates/index.html` | Main app HTML template |
| `templates/login.html` | Login page HTML template |
| `config.json` | User-editable configuration (credentials, settings, price ranges, snipe items) |
| `config.example.json` | Template config with example values |
| `item_cache.json` | Auto-generated cache of item metadata (do not edit manually) |
| `price_data.jsonl` | Auto-generated historical mall listing snapshots for snipe items |
| `mallbot.log` | Debug log file |

---

## Notes

- The bot enforces a configurable delay between HTTP requests (default 1 second) to avoid hammering the KoL servers. During parallel fetch phases the delay is lifted temporarily.
- Item names are resolved from the KoL item API when first encountered and cached locally.
- The web UI is single-user and intended to run locally. Do not expose it to the internet.
