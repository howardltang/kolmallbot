"""
KoL Mall Bot — Web UI
Run:   python web_mallbot.py
Open:  http://localhost:5000
"""
import os, sys, json, threading, time, traceback
from datetime import datetime
from zoneinfo import ZoneInfo
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests as _requests
from pathlib import Path
from typing import Optional, List, Dict

from flask import (Flask, request, session as fs, Response,
                   redirect, url_for, jsonify, render_template)

sys.path.insert(0, str(Path(__file__).parent))
from mallbot import (
    KoLSession, ItemCache,
    get_mall_price, buy_from_mall, add_to_store, remove_from_store, reprice_in_store, get_my_store,
    load_config, save_config, evaluate_snipe,
)
import mallbot as _mall_mod

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
app = Flask(__name__)
app.secret_key = os.urandom(24)

# ---------------------------------------------------------------------------
# Global state  (single-user bot)
# ---------------------------------------------------------------------------
_session: Optional[KoLSession] = None
_cache:   Optional[ItemCache]  = None
_inv_snapshot:   dict = {}   # last-fetched {item_id_str: qty_str}; avoids blocking on page load
_store_snapshot: list = []  # last-fetched store listings

_job_lock     = threading.Lock()
_job_running  = False

# ---------------------------------------------------------------------------
# Price data recorder — writes snipe-item mall listings each monitor cycle
# ---------------------------------------------------------------------------
_PRICE_DATA_PATH = Path(__file__).parent / "price_data.jsonl"

# Price data collector (disabled — replaced by in-loop recording)
# ---------------------------------------------------------------------------
# _PRICE_DATA_PATH   = Path(__file__).parent / "price_data.jsonl"
# _PRICEGUN_ITEMS    = {194: "Mr. Accessory"}   # item_id -> name; add more as needed
# _COLLECTOR_STOP    = threading.Event()
# _collector_thread  = None
# _COLLECTOR_SESSION = _requests.Session()       # separate session for external requests
#
#
# def _run_collector(mall_interval: int = 60, pricegun_interval: int = 3600):
#     """Background thread: fetches mall listings every `mall_interval` seconds and
#     pricegun trade history every `pricegun_interval` seconds (new trades only)."""
#     import logging as _logging
#     log = _logging.getLogger("mallbot")
#     log.info("Price data collector started.")
#
#     last_pricegun_fetch = 0.0
#     last_seen_trade_ts  = {iid: "" for iid in _PRICEGUN_ITEMS}
#
#     while not _COLLECTOR_STOP.wait(mall_interval):
#         if _session is None:
#             continue
#         now = time.time()
#         ts  = datetime.now().isoformat(timespec="seconds")
#         do_pricegun = (now - last_pricegun_fetch) >= pricegun_interval
#
#         for item_id, name in _PRICEGUN_ITEMS.items():
#             entry = {"ts": ts}
#
#             try:
#                 listings = _mall_mod._fetch_mall_listings(_session, name, exact=True)
#                 entry["mall_listings"] = [
#                     {"price": l["price"], "quantity": l["quantity"],
#                      "store_id": l["store_id"], "limit": l["limit"]}
#                     for l in listings[:10]
#                 ]
#             except Exception as e:
#                 log.warning(f"Collector: mall fetch failed for {name}: {e}")
#
#             if do_pricegun:
#                 try:
#                     resp = _COLLECTOR_SESSION.get(
#                         f"https://pricegun.loathers.net/api/{item_id}", timeout=15)
#                     if resp.ok:
#                         data  = resp.json()
#                         sales = data.get("sales", [])
#                         cutoff = last_seen_trade_ts[item_id]
#                         new_trades = [
#                             {
#                                 "date":      s["date"],
#                                 "unitPrice": float(s["unitPrice"]["__decimal__"])
#                                              if isinstance(s["unitPrice"], dict)
#                                              else s["unitPrice"],
#                                 "quantity":  s["quantity"],
#                             }
#                             for s in sales
#                             if s["date"] > cutoff
#                         ]
#                         if new_trades:
#                             entry["new_trades"] = new_trades
#                             last_seen_trade_ts[item_id] = max(s["date"] for s in new_trades)
#                 except Exception as e:
#                     log.warning(f"Collector: pricegun fetch failed for {name}: {e}")
#
#             try:
#                 with open(_PRICE_DATA_PATH, "a") as f:
#                     f.write(json.dumps({str(item_id): entry}) + "\n")
#             except Exception as e:
#                 log.warning(f"Collector: failed to write price_data.jsonl: {e}")
#
#         if do_pricegun:
#             last_pricegun_fetch = now
#
#     log.info("Price data collector stopped.")
#
#
# def _start_collector():
#     global _collector_thread
#     _COLLECTOR_STOP.clear()
#     _collector_thread = threading.Thread(target=_run_collector, daemon=True, name="price-collector")
#     _collector_thread.start()
#
#
# def _stop_collector():
#     _COLLECTOR_STOP.set()
_job_lines:   List[str] = []
_job_cancel   = threading.Event()  # set to abort any running job
_monitor_verbose = True  # False = only emit successful buy/list messages


