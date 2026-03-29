"""
KoL Mall Bot — Interactive Menu Edition
========================================
Run with:
    python mallbot.py
"""

import re
import json
import time
import getpass
import logging
import sys
from pathlib import Path
from typing import Optional, Dict, List, Tuple

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Logging — file only; the menu prints its own output to stdout
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler("mallbot.log")],
)
logger = logging.getLogger(__name__)

KOL_BASE    = "https://www.kingdomofloathing.com"
CONFIG_PATH = Path(__file__).parent / "config.json"
CACHE_PATH  = Path(__file__).parent / "item_cache.json"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _hr(char="─", width=60):
    print(char * width)

def _status(msg: str):
    """Print a timestamped status line to the screen."""
    print(f"  [{time.strftime('%H:%M:%S')}] {msg}", flush=True)

def _ask(prompt: str, default: str = "") -> str:
    val = input(f"  {prompt}").strip()
    return val if val else default

def _ask_int(prompt: str, default: Optional[int] = None) -> Optional[int]:
    raw = _ask(prompt, "" if default is None else str(default))
    try:
        return int(raw.replace(",", ""))
    except ValueError:
        return default

def _pause():
    input("\n  Press Enter to continue...")


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------

class KoLSession:
    def __init__(self, username: str, password: str, delay: float = 1.0):
        self.username = username
        self.password = password
        self.delay    = delay
        self._session = requests.Session()
        self._session.headers["User-Agent"] = (
            "KoL-MallBot/1.0 (Python)"
        )
        self.logged_in = False
        self.pwd_hash:  Optional[str] = None
        self.player_id: Optional[str] = None
        self._last_req = 0.0

    def _throttle(self):
        elapsed = time.time() - self._last_req
        if elapsed < self.delay:
            time.sleep(self.delay - elapsed)
        self._last_req = time.time()

    def get(self, path: str, params: Optional[Dict] = None) -> requests.Response:
        self._throttle()
        resp = self._session.get(f"{KOL_BASE}/{path}", params=params, timeout=30)
        resp.raise_for_status()
        return resp

    def post(self, path: str, data: Optional[Dict] = None) -> requests.Response:
        self._throttle()
        resp = self._session.post(f"{KOL_BASE}/{path}", data=data, timeout=30)
        resp.raise_for_status()
        return resp

    def login(self) -> bool:
        print("  Connecting to Kingdom of Loathing ...")
        self.get("login.php")
        resp = self.post("login.php", data={
            "loggingin": "Yup.",
            "loginname": self.username,
            "password":  self.password,
            "secure":    "0",
        })

        # KoL redirects after login; check we didn't land back on the login page
        if "login.php" in resp.url or "Invalid login" in resp.text or \
                "wrong password" in resp.text.lower():
            print("  Login failed — check your username and password.")
            logger.error(f"Login rejected by server. Final URL: {resp.url}")
            return False

        # Try to get pwd hash from the landing page first
        self.pwd_hash = self._extract_pwd(resp.text)

        # If not found there, ask the status API directly (most reliable)
        if not self.pwd_hash:
            try:
                status = self._session.get(
                    f"{KOL_BASE}/api.php",
                    params={"what": "status", "for": "MallBot"},
                    timeout=30,
                ).json()
                self.pwd_hash  = status.get("pwd")
                self.player_id = str(status["playerid"]) if "playerid" in status else None
                logger.debug(f"pwd hash obtained from status API: {bool(self.pwd_hash)}")
            except Exception as e:
                logger.warning(f"Could not fetch status API after login: {e}")

        if not self.pwd_hash:
            print("  Logged in but could not obtain session token. "
                  "The bot may not work correctly.")
            logger.error("Login appeared to succeed but pwd hash not found anywhere.")
            # Still mark as logged in — some actions may still work
        else:
            logger.info(f"Logged in as {self.username}, player_id={self.player_id}, pwd hash obtained.")

        self.logged_in = True
        print(f"  Logged in as {self.username}.")
        return True

    def logout(self):
        if self.logged_in:
            self.get("logout.php")
            self.logged_in = False
            logger.info("Logged out.")

    def _extract_pwd(self, html: str) -> Optional[str]:
        for pattern in [r'pwd=([a-f0-9]+)', r'"pwd":"([a-f0-9]+)"']:
            m = re.search(pattern, html)
            if m:
                return m.group(1)
        return None

    def refresh_pwd(self):
        data = self.get("api.php", params={"what": "status", "for": "MallBot"}).json()
        if "pwd" in data:
            self.pwd_hash = data["pwd"]


