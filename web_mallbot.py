"""
KoL Mall Bot — Web UI
Run:   python web_mallbot.py
Open:  http://localhost:5000
"""
import os, sys, json, threading, time, traceback
from pathlib import Path
from typing import Optional, List

from flask import (Flask, request, session as fs, Response,
                   redirect, url_for, jsonify, render_template_string)

sys.path.insert(0, str(Path(__file__).parent))
from mallbot import (
    KoLSession, ItemCache,
    get_mall_price, buy_from_mall, add_to_store, get_my_store,
    load_config, save_config,
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
_job_lines:   List[str] = []
_job_cancel   = threading.Event()  # set to abort any running job


def _emit(msg: str):
    _job_lines.append(str(msg))


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


def _do_monitor(interval: int, undercut_pct: float = 0):
    cfg    = load_config()
    ranges = {k: v for k, v in cfg.get("price_ranges", {}).items()
              if k != "_comment" and str(k).lstrip("-").isdigit()}
    if not ranges:
        _emit("No price ranges configured.")
        return
    own_store_id = str(_session.player_id) if _session.player_id else None
    undercut_label = f" (undercut {undercut_pct:g}%)" if undercut_pct else ""
    _emit(f"Monitoring {len(ranges)} item(s) every {interval}s{undercut_label}. Click Stop to end.")
    while not _job_cancel.is_set():
        _emit(f"--- {time.strftime('%H:%M:%S')} ---")
        for rule in ranges.values():
            item_id = int(rule["item_id"])
            name    = rule.get("name", f"item#{item_id}")
            min_p   = rule.get("min_price")
            max_p   = rule.get("max_price")
            buy_qty = rule.get("buy_qty", 1)

            listings = _mall_mod._fetch_mall_listings(_session, name, exact=True)
            if not listings:
                _emit(f"  {name}: no listings")
                continue

            # Exclude own store listings so our own price doesn't influence comparisons
            others = [l for l in listings if str(l["store_id"]) != own_store_id] if own_store_id else listings
            if not others:
                _emit(f"  {name}: only own store listed, skipping")
                continue
            price = min(l["price"] for l in others)

            if min_p and price < min_p:
                _emit(f"  {name}: {price:,} < min {min_p:,} → buying {buy_qty}x")
                buy_from_mall(_session, item_id, name, buy_qty, min_p)
            elif max_p and price > max_p:
                inv  = _session.get("api.php", params={"what": "inventory", "for": "MallBot"}).json()
                have = int(inv.get(str(item_id), 0))
                if have:
                    undercut_price = int(price * (1 - undercut_pct / 100))
                    list_price     = max(max_p, undercut_price)
                    _emit(f"  {name}: {price:,} > max {max_p:,} → listing {have}x at {list_price:,}")
                    add_to_store(_session, item_id, have, list_price, name=name)
                else:
                    _emit(f"  {name}: {price:,} > max {max_p:,}, none in inventory")
            else:
                lo = f"{min_p:,}" if min_p else "—"
                hi = f"{max_p:,}" if max_p else "—"
                _emit(f"  {name}: {price:,} meat  (OK, range {lo}–{hi})")
        _emit(f"Sleeping {interval}s...")
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
    return Response(MAIN_HTML, mimetype="text/html")


@app.route("/api/state")
def api_state():
    if not fs.get("logged_in"):
        return jsonify({"error": "not logged in"}), 401
    cfg    = load_config()
    ranges = [v for k, v in cfg.get("price_ranges", {}).items() if k != "_comment"]
    return jsonify({
        "username":   fs.get("username", ""),
        "cache_size": len(_cache._cache) if _cache else 0,
        "ranges":     ranges,
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
    unlimited = sorted(l["price"] for l in mall if l["limit"] == 0)
    limited   = sorted(l["price"] for l in mall if l["limit"] > 0)
    return jsonify({
        "mall_unlimited": unlimited[:3],
        "mall_limited":   limited[0] if limited else None,
    })


@app.route("/api/store")
def api_store():
    if not fs.get("logged_in"):
        return jsonify({"error": "not logged in"}), 401
    return jsonify({"listings": _store_snapshot, "loaded": True})


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
            return redirect(url_for("index"))
        error = "Login failed — check your username and password."
    return render_template_string(LOGIN_TMPL, error=error)


@app.route("/logout", methods=["POST"])
def logout():
    global _session, _cache
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
        buy_qty = int(request.form.get("buy_qty", 1) or 1)
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
                   int(request.form.get("interval", 5) or 5),
                   float(request.form.get("undercut_pct", 0) or 0))
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


# ---------------------------------------------------------------------------
# HTML templates
# ---------------------------------------------------------------------------

LOGIN_TMPL = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>KoL Mall Bot — Login</title>
  <style>
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
    body { background: #0d1117; color: #c9d1d9; font-family: system-ui, sans-serif;
           min-height: 100vh; display: flex; align-items: center; justify-content: center; }
    .card { background: #161b22; border: 1px solid #30363d; border-radius: 10px;
            padding: 2rem; width: 360px; }
    h3 { color: #58a6ff; text-align: center; margin-bottom: 1.5rem; font-size: 1.3rem; }
    label { display: block; font-size: .8rem; color: #8b949e; margin-bottom: .3rem; }
    input { width: 100%; padding: .5rem .7rem; background: #0d1117; border: 1px solid #30363d;
            border-radius: 5px; color: #c9d1d9; font-size: .9rem; outline: none; }
    input:focus { border-color: #58a6ff; }
    .field { margin-bottom: 1rem; }
    button { width: 100%; padding: .6rem; background: #238636; border: none; border-radius: 5px;
             color: #fff; font-size: .95rem; cursor: pointer; margin-top: .5rem; }
    button:hover { background: #2ea043; }
    .error { background: #3d1a1a; color: #f0a0a0; border: 1px solid #7a2020;
             border-radius: 5px; padding: .6rem .8rem; font-size: .85rem; margin-bottom: 1rem; }
  </style>
</head>
<body>
<div class="card">
  <h3>⚔️ KoL Mall Bot</h3>
  {% if error %}<div class="error">{{ error }}</div>{% endif %}
  <form method="POST" action="/login">
    <div class="field"><label>Username</label><input type="text" name="username" autofocus></div>
    <div class="field"><label>Password</label><input type="password" name="password"></div>
    <button type="submit">Log In</button>
  </form>
</div>
</body>
</html>"""


MAIN_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>KoL Mall Bot</title>
  <style>
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
    body { background: #0d1117; color: #c9d1d9; font-family: system-ui, sans-serif;
           height: 100vh; display: flex; flex-direction: column; overflow: hidden; }
    .topbar { background: #161b22; border-bottom: 1px solid #30363d;
              padding: .5rem 1rem; display: flex; align-items: center;
              justify-content: space-between; flex-shrink: 0; }
    .topbar-title { color: #58a6ff; font-weight: 600; font-size: 1rem; }
    .topbar-right { display: flex; align-items: center; gap: 1rem; font-size: .8rem; color: #8b949e; }
    .btn-logout { padding: .25rem .7rem; background: transparent; border: 1px solid #30363d;
                  border-radius: 4px; color: #8b949e; cursor: pointer; font-size: .8rem; }
    .btn-logout:hover { border-color: #8b949e; color: #c9d1d9; }
    .body { display: flex; flex: 1; overflow: hidden; }
    .sidebar { width: 185px; background: #161b22; border-right: 1px solid #30363d;
               padding: .75rem .6rem; overflow-y: auto; flex-shrink: 0; }
    .nav-section { font-size: .7rem; color: #484f58; text-transform: uppercase;
                   letter-spacing: .06em; padding: .6rem .4rem .2rem; }
    .nav-btn { display: block; width: 100%; text-align: left; background: transparent;
               border: none; color: #8b949e; padding: .35rem .6rem; border-radius: 4px;
               cursor: pointer; font-size: .82rem; margin-bottom: 1px; }
    .nav-btn:hover { background: #21262d; color: #c9d1d9; }
    .nav-btn.active { background: #21262d; color: #c9d1d9; }
    .main { flex: 1; display: flex; flex-direction: column; overflow: hidden;
            padding: .75rem; gap: .6rem; }
    .form-panel { background: #161b22; border: 1px solid #30363d; border-radius: 6px;
                  padding: .85rem 1rem; flex-shrink: 0; }
    .form-panel h6 { color: #e6edf3; font-size: .875rem; margin-bottom: .75rem; }
    .form-row { display: flex; flex-wrap: wrap; gap: .6rem; align-items: flex-end; }
    .field { display: flex; flex-direction: column; gap: .25rem; }
    .field label { font-size: .75rem; color: #8b949e; }
    .field input { padding: .35rem .5rem; background: #0d1117; border: 1px solid #30363d;
                   border-radius: 4px; color: #c9d1d9; font-size: .83rem;
                   width: 110px; outline: none; }
    .field input:focus { border-color: #58a6ff; }
    .field input.narrow { width: 70px; }
    .btn-run { padding: .35rem .9rem; background: #238636; border: none; border-radius: 4px;
               color: #fff; font-size: .83rem; cursor: pointer; }
    .btn-run:hover { background: #2ea043; }
    .btn-stop { padding: .35rem .9rem; background: #7a1f1f; border: none; border-radius: 4px;
                color: #fca5a5; font-size: .83rem; cursor: pointer; }
    .btn-stop:hover { background: #991b1b; }
    .btn-danger { padding: .35rem .9rem; background: #7a1f1f; border: none;
                  border-radius: 4px; color: #fca5a5; font-size: .83rem; cursor: pointer; }
    .btn-danger:hover { background: #991b1b; }
    .hidden { display: none !important; }
    /* Monitor + Ranges combined panel */
    #panel-monitor { flex: 1; min-height: 0; padding: 0; border: none; background: transparent; }
    #panel-monitor:not(.hidden) { display: flex; }
    .monitor-layout { flex: 1; display: flex; gap: .6rem; min-height: 0; }
    .monitor-left { flex: 1; display: flex; flex-direction: column; gap: .6rem; min-height: 0; }
    .monitor-ranges-box { flex: 1; min-height: 0; display: flex; flex-direction: column; overflow: hidden; }
    .ranges-table-wrap { flex: 1; overflow-y: auto; min-height: 0; margin-top: .6rem; }
    .monitor-right { flex: 1; min-width: 200px; display: flex; flex-direction: column; min-height: 0; }
    #monitor-output-box { flex: 1; overflow-y: auto; padding: .6rem .75rem; font-family: monospace;
                          font-size: .78rem; line-height: 1.5; white-space: pre; color: #c9d1d9; }
    /* List panel two-column layout */
    #panel-list { flex: 1; min-height: 0; padding: 0; border: none; background: transparent; }
    #panel-list:not(.hidden) { display: flex; }
    .list-layout { flex: 1; display: flex; gap: .6rem; min-height: 0; }
    .list-left { flex: 1; display: flex; flex-direction: column; min-height: 0;
                 background: #161b22; border: 1px solid #30363d; border-radius: 6px; overflow: hidden; }
    .list-controls { display: flex; align-items: flex-end; gap: .6rem; flex-shrink: 0;
                     padding: .6rem 1rem; border-bottom: 1px solid #30363d; }
    .list-table-wrap { flex: 1; overflow-y: auto; min-height: 0; }
    .list-table { width: 100%; border-collapse: collapse; font-size: .8rem; }
    .list-table thead th { position: sticky; top: 0; background: #1c2128; z-index: 1;
                           color: #8b949e; font-weight: normal; padding: .3rem .5rem;
                           border-bottom: 1px solid #30363d; white-space: nowrap; }
    .list-table td { padding: .25rem .5rem; border-bottom: 1px solid #21262d; color: #c9d1d9; }
    .list-refresh { flex-shrink: 0; border-top: 1px solid #30363d; padding: .5rem 1rem; }
    .list-right { flex: 1; min-width: 200px; display: flex; flex-direction: column; min-height: 0; }
    #panel-view_store { flex: 1; min-height: 0; display: flex; flex-direction: column; }
    .store-table-wrap { flex: 1; overflow-y: auto; min-height: 0; margin-top: .6rem; }
    #list-output-box { flex: 1; overflow-y: auto; padding: .6rem .75rem; font-family: monospace;
                       font-size: .78rem; line-height: 1.5; white-space: pre; color: #c9d1d9; }
    .msg { padding: .4rem .7rem; border-radius: 4px; font-size: .82rem; margin-bottom: .5rem; }
    .msg-ok   { background: #0c2d1a; color: #4ade80; border: 1px solid #0c6b3a; }
    .msg-err  { background: #2d0c0c; color: #f87171; border: 1px solid #6b1010; }
    .msg-warn { background: #2d200c; color: #fbbf24; border: 1px solid #6b4a10; }
    .rtable { width: 100%; font-size: .8rem; border-collapse: collapse; }
    .rtable th { color: #8b949e; font-weight: normal; text-align: left;
                 padding: .2rem .5rem; border-bottom: 1px solid #30363d; }
    .rtable td { padding: .2rem .5rem; border-bottom: 1px solid #21262d; }
    .rtable td.editable { cursor: pointer; }
    .rtable td.editable:hover { background: #21262d; }
    .rtable td.editable input { width: 80px; background: #0d1117; color: #c9d1d9;
      border: 1px solid #388bfd; border-radius: 3px; padding: .1rem .3rem;
      font-size: .8rem; outline: none; }
    .output-wrap { flex: 1; display: flex; flex-direction: column; overflow: hidden;
                   background: #0d1117; border: 1px solid #30363d; border-radius: 6px;
                   min-height: 0; }
    .output-bar { background: #161b22; border-bottom: 1px solid #30363d;
                  padding: .35rem .75rem; display: flex; align-items: center;
                  justify-content: space-between; flex-shrink: 0; }
    .output-bar span { font-size: .78rem; color: #8b949e; }
    .badge { font-size: .7rem; padding: .15em .5em; border-radius: 3px; }
    .badge-idle    { background: #1f2937; color: #6b7280; }
    .badge-running { background: #0c2d1a; color: #4ade80; }
    .btn-clear { padding: .15rem .5rem; background: transparent; border: 1px solid #30363d;
                 border-radius: 3px; color: #6b7280; font-size: .72rem; cursor: pointer; }
    .btn-clear:hover { border-color: #8b949e; color: #c9d1d9; }
    #output-box { flex: 1; overflow-y: auto; padding: .6rem .75rem; font-family: monospace;
                  font-size: .78rem; line-height: 1.5; white-space: pre; color: #c9d1d9; }
  </style>
</head>
<body>

<div class="topbar">
  <span class="topbar-title">KoL Mall Bot</span>
  <div class="topbar-right">
    <span id="user-info">loading...</span>
    <form method="POST" action="/logout" style="margin:0">
      <button type="submit" class="btn-logout">Logout</button>
    </form>
  </div>
</div>

<div class="body">
  <nav class="sidebar">
    <div class="nav-section">Inventory</div>
    <button class="nav-btn" data-panel="list">List by Price</button>
    <button class="nav-btn" data-panel="stock">Stock Mall</button>
    <div class="nav-section">Monitor</div>
    <button class="nav-btn" data-panel="monitor">Auto Monitor</button>
    <div class="nav-section">Store</div>
    <button class="nav-btn" data-panel="view_store">View My Store</button>
  </nav>

  <div class="main">

    <div class="form-panel hidden" id="panel-list">
      <div class="list-layout">
        <div class="list-left">
          <div class="list-controls">
            <div class="field"><label>Display floor (meat)</label>
              <input type="number" id="list-display-floor" value="0" min="0"></div>
            <button id="btn-apply-floor" class="btn-run">Apply</button>
            <span id="list-count" style="font-size:.78rem;color:#8b949e;align-self:center"></span>
          </div>
          <div class="list-table-wrap">
            <table class="list-table">
              <thead><tr>
                <th style="text-align:left">ID</th>
                <th style="text-align:left">Name</th>
                <th style="text-align:right">Qty</th>
                <th style="text-align:right">Min Price</th>
                <th style="text-align:right">Unltd Price</th>
                <th style="text-align:right">Total Value</th>
              </tr></thead>
              <tbody id="list-table-body">
                <tr><td colspan="6" style="color:#8b949e;text-align:center;padding:.75rem">Loading...</td></tr>
              </tbody>
            </table>
          </div>
          <div class="list-refresh">
            <div style="display:flex;align-items:flex-end;gap:2rem">
              <div>
                <button id="btn-reload-inv" class="btn-run" style="background:#1f6feb">Refresh Inventory</button>
              </div>
              <form id="form-list">
                <div class="form-row">
                  <div class="field"><label>Refresh threshold (meat)</label>
                    <input type="number" name="refresh_threshold" value="2000" min="0"
                      title="Skip a live mall lookup if the last recorded unlimited price is below this value."></div>
                  <button type="submit" class="btn-run">Refresh Prices</button>
                </div>
              </form>
            </div>
          </div>
        </div>
        <div style="width:1px;background:#30363d;flex-shrink:0"></div>
        <div class="list-right">
          <div class="output-wrap" style="flex:1;min-height:0">
            <div class="output-bar">
              <span>Output</span>
              <div style="display:flex;gap:.5rem;align-items:center">
                <span id="list-status-badge" class="badge badge-idle">Idle</span>
                <button id="list-btn-cancel" class="btn-stop hidden">Stop</button>
                <button id="list-btn-clear" class="btn-clear">Clear</button>
              </div>
            </div>
            <div id="list-output-box"></div>
          </div>
        </div>
      </div>
    </div>

    <div class="form-panel hidden" id="panel-stock">
      <h6>Stock Mall</h6>
      <form id="form-stock">
        <div class="form-row">
          <div class="field"><label>Min mall price (meat)</label>
            <input type="number" name="min_price" value="10000" min="0"></div>
          <div class="field"><label>Markup %</label>
            <input type="number" name="markup_pct" value="5" min="0" class="narrow"></div>
          <div class="field"><label>Max qty / item</label>
            <input type="number" name="max_qty" value="1" min="1" class="narrow"></div>
          <button type="submit" class="btn-run">Run</button>
        </div>
      </form>
    </div>

    <div class="form-panel hidden" id="panel-monitor">
      <div class="monitor-layout">
        <div class="monitor-left">
          <div class="form-panel" style="flex-shrink:0">
            <h6>Auto Monitor</h6>
            <form id="form-monitor">
              <div class="form-row">
                <div class="field"><label>Check interval (seconds)</label>
                  <input type="number" name="interval" value="5" min="2"></div>
                <div class="field"><label>Undercut %</label>
                  <input type="number" name="undercut_pct" value="0" min="0" max="100" step="0.1" style="width:70px"></div>
                <button type="submit" class="btn-run">Start</button>
              </div>
            </form>
          </div>
          <div class="form-panel monitor-ranges-box">
            <h6>Price Ranges</h6>
            <form id="form-add-range">
              <div class="form-row">
                <div class="field"><label>Item ID or Name</label>
                  <input type="text" name="item_id_or_name" required style="width:160px" placeholder="e.g. 8823 or Mr. Burnsger"></div>
                <div class="field"><label>Buy below (meat)</label>
                  <input type="number" name="min_price" placeholder="optional"></div>
                <div class="field"><label>Sell above (meat)</label>
                  <input type="number" name="max_price" placeholder="optional"></div>
                <div class="field"><label>Buy qty</label>
                  <input type="number" name="buy_qty" value="1" min="1" class="narrow"></div>
                <button type="submit" class="btn-run">Add / Update</button>
              </div>
            </form>
            <div id="msg-ranges" style="margin-top:.5rem"></div>
            <div class="ranges-table-wrap">
              <div id="ranges-body"><p style="font-size:.82rem;color:#8b949e">Loading...</p></div>
            </div>
          </div>
        </div>
        <div style="width:1px;background:#30363d;flex-shrink:0"></div>
        <div class="monitor-right">
          <div class="output-wrap" style="flex:1;min-height:0">
            <div class="output-bar">
              <span>Output</span>
              <div style="display:flex;gap:.5rem;align-items:center">
                <span id="monitor-status-badge" class="badge badge-idle">Idle</span>
                <button id="monitor-btn-cancel" class="btn-stop hidden">Stop</button>
                <button id="monitor-btn-clear" class="btn-clear">Clear</button>
              </div>
            </div>
            <div id="monitor-output-box"></div>
          </div>
        </div>
      </div>
    </div>

    <div class="form-panel hidden" id="panel-view_store">
      <div style="display:flex;align-items:center;justify-content:space-between;flex-shrink:0">
        <h6 style="margin:0">My Mall Store</h6>
        <div style="display:flex;align-items:center;gap:.6rem">
          <span id="store-status-badge" class="badge badge-idle">Idle</span>
          <button id="btn-fetch-store" class="btn-run">Refresh</button>
        </div>
      </div>
      <div class="store-table-wrap">
        <table class="list-table">
          <thead><tr>
            <th style="text-align:left">ID</th>
            <th style="text-align:left">Name</th>
            <th style="text-align:right">Price</th>
            <th style="text-align:right">Qty</th>
            <th style="text-align:right">Limit</th>
            <th style="text-align:right">Mall Unltd (low→high)</th>
            <th style="text-align:right">Mall Ltd (lowest)</th>
          </tr></thead>
          <tbody id="store-table-body">
            <tr><td colspan="7" style="color:#8b949e;text-align:center;padding:.75rem">Loading...</td></tr>
          </tbody>
        </table>
      </div>
    </div>

    <div class="output-wrap" id="global-output-wrap">
      <div class="output-bar">
        <span>Output</span>
        <div style="display:flex;gap:.5rem;align-items:center">
          <span id="status-badge" class="badge badge-idle">Idle</span>
          <button id="btn-cancel" class="btn-stop hidden">Stop</button>
          <button id="btn-clear" class="btn-clear">Clear</button>
        </div>
      </div>
      <div id="output-box"></div>
    </div>

  </div>
</div>

<script>
// Navigation
document.querySelectorAll('.nav-btn').forEach(function(btn) {
  btn.addEventListener('click', function() {
    var panel = btn.getAttribute('data-panel');
    document.querySelectorAll('.main > .form-panel').forEach(function(el) { el.classList.add('hidden'); });
    document.getElementById('panel-' + panel).classList.remove('hidden');
    document.querySelectorAll('.nav-btn').forEach(function(b) { b.classList.remove('active'); });
    btn.classList.add('active');
    if (panel === 'list') {
      document.getElementById('global-output-wrap').classList.add('hidden');
      loadInventoryTable();
    } else if (panel === 'view_store') {
      document.getElementById('global-output-wrap').classList.add('hidden');
      triggerStoreFetch();
    } else if (panel === 'monitor') {
      document.getElementById('global-output-wrap').classList.add('hidden');
      loadRanges();
    } else {
      document.getElementById('global-output-wrap').classList.remove('hidden');
    }
  });
});

// Output polling
var linesFrom = 0;
var pollTimer = null;

document.getElementById('btn-clear').addEventListener('click', function() {
  document.getElementById('output-box').textContent = '';
  linesFrom = 0;
});

function appendLines(lines) {
  var box = document.getElementById('output-box');
  var atBottom = box.scrollHeight - box.clientHeight <= box.scrollTop + 5;
  box.textContent += lines.join('\\n') + (lines.length ? '\\n' : '');
  if (atBottom) box.scrollTop = box.scrollHeight;
}

function setRunning(running) {
  var badge = document.getElementById('status-badge');
  badge.textContent = running ? 'Running...' : 'Idle';
  badge.className = 'badge ' + (running ? 'badge-running' : 'badge-idle');
  document.getElementById('btn-cancel').classList.toggle('hidden', !running);
}

function startPolling() {
  if (pollTimer) return;
  setRunning(true);
  pollTimer = setInterval(poll, 500);
}

function stopPolling() {
  clearInterval(pollTimer);
  pollTimer = null;
  setRunning(false);
}

function poll() {
  fetch('/lines?from=' + linesFrom)
    .then(function(r) { return r.json(); })
    .then(function(data) {
      if (data.lines && data.lines.length) {
        appendLines(data.lines);
        linesFrom = data.total;
      }
      if (!data.running) stopPolling();
    })
    .catch(function(e) { console.error('poll:', e); });
}

// Generic job form submit
function bindJobForm(formId, action) {
  var form = document.getElementById(formId);
  if (!form) return;
  form.addEventListener('submit', function(e) {
    e.preventDefault();
    document.getElementById('output-box').textContent = '';
    linesFrom = 0;
    var body = new URLSearchParams(new FormData(form));
    fetch('/run/' + action, { method: 'POST', body: body })
      .then(function(r) { return r.json().then(function(d) { return { ok: r.ok, data: d }; }); })
      .then(function(res) {
        if (!res.ok) { appendLines(['ERROR: ' + (res.data.error || 'unknown error')]); return; }
        startPolling();
      });
  });
}

// Generic ajax form (instant, no polling)
function bindAjaxForm(formId, action, msgId) {
  var form = document.getElementById(formId);
  if (!form) return;
  form.addEventListener('submit', function(e) {
    e.preventDefault();
    var body = new URLSearchParams(new FormData(form));
    fetch('/run/' + action, { method: 'POST', body: body })
      .then(function(r) { return r.json().then(function(d) { return { ok: r.ok, data: d }; }); })
      .then(function(res) {
        var el = document.getElementById(msgId);
        var cls = res.ok ? 'msg msg-ok' : 'msg msg-err';
        el.innerHTML = '<div class="' + cls + '">' +
          (res.data.message || res.data.error || (res.ok ? 'Done.' : 'Error')) + '</div>';
      });
  });
}

// ---- List panel ----
var _inventoryItems = [];
var listLinesFrom = 0;
var listPollTimer = null;

function loadInventoryTable() {
  var tbody = document.getElementById('list-table-body');
  fetch('/api/inventory')
    .then(function(r) { return r.json(); })
    .then(function(data) {
      if (data.error) {
        tbody.innerHTML = '<tr><td colspan="6" style="color:#f87171;text-align:center;padding:.75rem">' + data.error + '</td></tr>';
        return;
      }
      if (!data.loaded) {
        // No snapshot yet — auto-trigger a background fetch
        tbody.innerHTML = '<tr><td colspan="6" style="color:#8b949e;text-align:center;padding:.75rem">Fetching inventory...</td></tr>';
        triggerInventoryFetch();
        return;
      }
      _inventoryItems = data.items;
      applyListFilter();
    })
    .catch(function(e) {
      tbody.innerHTML = '<tr><td colspan="6" style="color:#f87171;text-align:center;padding:.75rem">Error: ' + e + '</td></tr>';
    });
}

function triggerInventoryFetch() {
  var box = document.getElementById('list-output-box');
  box.textContent = '';
  listLinesFrom = 0;
  fetch('/run/refresh_inventory', { method: 'POST', body: new URLSearchParams() })
    .then(function(r) { return r.json().then(function(d) { return { ok: r.ok, data: d }; }); })
    .then(function(res) {
      if (!res.ok) return; // already running — polling will pick it up when done
      startListPolling();
    });
}

function applyListFilter() {
  var floor = parseInt(document.getElementById('list-display-floor').value) || 0;
  var filtered = _inventoryItems.filter(function(it) {
    if (!it.has_cached || it.min_price === null) return floor === 0;
    return it.min_price >= floor;
  });
  filtered.sort(function(a, b) {
    var ap = a.min_price === null ? -1 : a.min_price;
    var bp = b.min_price === null ? -1 : b.min_price;
    return bp - ap;
  });
  var tbody = document.getElementById('list-table-body');
  document.getElementById('list-count').textContent = filtered.length + ' item(s)';
  if (filtered.length === 0) {
    tbody.innerHTML = '<tr><td colspan="6" style="color:#8b949e;text-align:center;padding:.75rem">No items match the current filter.</td></tr>';
    return;
  }
  var rows = filtered.map(function(it) {
    var minP  = it.min_price     === null ? '—' : it.min_price.toLocaleString();
    var minU  = it.min_unlimited === null ? (it.has_cached ? '(none)' : '—') : it.min_unlimited.toLocaleString();
    var total = it.min_price     === null ? '—' : (it.min_price * it.qty).toLocaleString();
    return '<tr>' +
      '<td>' + it.item_id + '</td>' +
      '<td>' + it.name + '</td>' +
      '<td style="text-align:right">' + it.qty.toLocaleString() + '</td>' +
      '<td style="text-align:right">' + minP + '</td>' +
      '<td style="text-align:right">' + minU + '</td>' +
      '<td style="text-align:right">' + total + '</td>' +
      '</tr>';
  }).join('');
  tbody.innerHTML = rows;
}

function setListRunning(running) {
  var badge = document.getElementById('list-status-badge');
  badge.textContent = running ? 'Running...' : 'Idle';
  badge.className = 'badge ' + (running ? 'badge-running' : 'badge-idle');
  document.getElementById('list-btn-cancel').classList.toggle('hidden', !running);
}

function startListPolling() {
  if (listPollTimer) return;
  setListRunning(true);
  listPollTimer = setInterval(pollList, 500);
}

function stopListPolling() {
  clearInterval(listPollTimer);
  listPollTimer = null;
  setListRunning(false);
  loadInventoryTable();  // reload table with updated cache prices
}

function pollList() {
  fetch('/lines?from=' + listLinesFrom)
    .then(function(r) { return r.json(); })
    .then(function(data) {
      if (data.lines && data.lines.length) {
        var box = document.getElementById('list-output-box');
        var atBottom = box.scrollHeight - box.clientHeight <= box.scrollTop + 5;
        box.textContent += data.lines.join('\\n') + '\\n';
        if (atBottom) box.scrollTop = box.scrollHeight;
        listLinesFrom = data.total;
      }
      if (!data.running) stopListPolling();
    })
    .catch(function(e) { console.error('pollList:', e); });
}

document.getElementById('form-list').addEventListener('submit', function(e) {
  e.preventDefault();
  var box = document.getElementById('list-output-box');
  box.textContent = '';
  listLinesFrom = 0;
  var body = new URLSearchParams(new FormData(e.target));
  fetch('/run/list', { method: 'POST', body: body })
    .then(function(r) { return r.json().then(function(d) { return { ok: r.ok, data: d }; }); })
    .then(function(res) {
      if (!res.ok) { box.textContent += 'ERROR: ' + (res.data.error || 'unknown error') + '\\n'; return; }
      startListPolling();
    });
});

document.getElementById('list-btn-cancel').addEventListener('click', function() {
  fetch('/run/cancel', { method: 'POST', body: new URLSearchParams() });
});

document.getElementById('list-btn-clear').addEventListener('click', function() {
  document.getElementById('list-output-box').textContent = '';
  listLinesFrom = 0;
});

document.getElementById('btn-apply-floor').addEventListener('click', applyListFilter);
document.getElementById('list-display-floor').addEventListener('keydown', function(e) {
  if (e.key === 'Enter') { e.preventDefault(); applyListFilter(); }
});
document.getElementById('btn-reload-inv').addEventListener('click', triggerInventoryFetch);
bindJobForm('form-stock', 'stock');

// ---- Monitor panel ----
var monitorPollTimer = null;
var monitorLinesFrom = 0;

function appendMonitorLines(lines) {
  var box = document.getElementById('monitor-output-box');
  var atBottom = box.scrollHeight - box.clientHeight <= box.scrollTop + 5;
  box.textContent += lines.join('\\n') + (lines.length ? '\\n' : '');
  if (atBottom) box.scrollTop = box.scrollHeight;
}

function setMonitorRunning(running) {
  var badge = document.getElementById('monitor-status-badge');
  badge.textContent = running ? 'Running...' : 'Idle';
  badge.className = 'badge ' + (running ? 'badge-running' : 'badge-idle');
  document.getElementById('monitor-btn-cancel').classList.toggle('hidden', !running);
}

function startMonitorPolling() {
  if (monitorPollTimer) return;
  setMonitorRunning(true);
  monitorPollTimer = setInterval(function() {
    fetch('/lines?from=' + monitorLinesFrom)
      .then(function(r) { return r.json(); })
      .then(function(data) {
        if (data.lines && data.lines.length) {
          appendMonitorLines(data.lines);
          monitorLinesFrom = data.total;
        }
        if (!data.running) { clearInterval(monitorPollTimer); monitorPollTimer = null; setMonitorRunning(false); }
      })
      .catch(function(e) { console.error('monitorPoll:', e); });
  }, 500);
}

document.getElementById('form-monitor').addEventListener('submit', function(e) {
  e.preventDefault();
  document.getElementById('monitor-output-box').textContent = '';
  monitorLinesFrom = 0;
  var body = new URLSearchParams(new FormData(e.target));
  fetch('/run/monitor', { method: 'POST', body: body })
    .then(function(r) { return r.json().then(function(d) { return { ok: r.ok, data: d }; }); })
    .then(function(res) {
      if (!res.ok) { appendMonitorLines(['ERROR: ' + (res.data.error || 'unknown')]); return; }
      startMonitorPolling();
    });
});

document.getElementById('monitor-btn-cancel').addEventListener('click', function() {
  fetch('/run/cancel', { method: 'POST', body: new URLSearchParams() });
});

document.getElementById('monitor-btn-clear').addEventListener('click', function() {
  document.getElementById('monitor-output-box').textContent = '';
  monitorLinesFrom = 0;
});
document.getElementById('form-add-range').addEventListener('submit', function(e) {
  e.preventDefault();
  var form = e.target;
  var body = new URLSearchParams(new FormData(form));
  fetch('/run/set_range', { method: 'POST', body: body })
    .then(function(r) { return r.json().then(function(d) { return { ok: r.ok, data: d }; }); })
    .then(function(res) {
      var el = document.getElementById('msg-ranges');
      if (res.data.choices) {
        var html = '<div class="msg msg-warn">Multiple items matched — enter the item ID for the one you want:' +
          '<ul style="margin:.35rem 0 0 1.1rem;line-height:1.8">';
        res.data.choices.forEach(function(c) {
          var safe = (c.name || '').replace(/&/g, '&amp;').replace(/</g, '&lt;');
          html += '<li><b>' + c.item_id + '</b> \u2014 ' + safe + '</li>';
        });
        html += '</ul></div>';
        el.innerHTML = html;
        return;  // leave form intact
      }
      var cls = res.ok ? 'msg msg-ok' : 'msg msg-err';
      el.innerHTML = '<div class="' + cls + '">' +
        (res.data.message || res.data.error || (res.ok ? 'Done.' : 'Error')) + '</div>';
      if (res.ok) { form.reset(); loadRanges(); }
    });
});

// ---- Store panel ----
var storePollTimer = null;

function loadStoreTable() {
  fetch('/api/store')
    .then(function(r) { return r.json(); })
    .then(function(data) {
      var tbody = document.getElementById('store-table-body');
      if (!data.listings || data.listings.length === 0) {
        tbody.innerHTML = '<tr><td colspan="7" style="color:#8b949e;text-align:center;padding:.75rem">Store is empty.</td></tr>';
        return;
      }
      tbody.innerHTML = data.listings.map(function(it) {
        var lim     = it.limit ? it.limit.toLocaleString() : '&mdash;';
        var safeName = (it.name || '').replace(/&/g, '&amp;').replace(/"/g, '&quot;');
        return '<tr>' +
          '<td>' + it.item_id + '</td>' +
          '<td>' + it.name + '</td>' +
          '<td style="text-align:right">' + it.price.toLocaleString() + '</td>' +
          '<td style="text-align:right">' + it.quantity.toLocaleString() + '</td>' +
          '<td style="text-align:right">' + lim + '</td>' +
          '<td style="text-align:right">' +
            '<button class="btn-fetch-mall" data-name="' + safeName + '" ' +
            'style="font-size:.72rem;padding:.15rem .45rem">Check Prices</button>' +
          '</td>' +
          '<td style="text-align:right">&mdash;</td>' +
          '</tr>';
      }).join('');
    });
}

function setStoreRunning(running) {
  var badge = document.getElementById('store-status-badge');
  badge.textContent = running ? 'Loading...' : 'Idle';
  badge.className = 'badge ' + (running ? 'badge-running' : 'badge-idle');
  document.getElementById('btn-fetch-store').disabled = running;
}

function stopStorePolling() {
  clearInterval(storePollTimer);
  storePollTimer = null;
  setStoreRunning(false);
  loadStoreTable();
}

function startStorePolling() {
  if (storePollTimer) return;
  setStoreRunning(true);
  var from = 0;
  storePollTimer = setInterval(function() {
    fetch('/lines?from=' + from)
      .then(function(r) { return r.json(); })
      .then(function(data) {
        from = data.total;
        if (!data.running) stopStorePolling();
      })
      .catch(function(e) { console.error('storePoll:', e); });
  }, 500);
}

function triggerStoreFetch() {
  var tbody = document.getElementById('store-table-body');
  tbody.innerHTML = '<tr><td colspan="7" style="color:#8b949e;text-align:center;padding:.75rem">Loading...</td></tr>';
  fetch('/run/view_store', { method: 'POST', body: new URLSearchParams() })
    .then(function(r) { return r.json().then(function(d) { return { ok: r.ok, data: d }; }); })
    .then(function(res) {
      if (!res.ok) {
        document.getElementById('store-table-body').innerHTML =
          '<tr><td colspan="7" style="color:#f87171;text-align:center;padding:.75rem">Error: ' +
          (res.data.error || 'unknown') + '</td></tr>';
        return;
      }
      startStorePolling();
    });
}

document.getElementById('btn-fetch-store').addEventListener('click', triggerStoreFetch);

document.addEventListener('click', function(e) {
  if (!e.target.classList.contains('btn-fetch-mall')) return;
  var btn  = e.target;
  var name = btn.getAttribute('data-name');
  var row  = btn.closest('tr');
  btn.disabled    = true;
  btn.textContent = '...';
  fetch('/api/mall_price?name=' + encodeURIComponent(name))
    .then(function(r) { return r.json(); })
    .then(function(data) {
      if (data.error) { btn.disabled = false; btn.textContent = 'Check Prices'; return; }
      var cells = row.querySelectorAll('td');
      var unltd = (data.mall_unlimited && data.mall_unlimited.length)
        ? data.mall_unlimited.map(function(p) { return p.toLocaleString(); }).join(' \u00b7 ')
        : '\u2014';
      var ltd = data.mall_limited != null ? data.mall_limited.toLocaleString() : '\u2014';
      cells[5].textContent = unltd;
      cells[5].style.textAlign = 'right';
      cells[6].textContent = ltd;
      cells[6].style.textAlign = 'right';
    })
    .catch(function() { btn.disabled = false; btn.textContent = 'Check Prices'; });
});

document.getElementById('btn-cancel').addEventListener('click', function() {
  fetch('/run/cancel', { method: 'POST', body: new URLSearchParams() });
});

// Load ranges table
function loadRanges() {
  fetch('/api/state')
    .then(function(r) { return r.json(); })
    .then(function(data) {
      var el = document.getElementById('ranges-body');
      if (!data.ranges || data.ranges.length === 0) {
        el.innerHTML = '<p style="font-size:.82rem;color:#8b949e">No ranges configured yet.</p>';
        return;
      }
      var rows = data.ranges.map(function(r) {
        var minDisp = r.min_price != null ? r.min_price.toLocaleString() : '&mdash;';
        var maxDisp = r.max_price != null ? r.max_price.toLocaleString() : '&mdash;';
        var safeName = (r.name || '').replace(/&/g, '&amp;').replace(/"/g, '&quot;');
        var btnStyle = 'font-size:.72rem;padding:.15rem .45rem';
        return '<tr>' +
          '<td>' + r.item_id + '</td>' +
          '<td>' + (r.name || '') + '</td>' +
          '<td class="editable" data-field="min_price" data-value="' + (r.min_price != null ? r.min_price : '') + '">' + minDisp + '</td>' +
          '<td class="editable" data-field="max_price" data-value="' + (r.max_price != null ? r.max_price : '') + '">' + maxDisp + '</td>' +
          '<td class="editable" data-field="buy_qty"   data-value="' + (r.buy_qty || 1) + '">' + (r.buy_qty || 1) + '</td>' +
          '<td style="white-space:nowrap">' +
            '<button class="btn-run upd-range-btn" data-id="' + r.item_id + '" data-name="' + safeName + '" style="' + btnStyle + ';margin-right:.3rem">Update</button>' +
            '<button class="btn-danger rm-range-btn" data-id="' + r.item_id + '" data-name="' + safeName + '" style="' + btnStyle + '">Remove</button>' +
          '</td>' +
          '</tr>';
      }).join('');
      el.innerHTML = '<table class="rtable"><thead><tr><th>ID</th><th>Name</th>' +
        '<th>Buy Below</th><th>Sell Above</th><th>Buy Qty</th><th></th></tr></thead>' +
        '<tbody>' + rows + '</tbody></table>';
    });
}

document.addEventListener('click', function(e) {
  if (!e.target.classList.contains('rm-range-btn')) return;
  var itemId = e.target.getAttribute('data-id');
  var name = e.target.getAttribute('data-name');
  if (!confirm('Remove price range for "' + name + '"?')) return;
  var body = new URLSearchParams();
  body.append('item_id', itemId);
  fetch('/run/remove_range', { method: 'POST', body: body })
    .then(function(r) { return r.json().then(function(d) { return { ok: r.ok, data: d }; }); })
    .then(function(res) {
      var el = document.getElementById('msg-ranges');
      var cls = res.ok ? 'msg msg-ok' : 'msg msg-err';
      el.innerHTML = '<div class="' + cls + '">' +
        (res.data.message || res.data.error || (res.ok ? 'Done.' : 'Error')) + '</div>';
      if (res.ok) loadRanges();
    });
});

// Click editable cell to switch to input
document.addEventListener('click', function(e) {
  var cell = e.target.closest('td.editable');
  if (!cell || cell.querySelector('input')) return;
  var val = cell.getAttribute('data-value');
  var field = cell.getAttribute('data-field');
  var min = field === 'buy_qty' ? '1' : '0';
  var placeholder = (val === '' || val == null) ? 'optional' : '';
  cell.innerHTML = '<input type="number" value="' + (val || '') +
    '" placeholder="' + placeholder + '" min="' + min + '">';
  var inp = cell.querySelector('input');
  inp.focus();
  inp.select();
});

// Update button — save edited fields for a row
document.addEventListener('click', function(e) {
  if (!e.target.classList.contains('upd-range-btn')) return;
  var btn    = e.target;
  var row    = btn.closest('tr');
  var itemId = btn.getAttribute('data-id');
  var name   = btn.getAttribute('data-name');

  function cellVal(field) {
    var cell = row.querySelector('td[data-field="' + field + '"]');
    var inp  = cell && cell.querySelector('input');
    return inp ? inp.value.trim() : (cell ? cell.getAttribute('data-value') : '');
  }

  var body = new URLSearchParams();
  body.append('item_id_or_name', itemId);
  var minVal = cellVal('min_price');
  var maxVal = cellVal('max_price');
  var qtyVal = cellVal('buy_qty');
  if (minVal) body.append('min_price', minVal);
  if (maxVal) body.append('max_price', maxVal);
  body.append('buy_qty', qtyVal || '1');

  btn.disabled = true;
  fetch('/run/set_range', { method: 'POST', body: body })
    .then(function(r) { return r.json().then(function(d) { return { ok: r.ok, data: d }; }); })
    .then(function(res) {
      btn.disabled = false;
      var el = document.getElementById('msg-ranges');
      var cls = res.ok ? 'msg msg-ok' : 'msg msg-err';
      el.innerHTML = '<div class="' + cls + '">' +
        (res.data.message || res.data.error || (res.ok ? 'Done.' : 'Error')) + '</div>';
      if (res.ok) loadRanges();
    });
});

// Init: load state and show first panel
fetch('/api/state')
  .then(function(r) { return r.json(); })
  .then(function(data) {
    document.getElementById('user-info').textContent =
      data.username + '  \u00b7  ' + data.cache_size + ' items cached';
  });

document.querySelector('.nav-btn[data-panel="list"]').click();
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    print("  KoL Mall Bot — Web UI")
    print("  Open http://localhost:8080 in your browser.")
    print("  Press Ctrl-C to stop the server.")
    app.run(host="127.0.0.1", port=8080, debug=False)