def _emit(msg: str):
    _job_lines.append(str(msg))


_QUIET_KEYWORDS = ("Bought ", "Successfully listed", "Monitor stopped", "[ERROR]", "WARNING: Listing")

def _monitor_emit(msg: str):
    """Emit for monitor job — filtered when _monitor_verbose is False."""
    if _monitor_verbose:
        _emit(msg)
    elif any(kw in msg for kw in _QUIET_KEYWORDS):
        _emit(f"[{time.strftime('%H:%M:%S')}] {msg.strip()}")


# Redirect mallbot's terminal _status() calls into the web output stream.
_mall_mod._status = _emit  # type: ignore


def _start_job(fn, *args) -> bool:
    global _job_running, _job_lines
    with _job_lock:
        if _job_running:
            return False
        _job_running = True
        _job_lines   = []
        _job_cancel.clear()

    def _run():
        global _job_running
        try:
            fn(*args)
        except Exception as e:
            _emit(f"[ERROR] {e}")
            _emit(traceback.format_exc())
        finally:
            _job_running = False
            if _cache:
                _cache.save()

    threading.Thread(target=_run, daemon=True).start()
    return True


# ---------------------------------------------------------------------------
# Job implementations  (no input(), output via _emit)
# ---------------------------------------------------------------------------

def _do_fetch_inventory():
    global _inv_snapshot
    _emit("Fetching inventory from KoL...")
    _inv_snapshot = _session.get("api.php", params={"what": "inventory", "for": "MallBot"}).json()
    uncached = [int(id_str) for id_str in _inv_snapshot
                if int(id_str) not in _cache._cache
                or _cache._cache[int(id_str)].get("name", "").startswith("item#")]
    if uncached:
        _emit(f"Fetching names for {len(uncached)} new item(s)...")
        for item_id in uncached:
            _cache.get(item_id)
    _emit(f"Loaded {len(_inv_snapshot)} item type(s).")


def _do_list(refresh_threshold: int, display_floor: int):
    global _inv_snapshot
    _emit("Fetching inventory...")
    inv = _session.get("api.php", params={"what": "inventory", "for": "MallBot"}).json()
    _inv_snapshot = inv
    total = len(inv)
    _emit(f"{total} item type(s). Scanning... (click Stop to abort at any time)")
    rows = []
    for i, (id_str, qty_str) in enumerate(inv.items(), 1):
        if _job_cancel.is_set():
            _emit(f"  Aborted at item {i}/{total}.")
            break
        item_id = int(id_str)
        info    = _cache.get(item_id)
        name    = info["name"]
        if not info["tradeable"]:
            _emit(f"[{i}/{total}] {name} — not tradeable, skipping")
            continue

        cached_entry = _cache._cache.get(item_id, {})
        has_cached   = "last_min_unlimited" in cached_entry
        last_u       = cached_entry.get("last_min_unlimited")  # None or int
        # Use cached price if previously checked AND unlimited price is below threshold
        use_cache    = has_cached and (last_u is None or last_u < refresh_threshold)

        if use_cache:
            min_p = cached_entry.get("last_min_price")
            min_u = last_u
            label = "no unltd listings" if last_u is None else f"unltd {last_u:,}"
            _emit(f"[{i}/{total}] {name} — cached ({label}), skipping network request")
            if min_p is None:
                continue
        else:
            _emit(f"[{i}/{total}] {name} — checking mall price...")
            prices = get_mall_price(_session, item_id, name)
            min_p, min_u = prices["min_price"], prices["min_unlimited"]
            _cache._cache[item_id]["last_min_price"]     = min_p
            _cache._cache[item_id]["last_min_unlimited"] = min_u
            _cache._dirty = True
            if min_p is None:
                _emit(f"  → not listed")
                continue
            if min_u is None:
                _emit(f"  → {min_p:,} (all listings limited)")
            else:
                _emit(f"  → {min_p:,} (ltd) / {min_u:,} (unltd)")

        rows.append((min_p, min_u, name, item_id, int(qty_str)))

    _emit(f"Done. {len(rows)} item(s) scanned. Table updated.")