# ---------------------------------------------------------------------------
# Item cache
# ---------------------------------------------------------------------------

class ItemCache:
    """
    In-memory item metadata cache backed by a JSON file on disk.
    Item names, tradeability, and autosell values never change in KoL, so the
    disk cache is permanent — loading it skips one HTTP request per known item.
    """

    def __init__(self, session: KoLSession):
        self.session = session
        self._cache: Dict[int, Dict] = self._load_disk_cache()
        self._dirty = False  # tracks whether new entries need to be saved

    @staticmethod
    def _load_disk_cache() -> Dict[int, Dict]:
        if CACHE_PATH.exists():
            try:
                with open(CACHE_PATH) as f:
                    raw = json.load(f)
                # Keys are stored as strings in JSON; convert back to int
                cache = {int(k): v for k, v in raw.items()}
                logger.info(f"Item cache loaded from disk: {len(cache)} known item(s).")
                return cache
            except Exception as e:
                logger.warning(f"Could not load item cache from disk: {e}")
        return {}

    def save(self):
        if not self._dirty:
            return
        try:
            with open(CACHE_PATH, "w") as f:
                json.dump({str(k): v for k, v in self._cache.items()}, f)
            logger.info(f"Item cache saved to disk: {len(self._cache)} item(s).")
        except Exception as e:
            logger.warning(f"Could not save item cache: {e}")

    def get(self, item_id: int) -> Dict:
        if item_id not in self._cache:
            resp = self.session.get("api.php", params={
                "what": "item", "id": item_id, "for": "MallBot",
            })
            try:
                data = resp.json()
                logger.debug(f"Item #{item_id} API response: {data}")

                if not isinstance(data, dict):
                    raise ValueError(f"API returned non-dict: {data!r}")

                if "cantransfer" in data:
                    tradeable = bool(int(data["cantransfer"]))
                elif "notrade" in data:
                    tradeable = not bool(int(data["notrade"]))
                elif "tradeable" in data:
                    tradeable = bool(int(data["tradeable"]))
                else:
                    logger.warning(f"Item #{item_id}: no tradeability field, assuming tradeable. "
                                   f"Keys: {list(data.keys())}")
                    tradeable = True

                self._cache[item_id] = {
                    "name":      data.get("name", f"item#{item_id}"),
                    "tradeable": tradeable,
                    "autosell":  int(data.get("autosell", data.get("sellvalue", 0))),
                    "descid":    str(data.get("descid", "")),
                }
            except (ValueError, KeyError) as e:
                logger.warning(f"Item #{item_id}: failed to parse API response ({e}). "
                               f"Raw: {resp.text[:300]}")
                self._cache[item_id] = {
                    "name": f"item#{item_id}", "tradeable": True, "autosell": 0,
                    "descid": "",
                }
            self._dirty = True
        return self._cache[item_id]

    def get_desc_id(self, item_id: int) -> str:
        """Return the descid for item_id, fetching from API if not cached."""
        entry = self._cache.get(item_id, {})
        if entry.get("descid"):
            return entry["descid"]
        # Force a fresh API lookup to get descid
        self._cache.pop(item_id, None)
        return self.get(item_id).get("descid", "")


# ---------------------------------------------------------------------------
# Mall / store utilities
# ---------------------------------------------------------------------------

