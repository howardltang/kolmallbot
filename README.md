# KoL Mall Bot

A trading bot for [Kingdom of Loathing](https://www.kingdomofloathing.com) that monitors mall prices and automatically buys and sells items based on configured price ranges. Includes both a web UI and a CLI interface.

---

## Features

- **Auto Monitor** — Continuously watches item prices; buys when price drops below a minimum, lists when price rises above a maximum
- **Own-store exclusion** — When evaluating sell prices, ignores your own store's listings so they don't skew comparisons
- **Undercutting** — Configurable undercut percentage so listings are priced just below the current market low
- **Meat balance check** — Before buying, checks available meat and buys as many units as affordable up to the configured quantity
- **Inventory browser** — View all tradeable inventory items with cached mall prices
- **Store viewer** — View your current mall store listings with live price lookup
- **Stock Mall** — Bulk-list inventory items above a price threshold with a configurable markup
- **Item cache** — Persists item names, tradeability, and autosell values to disk to avoid redundant API calls
- **Web UI** — Full browser-based interface with real-time job output

---

## Requirements

- Python 3.8+
- `requests`
- `beautifulsoup4`
- `flask`

Install dependencies:

```bash
pip install requests beautifulsoup4 flask
```

---

## Setup

1. Clone or copy the `mallbot/` directory.
2. Edit `config.json` and fill in your KoL username and password (or leave blank to be prompted at login).
3. Optionally configure `settings` (request delay, markup %, default stock quantity).

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
| **Stock Mall** | Bulk-list inventory items above a minimum price with markup |
| **Auto Monitor** | Start the price monitoring loop with configurable interval and undercut % |
| **View My Store** | See current store listings; check live mall prices per item |

---

## Running the CLI

```bash
python mallbot.py
```

You will be prompted for your username and password if not set in `config.json`, then presented with an interactive menu.

---

## Price Ranges

Price ranges define how the bot behaves for a specific item:

| Field | Description |
|---|---|
| `min_price` | Buy the item when the mall price drops below this value |
| `max_price` | List the item when the mall price rises above this value |
| `buy_qty` | Maximum units to buy per trigger (subject to available meat) |

Ranges can be added via the **Auto Monitor** panel in the web UI by entering an item ID or name. If a name search returns multiple items, the bot will display the matches and their IDs so you can select the correct one.

---

## Auto Monitor behaviour

1. Fetches all mall listings for each configured item (exact-name search).
2. Excludes your own store's listings from the price comparison.
3. **Buy side** — if the lowest external price is below `min_price`, buys up to `buy_qty` units (capped by available meat).
4. **Sell side** — if the lowest external price is above `max_price`, lists any held inventory at:
   ```
   list_price = max(max_price, floor(lowest_external_price × (1 − undercut% / 100)))
   ```
5. Sleeps for the configured interval, then repeats.

---

## Files

| File | Description |
|---|---|
| `mallbot.py` | Core bot logic: KoL session, item cache, mall/store functions, CLI |
| `web_mallbot.py` | Flask web UI and background job runner |
| `config.json` | User-editable configuration (credentials, settings, price ranges) |
| `item_cache.json` | Auto-generated cache of item metadata (do not edit manually) |
| `mallbot.log` | Debug log file |

---

## Notes

- The bot enforces a configurable delay between HTTP requests (default 1 second) to avoid hammering the KoL servers.
- Item names are resolved from the KoL item API when first encountered and cached locally. Items not in your inventory can be looked up by name via a mall search.
- The web UI is single-user and intended to run locally. Do not expose it to the internet.