def _do_stock(min_price: int, markup_pct: int, max_qty: int):
    markup = markup_pct / 100.0
    _emit("Fetching inventory...")
    inv = _session.get("api.php", params={"what": "inventory", "for": "MallBot"}).json()
    total = len(inv)
    stocked = skipped = 0
    for i, (id_str, qty_str) in enumerate(inv.items(), 1):
        item_id = int(id_str)
        info    = _cache.get(item_id)
        name    = info["name"]
        if not info["tradeable"]:
            continue
        _emit(f"[{i}/{total}] {name} — checking mall price...")
        prices = get_mall_price(_session, item_id, name)
        mall_p = prices["min_price"]
        if mall_p is None or mall_p < min_price:
            _emit(f"  → {'not listed' if mall_p is None else f'{mall_p:,}'}, skip")
            skipped += 1
            continue
        inv_qty    = int(qty_str)
        list_qty   = min(max_qty, inv_qty)
        list_price = max(info["autosell"] + 1, int(mall_p * (1 + markup)))
        _emit(f"  → Mall {mall_p:,}. Listing {list_qty}x at {list_price:,} meat...")
        add_to_store(_session, item_id, list_qty, list_price, name=name)
        stocked += 1
    _emit(f"Done. Stocked {stocked}, skipped {skipped}.")


def _do_monitor(interval: int, verbose: bool = True,
                max_workers: int = 5, snipe_threshold: int = 700_000):
    global _monitor_verbose
    _monitor_verbose = verbose
    _mall_mod._status = _monitor_emit  # type: ignore
    cfg = load_config()
    snipe_cfg   = cfg.get("snipe_items", {})
    snipe_items = [v for k, v in snipe_cfg.items()
                   if k != "_comment" and str(k).lstrip("-").isdigit()] if snipe_cfg else None
    try:
        _do_monitor_inner(interval, max_workers,
                          snipe_items=snipe_items, snipe_threshold=snipe_threshold)
    finally:
        _mall_mod._status = _emit  # type: ignore
        _monitor_verbose = True