def _fetch_mall_listings(session: KoLSession, item_name: str,
                         exact: bool = False) -> List[Dict]:
    """
    Fetch all mall listings for item_name. Returns a list of dicts:
      {price, limit, store_id, search_item_id, item_name}
    limit=0 means unlimited; any positive integer means a per-day purchase limit.
    Set exact=True to wrap the name in quotes for an exact-match search.
    """
    query = f'"{item_name}"' if exact else item_name
    resp = session.get("mall.php", params={
        "pudnuggler": query,
        "category":   "",
        "start":      0,
    })
    logger.debug(f"Mall search for '{item_name}' (exact={exact}): {len(resp.text)} chars")

    if "whichstore" not in resp.text:
        return []

    soup = BeautifulSoup(resp.text, "html.parser")

    # Build item_id -> name map from the item header rows (id="item_N")
    item_names: Dict[str, str] = {}
    for hdr in soup.find_all("tr", id=re.compile(r"^item_\d+$")):
        iid = hdr["id"].split("_", 1)[1]
        anchor = hdr.find("b")
        if anchor:
            item_names[iid] = anchor.get_text(strip=True)

    listings = []

    # Each store row has id="stock_STOREID_SEARCHITEM"
    for row in soup.find_all("tr", id=re.compile(r"^stock_")):
        price_td = row.find("td", class_="price")
        if not price_td:
            continue
        link = price_td.find("a", href=True)
        if not link:
            continue
        href = link["href"]
        store_m = re.search(r'whichstore=(\d+)', href)
        sitem_m = re.search(r'searchitem=(\d+)', href)
        price_m = re.search(r'searchprice=(\d+)', href)
        if not (store_m and sitem_m and price_m):
            continue

        # The limit cell has exactly class="small" (no other classes).
        # &nbsp; or empty text = unlimited (0); a number = daily purchase cap.
        limit = 0
        for td in row.find_all("td"):
            if td.get("class") == ["small"]:
                digits = re.sub(r'\D', '', td.get_text())
                if digits:
                    limit = int(digits)
                break

        siid = sitem_m.group(1)
        listing = {
            "price":          int(price_m.group(1)),
            "limit":          limit,
            "store_id":       store_m.group(1),
            "search_item_id": siid,
            "item_name":      item_names.get(siid, ""),
        }
        logger.debug(f"  listing: price={listing['price']:,}  limit={listing['limit']}  "
                     f"store={listing['store_id']}  searchitem={listing['search_item_id']}")
        listings.append(listing)

    logger.debug(f"  → {len(listings)} listing(s) parsed for '{item_name}'")
    return listings


def get_mall_price(session: KoLSession, item_id: int, item_name: str) -> Dict:
    """
    Return {"min_price": N_or_None, "min_unlimited": N_or_None}.
    min_price    — lowest price across all listings.
    min_unlimited — lowest price among listings with no purchase limit.
                    None if every listing has a limit (or there are no listings).
    """
    listings = _fetch_mall_listings(session, item_name)
    if not listings:
        logger.warning(f"No mall listings found for '{item_name}' (#{item_id}).")
        return {"min_price": None, "min_unlimited": None}

    min_price = min(l["price"] for l in listings)
    unlimited = [l["price"] for l in listings if l["limit"] == 0]
    min_unlimited = min(unlimited) if unlimited else None

    return {"min_price": min_price, "min_unlimited": min_unlimited}


def get_my_store(session: KoLSession) -> List[Dict]:
    resp = session.get("backoffice.php", params={"action": "store", "pwd": session.pwd_hash})
    logger.debug(f"backoffice.php response: {len(resp.text)} chars")
    soup = BeautifulSoup(resp.text, "html.parser")
    listings = []
    # Row structure: <tr class="deets">
    #   <td><img/></td>
    #   <td><b>ITEM NAME</b></td>
    #   <td>QUANTITY</td>
    #   <td>...<input name="price[ID]" value="PRICE"/></td>
    #   <td>...<input name="limit[ID]" value="LIMIT"/></td>
    # </tr>
    for price_inp in soup.find_all("input", {"name": re.compile(r"^price\[\d+\]$")}):
        m = re.search(r'price\[(\d+)\]', price_inp.get("name", ""))
        if not m:
            continue
        item_id = int(m.group(1))
        row = price_inp.find_parent("tr")
        if not row:
            continue

        # Name from the <b> tag in the row
        b_tag = row.find("b")
        name = b_tag.get_text(strip=True) if b_tag else f"item#{item_id}"

        # Quantity from the 3rd <td> (index 2)
        tds = row.find_all("td", recursive=False)
        qty = 0
        if len(tds) > 2:
            try:
                qty = int(tds[2].get_text(strip=True).replace(",", ""))
            except ValueError:
                pass

        # Price from input value
        try:
            price = int(price_inp.get("value", "0").replace(",", ""))
        except ValueError:
            price = 0

        # Limit
        limit_inp = row.find("input", {"name": f"limit[{item_id}]"})
        limit = 0
        if limit_inp:
            try:
                limit = int(limit_inp.get("value", 0) or 0)
            except ValueError:
                pass

        logger.debug(f"  store: item={item_id} name={name!r} qty={qty} price={price} limit={limit}")
        listings.append({"item_id": item_id, "name": name, "quantity": qty,
                          "price": price, "limit": limit})
    logger.debug(f"get_my_store: {len(listings)} listing(s)")
    return listings


def add_to_store(session: KoLSession, item_id: int, quantity: int,
                 price: int, purchase_limit: int = 0, desc_id: str = "",
                 name: str = "") -> bool:
    label = name or f"item#{item_id}"
    _status(f"    Listing {quantity}x {label} in store at {price:,} meat ...")
    resp = session.post("backoffice.php", data={
        "pwd":        session.pwd_hash,
        "action":     "additem",
        "itemid":     item_id,
        "price":      price,
        "quantity":   quantity,
        "limit":      purchase_limit,
        "neveragain": "0",
        "priceok":    "0",
    })
    lower = resp.text.lower()
    ok = "can't stock" not in lower and "can not stock" not in lower and \
         "not an item you can stock" not in lower and "error" not in lower[:500]
    if ok:
        _status(f"    Successfully listed {label}.")
    else:
        _status(f"    WARNING: Listing {label} may have failed — check your store.")
    logger.info(f"add_to_store item#{item_id} qty={quantity} price={price} ok={ok}")
    return ok


def remove_from_store(session: KoLSession, item_id: int, quantity: int) -> bool:
    _status(f"Removing {quantity}x item#{item_id} from store ...")
    resp = session.post("backoffice.php", data={
        "pwd": session.pwd_hash, "action": "removeitem",
        "whichitem": item_id, "qty": quantity,
    })
    ok = "removed" in resp.text.lower() or "store" in resp.text.lower()
    if ok:
        _status(f"  Removed item#{item_id} from store.")
    else:
        _status(f"  WARNING: Removing item#{item_id} may have failed.")
    logger.info(f"remove_from_store item#{item_id} qty={quantity} ok={ok}")
    return ok


def _parse_acquired(html: str) -> int:
    """Return the number of items actually acquired from a KoL purchase response.

    KoL formats:
      single: You acquire an item: <b>Name</b>
      multi:  You acquire some items: <b>Name</b> (N)
    """
    # Multi-item: (N) appears right after the bold item name in an acquire message
    m = re.search(r'acquire[^<]*<b>[^<]+</b>\s*\((\d+)\)', html, re.IGNORECASE)
    if m:
        return int(m.group(1))
    # Single item
    if re.search(r'acquire an item', html, re.IGNORECASE):
        return 1
    # Fallback: acquire mentioned but format unrecognised
    if re.search(r'acquire', html, re.IGNORECASE):
        return 1
    return 0