def _do_monitor_inner(interval: int, max_workers: int = 5,
                      snipe_items: Optional[List[Dict]] = None,
                      snipe_threshold: int = 700_000):
    cfg    = load_config()
    ranges = {k: v for k, v in cfg.get("price_ranges", {}).items()
              if k != "_comment" and str(k).lstrip("-").isdigit()}
    if not ranges:
        _emit("No price ranges configured.")
        return
    own_store_id = str(_session.player_id) if _session.player_id else None

    # Fetch inventory and meat balance once; keep them updated after each transaction.
    _emit("Fetching initial inventory and meat balance...")
    status = _session.get("api.php", params={"what": "status", "for": "MallBot"}).json()
    if "pwd" in status:
        _session.pwd_hash = status["pwd"]
    meat = int(status.get("meat", 0))
    inv_raw = _session.get("api.php", params={"what": "inventory", "for": "MallBot"}).json()
    inventory = {int(k): int(v) for k, v in inv_raw.items()}
    _emit(f"Meat: {meat:,} | {len(inventory)} item type(s) in inventory.")

    _KOL_TZ = ZoneInfo('America/Los_Angeles')

    def _kol_day() -> str:
        """Return a string identifying the current KoL day.
        KoL rollover is at 8:30 PM PST; we reset at 8:32 PM to allow for slight delays.
        Achieved by shifting the clock back 20h32m so midnight of the shifted time
        coincides with 8:32 PM PST."""
        from datetime import timedelta
        return (datetime.now(_KOL_TZ) - timedelta(hours=20, minutes=32)).strftime('%Y-%m-%d')

    purchase_tracker: Dict = {}
    tracker_date = _kol_day()

    _emit(f"Monitoring {len(ranges)} item(s) every {interval}s. Click Stop to end.")
    while not _job_cancel.is_set():
        # Reset purchase tracker after KoL rollover (8:32 PST)
        today = _kol_day()
        if today != tracker_date:
            _emit(f"  Rollover detected ({today}) — purchase tracker reset.")
            purchase_tracker.clear()
            tracker_date = today

        _monitor_emit(f"--- {time.strftime('%H:%M:%S')} ---")
        # Refresh meat balance and inventory each cycle to stay in sync with reality
        try:
            status = _session.get("api.php", params={"what": "status", "for": "MallBot"}).json()
            if "pwd" in status:
                _session.pwd_hash = status["pwd"]
            meat = int(status.get("meat", 0))
            inv_resp = _session.get("api.php", params={"what": "inventory", "for": "MallBot"}).json()
            inventory = {int(k): int(v) for k, v in inv_resp.items()}
        except _requests.exceptions.RequestException:
            pass  # keep using last known values if the refresh fails
        # Phase 1: fetch all listings in parallel — price ranges + snipe items together
        rule_list   = [r for r in ranges.values() if not _job_cancel.is_set()]
        snipe_list  = snipe_items or []

        def _fetch_one(rule):
            name = rule.get("name", f"item#{int(rule['item_id'])}")
            return int(rule["item_id"]), _mall_mod._fetch_mall_listings(_session, name, exact=True)

        # Build combined fetch list: range rules + snipe rules (tagged with _is_snipe)
        all_fetch = list(rule_list) + [dict(si, _is_snipe=True) for si in snipe_list]

        fetched = {}  # item_id -> (listings, error)
        if max_workers > 1:
            saved_delay = _session.delay
            _session.delay = 0
            try:
                with ThreadPoolExecutor(max_workers=max_workers) as executor:
                    future_map = {executor.submit(_fetch_one, rule): rule for rule in all_fetch}
                    for future in as_completed(future_map):
                        try:
                            item_id, listings = future.result()
                            fetched[item_id] = (listings, None)
                        except (_requests.exceptions.Timeout,
                                _requests.exceptions.ConnectionError) as e:
                            rule = future_map[future]
                            fetched[int(rule["item_id"])] = (None, e)
            finally:
                _session.delay = saved_delay
        else:
            for rule in all_fetch:
                if _job_cancel.is_set():
                    break
                try:
                    item_id, listings = _fetch_one(rule)
                    fetched[item_id] = (listings, None)
                except (_requests.exceptions.Timeout,
                        _requests.exceptions.ConnectionError) as e:
                    fetched[int(rule["item_id"])] = (None, e)
                    break

        # Phase 2: evaluate all actions, collect buys sorted by estimated profit, sells last
        pending_buys  = []  # list of (estimated_profit, action_fn) to execute in priority order
        pending_sells = []  # list of action_fn to execute after all buys

        # --- Evaluate price-range rules ---
        for rule in rule_list:
            if _job_cancel.is_set():
                break
            item_id = int(rule["item_id"])
            name    = rule.get("name", f"item#{item_id}")
            min_p   = rule.get("min_price")
            max_p   = rule.get("max_price")
            buy_qty = rule.get("buy_qty", 1)

            listings, err = fetched.get(item_id, (None, None))
            if err is not None:
                if isinstance(err, _requests.exceptions.Timeout):
                    _monitor_emit(f"  {name}: request timed out (server may be in rollover) — skipping this cycle")
                else:
                    _monitor_emit(f"  {name}: connection error ({err.__class__.__name__}) — skipping this cycle")
                continue
            if not listings:
                _monitor_emit(f"  {name}: no listings")
                continue

            others = [l for l in listings if str(l["store_id"]) != own_store_id] if own_store_id else listings
            if not others:
                _monitor_emit(f"  {name}: only own store listed, skipping")
                continue

            purchasable = [l for l in others
                           if l["limit"] == 0 or
                           purchase_tracker.get((l["store_id"], l["search_item_id"]), 0) < l["limit"]]
            buy_price   = min(l["price"] for l in purchasable) if purchasable else None
            unlimited   = [l for l in others if l["limit"] == 0]
            unltd_price = min(l["price"] for l in unlimited) if unlimited else None

            if min_p and buy_price is not None and buy_price <= min_p:
                est_profit = (min_p - buy_price) * buy_qty
                def _range_buy(item_id=item_id, name=name, buy_qty=buy_qty, min_p=min_p,
                               buy_price=buy_price):
                    _monitor_emit(f"  {name}: {buy_price:,} <= min {min_p:,} → buying {buy_qty}x")
                    result = buy_from_mall(_session, item_id, name, buy_qty, min_p,
                                          available_meat=meat,
                                          purchase_tracker=purchase_tracker)
                    return result["acquired"], result["spent"], item_id
                pending_buys.append((est_profit, _range_buy))
            elif max_p and unltd_price is not None and unltd_price >= max_p:
                have = inventory.get(item_id, 0)
                if have:
                    range_undercut = int(rule.get("undercut", 0))
                    list_price     = max(max_p, unltd_price - range_undercut)
                    def _range_sell(item_id=item_id, name=name, have=have,
                                    list_price=list_price, unltd_price=unltd_price, max_p=max_p):
                        _monitor_emit(f"  {name}: unltd {unltd_price:,} >= max {max_p:,} → listing {have}x at {list_price:,}")
                        ok = add_to_store(_session, item_id, have, list_price, name=name)
                        if ok:
                            inventory[item_id] = inventory.get(item_id, 0) - have
                    pending_sells.append(_range_sell)
                else:
                    _monitor_emit(f"  {name}: unltd {unltd_price:,} >= max {max_p:,}, none in inventory")
            else:
                lo = f"{min_p:,}" if min_p else "—"
                hi = f"{max_p:,}" if max_p else "—"
                unltd_disp = f"{unltd_price:,}" if unltd_price is not None else "no unltd listings"
                buy_disp   = f"{buy_price:,}" if buy_price is not None else "none purchasable"
                _monitor_emit(f"  {name}: {buy_disp} (unltd: {unltd_disp})  (OK, range {lo}–{hi})")

        # --- Evaluate snipe items ---
        snipe_actions = {}  # item_id -> action dict (deduplicate if same item in ranges+snipe)
        for si in snipe_list:
            if _job_cancel.is_set():
                break
            s_item_id = int(si["item_id"])
            s_name    = si.get("name", f"item#{s_item_id}")
            listings, err = fetched.get(s_item_id, (None, None))
            if err is not None:
                _monitor_emit(f"  [snipe] {s_name}: network error — skipping")
                continue
            if not listings:
                _monitor_emit(f"  [snipe] {s_name}: no listings")
                continue

            try:
                record = {
                    str(s_item_id): {
                        "ts": datetime.now().isoformat(timespec="seconds"),
                        "mall_listings": [
                            {"price": l["price"], "quantity": l["quantity"],
                             "store_id": l["store_id"], "limit": l["limit"]}
                            for l in listings[:10]
                        ],
                    }
                }
                with open(_PRICE_DATA_PATH, "a") as _f:
                    _f.write(json.dumps(record) + "\n")
            except Exception as _e:
                _monitor_emit(f"  [price_data] write failed: {_e}")

            action = evaluate_snipe(listings,
                                    snipe_threshold=int(si.get("snipe_threshold", snipe_threshold)),
                                    undercut=int(si.get("undercut", 1000)))
            if action is None:
                _monitor_emit(f"  [snipe] {s_name}: no opportunity")
                continue

            _monitor_emit(f"  [snipe] {s_name}: {action['reason']}")
            relist = action["relist_at"]
            buy_listings = action["buy"]
            # Estimate profit: (relist - avg_buy_price) * total_qty
            total_qty  = sum(l["quantity"] or 1 for l in buy_listings
                             if not (own_store_id and str(l["store_id"]) == own_store_id))
            avg_cost   = (sum(l["price"] * (l["quantity"] or 1) for l in buy_listings
                              if not (own_store_id and str(l["store_id"]) == own_store_id))
                          / total_qty) if total_qty > 0 else 0
            est_profit = int((relist - avg_cost) * total_qty)

            def _snipe_buy(s_item_id=s_item_id, s_name=s_name, buy_listings=buy_listings, relist=relist):
                total_acquired = 0
                total_spent    = 0
                cur_meat       = meat  # snapshot; dispatch loop will update meat after
                for listing in buy_listings:
                    if own_store_id and str(listing["store_id"]) == own_store_id:
                        own_qty = listing["quantity"] if listing["quantity"] > 0 else 1
                        _monitor_emit(f"  [snipe] {s_name}: own listing at {listing['price']:,} → repricing {own_qty}x to {relist:,}")
                        ok = remove_from_store(_session, s_item_id, own_qty)
                        if ok:
                            inventory[s_item_id] = inventory.get(s_item_id, 0) + own_qty
                            add_to_store(_session, s_item_id, own_qty, relist, name=s_name)
                            inventory[s_item_id] = inventory.get(s_item_id, 0) - own_qty
                        continue
                    if cur_meat <= 0:
                        _monitor_emit(f"  [snipe] out of meat, stopping")
                        break
                    can_buy = listing["quantity"] if listing["quantity"] > 0 else 1
                    can_buy = min(can_buy, cur_meat // listing["price"]) if listing["price"] > 0 else can_buy
                    if can_buy <= 0:
                        _monitor_emit(f"  [snipe] can't afford {listing['price']:,} — skipping store")
                        continue
                    result = buy_from_mall(_session, s_item_id, s_name, can_buy,
                                           listing["price"],
                                           available_meat=cur_meat,
                                           purchase_tracker=purchase_tracker)
                    acq, spent = result["acquired"], result["spent"]
                    total_acquired += acq
                    total_spent    += spent
                    cur_meat       -= spent
                    inventory[s_item_id] = inventory.get(s_item_id, 0) + acq
                if total_acquired > 0:
                    _monitor_emit(f"  [snipe] {s_name}: bought {total_acquired}x, relisting at {relist:,}")
                    ok = add_to_store(_session, s_item_id, total_acquired, relist, name=s_name)
                    if ok:
                        inventory[s_item_id] = inventory.get(s_item_id, 0) - total_acquired
                # inventory already updated inside; return spent so dispatch can update meat
                return 0, total_spent, s_item_id

            pending_buys.append((est_profit, _snipe_buy))

        # Phase 3: execute buys highest-profit first, then sells
        pending_buys.sort(key=lambda x: x[0], reverse=True)
        for est_profit, action_fn in pending_buys:
            if _job_cancel.is_set():
                break
            acquired, spent, item_id = action_fn()
            meat -= spent
            if acquired:  # only range buys return non-zero acquired (snipe handles inventory internally)
                inventory[item_id] = inventory.get(item_id, 0) + acquired

        for sell_fn in pending_sells:
            if _job_cancel.is_set():
                break
            sell_fn()

        _monitor_emit(f"Sleeping {interval}s...")
        _job_cancel.wait(interval)
    _emit("Monitor stopped.")


def _do_view_store():
    global _store_snapshot
    _emit("Fetching your store...")
    listings = get_my_store(_session)
    if not listings:
        _emit("Store is empty or could not be parsed.")
        _store_snapshot = []
        return
    listings.sort(key=lambda x: x["price"], reverse=True)
    _store_snapshot = listings
    _emit(f"Done. {len(listings)} listing(s) loaded.")


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    if not fs.get("logged_in"):
        return redirect(url_for("login"))
    return render_template("index.html")


@app.route("/api/state")
def api_state():
    if not fs.get("logged_in"):
        return jsonify({"error": "not logged in"}), 401
    cfg    = load_config()
    ranges = [v for k, v in cfg.get("price_ranges", {}).items() if k != "_comment"]
    snipes = [v for k, v in cfg.get("snipe_items",  {}).items() if k != "_comment"]
    return jsonify({
        "username":   fs.get("username", ""),
        "cache_size": len(_cache._cache) if _cache else 0,
        "ranges":     ranges,
        "snipes":     snipes,
    })


@app.route("/api/inventory")
def api_inventory():
    if not fs.get("logged_in"):
        return jsonify({"error": "not logged in"}), 401
    if not _inv_snapshot:
        return jsonify({"items": [], "loaded": False})
    items = []
    for id_str, qty_str in _inv_snapshot.items():
        item_id = int(id_str)
        entry = _cache._cache.get(item_id, {})
        name = entry.get("name", f"item#{item_id}")
        if not entry.get("tradeable", True):
            continue
        items.append({
            "item_id":       item_id,
            "name":          name,
            "qty":           int(qty_str),
            "min_price":     entry.get("last_min_price"),
            "min_unlimited": entry.get("last_min_unlimited"),
            "has_cached":    "last_min_unlimited" in entry,
        })
    return jsonify({"items": items, "loaded": True})


@app.route("/api/mall_price")
def api_mall_price():
    if not fs.get("logged_in"):
        return jsonify({"error": "not logged in"}), 401
    item_name = request.args.get("name", "").strip()
    if not item_name:
        return jsonify({"error": "missing name"}), 400
    mall      = _mall_mod._fetch_mall_listings(_session, item_name, exact=True)
    unlimited = sorted((l["price"], l["quantity"]) for l in mall if l["limit"] == 0)
    limited   = sorted(l["price"] for l in mall if l["limit"] > 0)
    return jsonify({
        "mall_unlimited": [{"price": p, "qty": q} for p, q in unlimited[:3]],
        "mall_limited":   limited[0] if limited else None,
    })


@app.route("/api/store")
def api_store():
    if not fs.get("logged_in"):
        return jsonify({"error": "not logged in"}), 401
    return jsonify({"listings": _store_snapshot, "loaded": True})


@app.route("/run/reprice", methods=["POST"])
def run_reprice():
    if not fs.get("logged_in"):
        return jsonify({"error": "not logged in"}), 401
    item_id   = request.form.get("item_id", "").strip()
    new_price = request.form.get("new_price", "").strip()
    limit     = request.form.get("limit", "0").strip()
    name      = request.form.get("name", "").strip()
    if not item_id or not new_price:
        return jsonify({"error": "item_id and new_price are required"}), 400
    try:
        item_id   = int(item_id)
        new_price = int(new_price)
        limit     = int(limit) if limit else 0
    except ValueError:
        return jsonify({"error": "invalid values"}), 400
    ok = reprice_in_store(_session, item_id, new_price, purchase_limit=limit, name=name)
    # Update snapshot so the table reflects the change without a full refresh
    for listing in _store_snapshot:
        if listing.get("item_id") == item_id:
            listing["price"] = new_price
            break
    if ok:
        return jsonify({"ok": True, "message": f"Repriced to {new_price:,} meat."})
    return jsonify({"error": "Reprice may have failed — check your store."}), 500


@app.route("/login", methods=["GET", "POST"])
def login():
    global _session, _cache
    error = None
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "").strip()
        cfg   = load_config()
        delay = cfg.get("settings", {}).get("request_delay_seconds", 0.5)
        sess  = KoLSession(username, password, delay=delay)
        if sess.login():
            _session = sess
            _cache   = ItemCache(_session)
            fs["logged_in"] = True
            fs["username"]  = username
            # _start_collector()  # price data collector disabled
            return redirect(url_for("index"))
        error = "Login failed — check your username and password."
    return render_template("login.html", error=error)