def buy_from_mall(session: KoLSession, item_id: int, item_name: str,
                  quantity: int, max_price: int) -> bool:
    """Search the mall by name, iterate listings cheapest-first, and buy up to
    quantity units within max_price, skipping stores whose limit is exhausted."""
    _status(f"    Searching mall for '{item_name}' ...")
    listings = _fetch_mall_listings(session, item_name, exact=True)

    if not listings:
        _status(f"    No mall listings found for '{item_name}'.")
        logger.warning(f"buy_from_mall: no listings for '{item_name}' (#{item_id})")
        return False

    affordable_listings = [l for l in listings if l["price"] <= max_price]
    if not affordable_listings:
        cheapest_price = min(l["price"] for l in listings)
        _status(f"    Cheapest price {cheapest_price:,} exceeds max {max_price:,} — skipping.")
        logger.info(f"buy_from_mall: price {cheapest_price} > max {max_price}, skipping")
        return False

    affordable_listings.sort(key=lambda x: x["price"])

    # Fetch meat balance once before iterating
    status = session.get("api.php", params={"what": "status", "for": "MallBot"}).json()
    if "pwd" in status:
        session.pwd_hash = status["pwd"]
    meat = int(status.get("meat", 0))

    remaining = quantity
    any_bought = False

    for listing in affordable_listings:
        if remaining <= 0:
            break
        if meat <= 0:
            _status(f"    Out of meat — stopping.")
            break

        price          = listing["price"]
        store_id       = listing["store_id"]
        search_item_id = listing["search_item_id"]

        # Cap by this listing's per-day purchase limit (0 = unlimited)
        listing_limit = listing["limit"]
        capped = min(remaining, listing_limit) if listing_limit > 0 else remaining

        # Cap by what we can currently afford
        can_afford = min(capped, meat // price)
        if can_afford <= 0:
            _status(f"    Not enough meat for 1x at {price:,} (have {meat:,}) — stopping.")
            break
        if can_afford < capped:
            _status(f"    Only enough meat for {can_afford}x at {price:,} (have {meat:,}).")

        _status(f"    Buying {can_afford}x at {price:,} meat (store {store_id}) ...")
        resp = session.post("mallstore.php", data={
            "pwd":        session.pwd_hash,
            "buying":     1,
            "whichstore": store_id,
            "whichitem":  f"{search_item_id}.{price}",
            "quantity":   can_afford,
        })
        acquired = _parse_acquired(resp.text)
        ok = acquired > 0
        # Log a snippet around "acquire" to help diagnose parse failures
        m = re.search(r'.{0,80}acquire.{0,80}', resp.text, re.IGNORECASE | re.DOTALL)
        logger.debug(f"buy_from_mall acquire snippet: {m.group(0)!r}" if m else "buy_from_mall: no 'acquire' in response")
        logger.info(f"buy_from_mall '{item_name}' store={store_id} qty={can_afford} acquired={acquired} price={price} ok={ok}")

        if ok:
            _status(f"    Bought {acquired}x '{item_name}' at {price:,} meat each.")
            remaining -= acquired
            meat      -= acquired * price
            any_bought = True
        elif listing_limit > 0 and can_afford > 1:
            # Purchase limit may be partially exhausted (e.g. limit raised after prior buys).
            # Retry with qty=1 to buy whatever remains of the daily allowance.
            _status(f"    Full quantity failed — retrying with 1x (partial limit remaining?) ...")
            resp2 = session.post("mallstore.php", data={
                "pwd":        session.pwd_hash,
                "buying":     1,
                "whichstore": store_id,
                "whichitem":  f"{search_item_id}.{price}",
                "quantity":   1,
            })
            acquired2 = _parse_acquired(resp2.text)
            ok2 = acquired2 > 0
            logger.info(f"buy_from_mall '{item_name}' store={store_id} qty=1 (retry) acquired={acquired2} price={price} ok={ok2}")
            if ok2:
                _status(f"    Bought {acquired2}x '{item_name}' at {price:,} meat each.")
                remaining -= acquired2
                meat      -= acquired2 * price
                any_bought = True
            else:
                _status(f"    Store {store_id} limit exhausted — trying next store.")
                logger.warning(f"buy_from_mall: store {store_id} limit exhausted, moving on")
        else:
            _status(f"    Purchase from store {store_id} failed (limit likely reached) — trying next store.")
            logger.warning(f"buy_from_mall: purchase failed for store {store_id}, trying next listing")

    if not any_bought:
        _status(f"    WARNING: Could not buy any '{item_name}' — all listings may be limit-reached.")
    return any_bought


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_config() -> Dict:
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH) as f:
            return json.load(f)
    return {"settings": {}, "price_ranges": {}}


def save_config(cfg: Dict):
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)


# ---------------------------------------------------------------------------
# Menu actions
# ---------------------------------------------------------------------------

def action_list_inventory(session: KoLSession, cache: ItemCache):
    """List all tradeable inventory items sorted by current mall price."""
    print()
    autosell_floor = _ask_int(
        "Skip items with autosell value below (0 = show all, e.g. 100): ", default=0
    ) or 0
    mall_floor = _ask_int(
        "Hide items with mall price below (0 = show all, e.g. 5000): ", default=0
    ) or 0

    _status("Fetching inventory from server ...")
    inv = session.get("api.php", params={"what": "inventory", "for": "MallBot"}).json()
    total_items = len(inv)
    _status(f"Inventory contains {total_items} item type(s). Checking which are tradeable ...")

    rows: List[Tuple] = []
    for i, (id_str, qty_str) in enumerate(inv.items(), 1):
        item_id = int(id_str)
        info = cache.get(item_id)
        if not info["tradeable"]:
            _status(f"  [{i}/{total_items}] {info['name']} — not tradeable, skipping")
            continue
        if autosell_floor and info["autosell"] < autosell_floor:
            _status(f"  [{i}/{total_items}] {info['name']} — autosell {info['autosell']} meat, below floor, skipping")
            continue
        _status(f"  [{i}/{total_items}] {info['name']} — tradeable, checking mall price ...")
        prices = get_mall_price(session, item_id, info["name"])
        min_p  = prices["min_price"]
        min_u  = prices["min_unlimited"]
        if min_p is None:
            _status(f"         → not listed in mall, skipping")
            continue
        if mall_floor and min_p < mall_floor:
            _status(f"         → {min_p:,} meat, below price floor ({mall_floor:,}), skipping")
            continue
        if min_u is None:
            _status(f"         → {min_p:,} meat (all listings have a purchase limit)")
        elif min_u == min_p:
            _status(f"         → {min_p:,} meat (unlimited)")
        else:
            _status(f"         → {min_p:,} meat (limited); cheapest unlimited: {min_u:,} meat")
        rows.append((min_p, min_u, info["name"], item_id, int(qty_str)))

    total = len(rows)
    if total == 0:
        _status("No tradeable items found in inventory matching the filters.")
        return

    rows.sort(key=lambda r: r[0] or 0, reverse=True)

    print()
    _hr()
    print(f"  {'Min Price':>12}  {'Unltd Price':>12}  {'Qty':>5}  {'ID':>8}  Name")
    _hr()
    for min_p, min_u, name, item_id, qty in rows:
        if min_p is None:
            price_str = f"  {'(not listed)':>12}"
            unltd_str = f"  {'':>12}"
        else:
            price_str = f"  {min_p:>12,}"
            if min_u is None:
                unltd_str = f"  {'(none)':>12}"
            elif min_u == min_p:
                unltd_str = f"  {'—':>12}"
            else:
                unltd_str = f"  {min_u:>12,}"
        print(f"{price_str}{unltd_str}  {qty:>5}  {item_id:>8}  {name}")
    _hr()
    print(f"  {total} tradeable item type(s) total.")
    print(f"  Unltd Price: cheapest listing with no per-day purchase limit.")
    print(f"  '—' = cheapest listing is already unlimited.  '(none)' = all listings have a limit.")


def action_stock_mall(session: KoLSession, cache: ItemCache):
    """Stock mall with inventory items whose price meets a threshold."""
    print()
    min_price = _ask_int("Minimum mall price to stock (e.g. 10000): ")
    if min_price is None:
        print("  Invalid price — cancelled.")
        return
    markup_pct = _ask_int("Listing markup % over mall price (default 5): ", default=5)
    max_qty = _ask_int("Max quantity to list per item (default 1): ", default=1)

    markup = (markup_pct or 5) / 100.0
    max_qty = max_qty or 1

    _status("Fetching inventory from server ...")
    inv = session.get("api.php", params={"what": "inventory", "for": "MallBot"}).json()
    total_items = len(inv)
    _status(f"Inventory contains {total_items} item type(s). Checking tradeability ...")

    stocked = 0
    skipped = 0
    for i, (id_str, qty_str) in enumerate(inv.items(), 1):
        item_id = int(id_str)
        info = cache.get(item_id)
        if not info["tradeable"]:
            _status(f"  [{i}/{total_items}] {info['name']} — not tradeable, skipping")
            continue
        _status(f"  [{i}/{total_items}] {info['name']} — tradeable, checking mall price ...")
        prices     = get_mall_price(session, item_id, info["name"])
        mall_price = prices["min_price"]

        if mall_price is None:
            _status(f"         → Not listed in mall, skipping.")
            skipped += 1
            continue
        if mall_price < min_price:
            _status(f"         → {mall_price:,} meat is below threshold ({min_price:,}), skipping.")
            skipped += 1
            continue

        inv_qty    = int(qty_str)
        list_qty   = min(max_qty, inv_qty)
        list_price = max(info["autosell"] + 1, int(mall_price * (1 + markup)))
        _status(f"         → Mall price {mall_price:,}. Listing {list_qty}x at {list_price:,} meat ...")
        add_to_store(session, item_id, list_qty, list_price)
        stocked += 1

    _status(f"Done. Stocked {stocked} item type(s), skipped {skipped}.")