@app.route("/logout", methods=["POST"])
def logout():
    global _session, _cache
    # _stop_collector()  # price data collector disabled
    if _session:
        _session.logout()
        _session = None
    if _cache:
        _cache.save()
        _cache = None
    fs.clear()
    return redirect(url_for("login"))


@app.route("/run/<action>", methods=["POST"])
def run_action(action):
    if not fs.get("logged_in"):
        return jsonify({"error": "not logged in"}), 401

    # Instant actions (no background job)
    if action in ("stop_monitor", "cancel"):
        _job_cancel.set()
        return jsonify({"ok": True})

    if action == "set_range":
        id_or_name = request.form.get("item_id_or_name", "").strip()
        if not id_or_name:
            return jsonify({"error": "Item ID or name is required."}), 400
        if id_or_name.isdigit():
            item_id = int(id_or_name)
        else:
            # Search local cache first (no network cost); match ignoring punctuation
            import re as _re
            name_lower = id_or_name.lower()
            name_norm  = _re.sub(r"[^\w\s]", "", name_lower)
            item_id = next(
                (iid for iid, info in _cache._cache.items()
                 if info.get("name", "").lower() == name_lower
                 or _re.sub(r"[^\w\s]", "", info.get("name", "").lower()) == name_norm),
                None
            )
            if item_id is None:
                # Fall back to mall search
                listings = _mall_mod._fetch_mall_listings(_session, id_or_name)
                if not listings:
                    return jsonify({"error": f"Item \"{id_or_name}\" not found in the mall. "
                                             f"If the item has no current listings, use its item ID instead."}), 404
                # Collect distinct item IDs from results (preserving order), keep name
                seen = {}
                for l in listings:
                    iid = int(l["search_item_id"])
                    if iid not in seen:
                        seen[iid] = l.get("item_name", "")
                # Seed cache with names from mall HTML so direct ID entry works later
                for uid, mall_name in seen.items():
                    if mall_name and not mall_name.startswith("item#"):
                        entry = _cache._cache.setdefault(uid, {"tradeable": True, "autosell": 0})
                        if not entry.get("name") or entry["name"].startswith("item#"):
                            entry["name"] = mall_name
                            _cache._dirty = True
                if len(seen) > 1:
                    choices = []
                    for uid in seen:
                        name = seen[uid] or _cache._cache.get(uid, {}).get("name", f"item#{uid}")
                        choices.append({"item_id": uid, "name": name})
                    return jsonify({"choices": choices})
                item_id  = int(listings[0]["search_item_id"])
                min_p    = min(l["price"] for l in listings)
                unltd    = [l["price"] for l in listings if l["limit"] == 0]
                min_u    = min(unltd) if unltd else None
                entry    = _cache._cache.setdefault(item_id, {"tradeable": True, "autosell": 0})
                entry["last_min_price"]     = min_p
                entry["last_min_unlimited"] = min_u
                _cache._dirty = True
        min_p   = request.form.get("min_price", "").strip() or None
        max_p   = request.form.get("max_price", "").strip() or None
        buy_qty    = int(request.form.get("buy_qty", 1) or 1)
        r_undercut = int(request.form.get("undercut", 0) or 0)
        cached_name = _cache._cache.get(item_id, {}).get("name", "")
        if cached_name and not cached_name.startswith("item#"):
            info = _cache._cache[item_id]  # name already known, skip API
        else:
            # Before hitting the API, check if the config already has a good name
            existing_name = load_config().get("price_ranges", {}).get(str(item_id), {}).get("name", "")
            if existing_name and not existing_name.startswith("item#"):
                entry = _cache._cache.setdefault(item_id, {"tradeable": True, "autosell": 0, "descid": ""})
                entry["name"] = existing_name
                _cache._dirty = True
                info = entry
            else:
                _cache._cache.pop(item_id, None)
                info = _cache.get(item_id)   # try item API
        _cache.save()
        cfg = load_config()
        cfg.setdefault("price_ranges", {})[str(item_id)] = {
            "item_id":   item_id,   "name":      info["name"],
            "min_price": int(min_p) if min_p else None,
            "max_price": int(max_p) if max_p else None,
            "buy_qty":   buy_qty,
            "undercut":  r_undercut,
        }
        save_config(cfg)
        return jsonify({"ok": True, "message": f"Saved range for \"{info['name']}\"."})

    if action == "remove_range":
        item_id = str(request.form.get("item_id", ""))
        cfg    = load_config()
        ranges = cfg.get("price_ranges", {})
        # Key is normally the item_id string, but template entries use a word key
        key = item_id if item_id in ranges else next(
            (k for k, v in ranges.items() if str(v.get("item_id", "")) == item_id),
            None
        )
        if key:
            name = ranges[key].get("name", f"item#{item_id}")
            del ranges[key]
            save_config(cfg)
            return jsonify({"ok": True, "message": f"Removed range for \"{name}\"."})
        return jsonify({"error": "Range not found."}), 404

    if action == "set_snipe":
        id_or_name = request.form.get("item_id_or_name", "").strip()
        threshold  = int(float(request.form.get("snipe_threshold", 700_000) or 700_000))
        s_undercut = int(float(request.form.get("undercut", 1000) or 1000))
        if not id_or_name:
            return jsonify({"error": "Item ID or name is required."}), 400
        cache_data = _cache._cache if _cache is not None else {}
        if id_or_name.isdigit():
            item_id = int(id_or_name)
            name    = cache_data.get(item_id, {}).get("name", f"item#{item_id}")
        else:
            import re as _re
            name_lower = id_or_name.lower()
            name_norm  = _re.sub(r"[^\w\s]", "", name_lower)
            item_id = next(
                (iid for iid, info in cache_data.items()
                 if info.get("name", "").lower() == name_lower
                 or _re.sub(r"[^\w\s]", "", info.get("name", "").lower()) == name_norm),
                None
            )
            if item_id is None:
                if _session is None:
                    return jsonify({"error": "Not logged in — cannot search mall by name."}), 400
                listings = _mall_mod._fetch_mall_listings(_session, id_or_name)
                if not listings:
                    return jsonify({"error": f"Item \"{id_or_name}\" not found in the mall."}), 404
                item_id = int(listings[0]["search_item_id"])
                name    = listings[0].get("item_name") or id_or_name
            else:
                name = cache_data[item_id].get("name", id_or_name)
        cfg = load_config()
        cfg.setdefault("snipe_items", {})[str(item_id)] = {
            "item_id":         item_id,
            "name":            name,
            "snipe_threshold": threshold,
            "undercut":        s_undercut,
        }
        save_config(cfg)
        return jsonify({"ok": True, "message": f"Snipe item set: \"{name}\" (threshold {threshold:,}, undercut {s_undercut:,})"})

    if action == "rm_snipe":
        item_id = request.form.get("item_id", "").strip()
        cfg = load_config()
        items = cfg.get("snipe_items", {})
        if str(item_id) in items:
            name = items[str(item_id)].get("name", f"item#{item_id}")
            del items[str(item_id)]
            save_config(cfg)
            return jsonify({"ok": True, "message": f"Removed snipe item \"{name}\"."})
        return jsonify({"error": "Snipe item not found."}), 404

    # Background job actions
    if _job_running:
        return jsonify({"error": "A job is already running. Please wait."}), 409

    if action == "refresh_inventory":
        _start_job(_do_fetch_inventory)
    elif action == "list":
        _start_job(_do_list,
            int(request.form.get("refresh_threshold", 2000) or 2000),
            0,  # display_floor is now client-side
        )
    elif action == "stock":
        _start_job(_do_stock,
            int(request.form.get("min_price", 0) or 0),
            int(request.form.get("markup_pct", 5) or 5),
            int(request.form.get("max_qty", 1) or 1),
        )
    elif action == "monitor":
        _start_job(_do_monitor,
                   int(request.form.get("interval", 60) or 60),
                   request.form.get("verbose", "1") != "0",
                   max(1, int(request.form.get("max_workers", 5) or 5)),
                   int(float(request.form.get("snipe_threshold", 700_000) or 700_000)))
    elif action == "view_store":
        _start_job(_do_view_store)
    else:
        return jsonify({"error": "Unknown action."}), 400

    return jsonify({"ok": True})


@app.route("/lines")
def get_lines():
    if not fs.get("logged_in"):
        return jsonify({"error": "not logged in"}), 401
    start = int(request.args.get("from", 0))
    return jsonify({
        "lines":   _job_lines[start:],
        "total":   len(_job_lines),
        "running": _job_running,
    })


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8080, debug=False)