def action_monitor(session: KoLSession, cache: ItemCache):
    """Watch price ranges and auto buy/sell. Runs until Ctrl-C."""
    cfg = load_config()
    ranges = {k: v for k, v in cfg.get("price_ranges", {}).items() if k != "_comment"}

    if not ranges:
        print("\n  No price ranges configured. Use option 4 to add some first.")
        return

    interval = _ask_int("Check interval in seconds (default 300): ", default=300) or 300
    print(f"\n  Monitoring {len(ranges)} item(s) every {interval}s. Press Ctrl-C to stop.\n")
    logger.info(f"Monitor started: {len(ranges)} items, interval={interval}s")

    try:
        while True:
            print(f"  --- {time.strftime('%H:%M:%S')} ---")
            for rule in ranges.values():
                item_id  = int(rule["item_id"])
                name     = rule.get("name", f"item#{item_id}")
                min_p    = rule.get("min_price")
                max_p    = rule.get("max_price")
                buy_qty  = rule.get("buy_qty", 1)

                prices = get_mall_price(session, item_id, name)
                price  = prices["min_price"]
                if price is None:
                    print(f"  {name}: no mall listings.")
                    continue

                status = f"  {name}: {price:,} meat"

                if min_p is not None and price < min_p:
                    print(f"{status}  → below min ({min_p:,}), buying {buy_qty}x ...")
                    buy_from_mall(session, item_id, name, buy_qty, min_p)

                elif max_p is not None and price > max_p:
                    inv = session.get("api.php",
                                      params={"what": "inventory", "for": "MallBot"}).json()
                    have = int(inv.get(str(item_id), 0))
                    if have > 0:
                        print(f"{status}  → above max ({max_p:,}), listing {have}x at {max_p:,} ...")
                        add_to_store(session, item_id, have, max_p)
                    else:
                        print(f"{status}  → above max ({max_p:,}) but none in inventory.")
                else:
                    lo = f"{min_p:,}" if min_p else "—"
                    hi = f"{max_p:,}" if max_p else "—"
                    print(f"{status}  (range {lo} – {hi}, OK)")

            print(f"  Sleeping {interval}s ...\n")
            time.sleep(interval)

    except KeyboardInterrupt:
        print("\n  Monitor stopped.")


def action_set_range(session: KoLSession, cache: ItemCache):
    """Configure a price range for an item."""
    print()
    item_id = _ask_int("Item ID: ")
    if item_id is None:
        print("  Invalid ID — cancelled.")
        return

    _status(f"Looking up item #{item_id} ...")
    info = cache.get(item_id)
    print(f"  Item: {info['name']}  (autosell: {info['autosell']:,} meat)")

    min_price = _ask_int("  Buy when price falls BELOW (leave blank to skip auto-buy): ")
    max_price = _ask_int("  Sell when price rises ABOVE (leave blank to skip auto-sell): ")

    if min_price is None and max_price is None:
        print("  No range set — cancelled.")
        return

    buy_qty = _ask_int("  Quantity to buy per trigger (default 1): ", default=1) or 1

    cfg = load_config()
    cfg.setdefault("price_ranges", {})[str(item_id)] = {
        "item_id":   item_id,
        "name":      info["name"],
        "min_price": min_price,
        "max_price": max_price,
        "buy_qty":   buy_qty,
    }
    save_config(cfg)

    parts = []
    if min_price is not None:
        parts.append(f"buy below {min_price:,}")
    if max_price is not None:
        parts.append(f"sell above {max_price:,}")
    print(f"  Saved: {info['name']} — {', '.join(parts)}.")


def action_show_ranges(session: KoLSession, cache: ItemCache):
    """Display all configured price ranges."""
    cfg = load_config()
    ranges = {k: v for k, v in cfg.get("price_ranges", {}).items() if k != "_comment"}

    if not ranges:
        print("\n  No price ranges configured yet.")
        return

    print()
    _hr()
    print(f"  {'ID':>8}  {'Min Price':>12}  {'Max Price':>12}  {'Buy Qty':>7}  Name")
    _hr()
    for rule in ranges.values():
        min_str = f"{rule['min_price']:>12,}" if rule.get("min_price") else f"{'—':>12}"
        max_str = f"{rule['max_price']:>12,}" if rule.get("max_price") else f"{'—':>12}"
        print(f"  {rule['item_id']:>8}  {min_str}  {max_str}  "
              f"{rule.get('buy_qty', 1):>7}  {rule.get('name', '')}")
    _hr()
    print(f"  {len(ranges)} range(s) configured.")


def action_remove_range(session: KoLSession, cache: ItemCache):
    """Remove a price range by item ID."""
    action_show_ranges(session, cache)
    print()
    item_id = _ask_int("Item ID to remove (or blank to cancel): ")
    if item_id is None:
        return
    cfg = load_config()
    key = str(item_id)
    if key in cfg.get("price_ranges", {}):
        name = cfg["price_ranges"][key].get("name", f"item#{item_id}")
        del cfg["price_ranges"][key]
        save_config(cfg)
        print(f"  Removed range for {name}.")
    else:
        print(f"  No range found for item ID {item_id}.")


def action_view_store(session: KoLSession, cache: ItemCache):
    """Show current mall store listings."""
    _status("Fetching your mall store ...")
    listings = get_my_store(session)
    _status(f"Received {len(listings)} listing(s).")

    if not listings:
        print("  Your mall store is empty (or could not be parsed).")
        return

    listings.sort(key=lambda x: x["price"], reverse=True)
    print()
    _hr()
    print(f"  {'Price':>12}  {'Qty':>5}  {'Limit':>6}  {'ID':>8}  Name")
    _hr()
    for item in listings:
        limit_str = str(item["limit"]) if item["limit"] else "none"
        print(f"  {item['price']:>12,}  {item['quantity']:>5}  "
              f"{limit_str:>6}  {item['item_id']:>8}  {item['name']}")
    _hr()
    print(f"  {len(listings)} listing(s) in your store.")


# ---------------------------------------------------------------------------
# Main menu
# ---------------------------------------------------------------------------

MENU_ITEMS = [
    ("List inventory by mall price",         action_list_inventory),
    ("Stock mall (items above a price)",      action_stock_mall),
    ("Monitor price ranges (auto buy/sell)",  action_monitor),
    ("Set a price range for an item",         action_set_range),
    ("Show configured price ranges",          action_show_ranges),
    ("Remove a price range",                  action_remove_range),
    ("View my mall store",                    action_view_store),
]


def print_menu():
    print()
    _hr()
    print("  KoL Mall Bot — Main Menu")
    _hr()
    for i, (label, _) in enumerate(MENU_ITEMS, 1):
        print(f"  {i}. {label}")
    print("  0. Logout and exit")
    _hr()


def main():
    print()
    _hr("═")
    print("  Kingdom of Loathing — Mall Bot")
    _hr("═")

    # Credentials
    print()
    username = _ask("Username: ")
    password = getpass.getpass("  Password: ")

    if not username or not password:
        print("  Username and password are required.")
        sys.exit(1)

    cfg   = load_config()
    delay = cfg.get("settings", {}).get("request_delay_seconds", 0.5)
    session = KoLSession(username, password, delay=delay)

    print()
    if not session.login():
        sys.exit(1)

    cache = ItemCache(session)
    known = len(cache._cache)
    if known:
        _status(f"Loaded {known} known item(s) from disk cache — skipping those lookups.")

    # Main loop
    try:
        while True:
            print_menu()
            choice = _ask("Choose an option: ")

            if choice == "0":
                break

            try:
                idx = int(choice) - 1
            except ValueError:
                print("  Please enter a number.")
                continue

            if idx < 0 or idx >= len(MENU_ITEMS):
                print("  Invalid choice.")
                continue

            label, action = MENU_ITEMS[idx]
            print(f"\n  ── {label} ──")
            try:
                action(session, cache)
            except requests.RequestException as e:
                print(f"\n  Network error: {e}")
                logger.exception("Network error during action")
            except Exception as e:
                print(f"\n  Unexpected error: {e}")
                logger.exception("Unexpected error during action")

            cache.save()
            _pause()

    except KeyboardInterrupt:
        print("\n\n  Interrupted.")
    finally:
        cache.save()
        session.logout()
        print("  Goodbye!")


if __name__ == "__main__":
    main()
