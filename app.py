# ============================================================
# app.py — SoShipScan API Server
# Endpoints + serves the HTML template
# Design is in templates/index.html
# ============================================================

from flask import Flask, request, jsonify, render_template
import pyodbc
import threading
import functools
import time
import logging
import ipaddress
import mimetypes
import json
import os
import requests as http_requests
from datetime import datetime, date
from zoneinfo import ZoneInfo
from config import (
    SQL_CONN_STR, TABLE_SHIPMENTS, TABLE_SCANLOG, TABLE_ACTIONS,
    FLASK_HOST, FLASK_PORT, API_KEY, ALLOWED_NETWORKS, RESTRICT_TO_COMPANY_NETWORK,
    ODATA_URL, ODATA_USER, ODATA_PASS, SYNC_INTERVAL_MINUTES,
    ODATA_TOKEN_URL, ODATA_REVOKE_URL, ODATA_CLIENT_ID, ODATA_CLIENT_SECRET,
    REST_BASE_URL, ACUMATICA_BASE_URL, ACTION_MAX_ATTEMPTS
)

_MIAMI = ZoneInfo("America/New_York")

class _MiamiFormatter(logging.Formatter):
    def formatTime(self, record, datefmt=None):
        ct = datetime.fromtimestamp(record.created, tz=_MIAMI)
        return ct.strftime(datefmt or "%Y-%m-%d %H:%M:%S %Z")

_handler = logging.StreamHandler()
_handler.setFormatter(_MiamiFormatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
logging.root.handlers = [_handler]
logging.root.setLevel(logging.INFO)

app = Flask(__name__)

# The IIS pool can hand these out as text/plain, which browsers refuse for
# stylesheets and scripts.
mimetypes.add_type("text/css", ".css")
mimetypes.add_type("application/javascript", ".js")
mimetypes.add_type("font/woff2", ".woff2")

_STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


def asset_version() -> int:
    """Newest mtime of the static CSS/JS: busts the cache on every deploy."""
    newest = 0
    for root, _dirs, files in os.walk(_STATIC):
        for name in files:
            if name.endswith((".css", ".js")):
                newest = max(newest, int(os.path.getmtime(os.path.join(root, name))))
    return newest


# ---- Sync state ----
_last_sync_time   = None   # datetime of last completed sync
_last_sync_count  = None   # records upserted
_last_sync_error  = None   # error message if last sync failed
_sync_lock        = threading.Lock()

# ---- OAuth token cache (Acumatica Connected Application — password grant) ----
_token_cache = {"access_token": None, "refresh_token": None, "expires_at": 0}


def _get_access_token():
    """Return a cached access token, refreshing/requesting a new one as needed."""
    now = time.time()
    if _token_cache["access_token"] and now < _token_cache["expires_at"] - 30:
        return _token_cache["access_token"]

    if _token_cache["refresh_token"]:
        data = {
            "grant_type": "refresh_token",
            "refresh_token": _token_cache["refresh_token"],
            "client_id": ODATA_CLIENT_ID,
            "client_secret": ODATA_CLIENT_SECRET,
        }
    else:
        data = {
            "grant_type": "password",
            "username": ODATA_USER,
            "password": ODATA_PASS,
            "scope": "api offline_access",
            "client_id": ODATA_CLIENT_ID,
            "client_secret": ODATA_CLIENT_SECRET,
        }

    resp = http_requests.post(ODATA_TOKEN_URL, data=data, timeout=30)
    if not resp.ok and _token_cache["refresh_token"]:
        # Refresh token may have expired/been revoked — fall back to a fresh password login
        _token_cache["refresh_token"] = None
        return _get_access_token()
    resp.raise_for_status()

    token_data = resp.json()
    _token_cache["access_token"]  = token_data["access_token"]
    _token_cache["refresh_token"] = token_data.get("refresh_token", _token_cache["refresh_token"])
    _token_cache["expires_at"]    = now + token_data.get("expires_in", 3600)
    return _token_cache["access_token"]


def _revoke_access_token():
    """
    Explicitly end the current Acumatica API session instead of waiting for it to expire.
    Intended for one-off/manual scripts (not the long-running server, which should keep
    reusing its cached token) — helps stay under Acumatica's concurrent API login limit.
    """
    for token, hint in (
        (_token_cache.get("refresh_token"), "refresh_token"),
        (_token_cache.get("access_token"),  "access_token"),
    ):
        if not token:
            continue
        try:
            http_requests.post(ODATA_REVOKE_URL, data={
                "token": token,
                "token_type_hint": hint,
                "client_id": ODATA_CLIENT_ID,
                "client_secret": ODATA_CLIENT_SECRET,
            }, timeout=15)
        except Exception as exc:
            print(f"[TOKEN] revoke ({hint}) failed: {exc}")

    _token_cache["access_token"]  = None
    _token_cache["refresh_token"] = None
    _token_cache["expires_at"]    = 0


# ---- Purge shipments Acumatica replaced (phantoms) ----
# When a shipment is cancelled/recreated in Acumatica it gets a new ShipmentNbr; the old
# one stops coming in the feed but lingers locally (the sync only upserts, never deletes),
# so the scanner would show the phantom's items too. This removes those phantoms.
PURGE_REPLACED_SHIPMENTS = True
_PURGE_BACKUP_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs", "purge_backup.jsonl")


def _purge_replaced_shipments(conn, records):
    """Delete local shipments that are gone from the feed but whose order IS still in the
    feed (i.e. a replacement exists). Scans are first remapped to the current shipment.
    Every deleted row is backed up to logs/purge_backup.jsonl for reversibility.
    Safe against transient feed blips: only fires when the order still has a current
    shipment in the feed, so a single-shipment order that momentarily drops out is skipped.
    """
    feed_ships = set()
    feed_by_order = {}
    for rec in records:
        on = rec.get("OrderNbr"); sn = rec.get("ShipmentNbr")
        feed_ships.add(sn)
        feed_by_order.setdefault(on, set()).add(sn)

    cur = conn.cursor()
    cur.execute(f"SELECT DISTINCT OrderNbr, ShipmentNbr FROM {TABLE_SHIPMENTS}")
    phantoms = {}   # OrderNbr -> [phantom ShipmentNbr]
    for on, sn in cur.fetchall():
        if on in feed_by_order and sn not in feed_ships:
            phantoms.setdefault(on, []).append(sn)
    if not phantoms:
        cur.close()
        return

    # Pass 1 — back up every row we are about to delete, BEFORE touching anything.
    backup_lines = []
    for on, olds in phantoms.items():
        ph = ",".join("?" * len(olds))
        for table in (TABLE_SCANLOG, TABLE_SHIPMENTS):
            cur.execute(f"SELECT * FROM {table} WHERE OrderNbr=? AND ShipmentNbr IN ({ph})", (on, *olds))
            cols = [d[0] for d in cur.description]
            for row in cur.fetchall():
                backup_lines.append(json.dumps(
                    {"table": table, "order": on, "row": {c: str(v) for c, v in zip(cols, row)}},
                    ensure_ascii=False))
    if backup_lines:
        try:
            with open(_PURGE_BACKUP_FILE, "a", encoding="utf-8") as f:
                f.write("\n".join(backup_lines) + "\n")
        except Exception as exc:
            print(f"[PURGE] backup write failed, aborting purge: {exc}")
            cur.close()
            return

    # Pass 2 — remap scans to the current shipment, then delete the phantom rows.
    now = datetime.now()
    tot_remap = tot_log = tot_ship = 0
    for on, olds in phantoms.items():
        currents = sorted(feed_by_order[on])          # >= 1 (order is in the feed)
        cur_ph = ",".join("?" * len(currents))
        old_ph = ",".join("?" * len(olds))

        # scanned units per item across ALL shipments of the order (before changes)
        cur.execute(f"""SELECT InventoryID, SUM(CASE WHEN ScanStatus='Scanned' THEN 1 ELSE 0 END)
                        FROM {TABLE_SCANLOG} WHERE OrderNbr=? GROUP BY InventoryID""", (on,))
        scanned_all = {inv: (n or 0) for inv, n in cur.fetchall()}

        # bring the current shipment's scanned count up to what was physically scanned
        cur.execute(f"""SELECT InventoryID, COUNT(*),
                               SUM(CASE WHEN ScanStatus='Scanned' THEN 1 ELSE 0 END)
                        FROM {TABLE_SCANLOG}
                        WHERE OrderNbr=? AND ShipmentNbr IN ({cur_ph})
                        GROUP BY InventoryID""", (on, *currents))
        order_remap = 0
        for inv, units, scanned in cur.fetchall():
            promote = min(units, scanned_all.get(inv, 0)) - (scanned or 0)
            if promote > 0:
                cur.execute(f"""UPDATE TOP (?) {TABLE_SCANLOG}
                                SET ScanStatus='Scanned', ScanDate=?, ScanUser='remap'
                                WHERE OrderNbr=? AND ShipmentNbr IN ({cur_ph})
                                  AND InventoryID=? AND ScanStatus='Pending'""",
                            (promote, now, on, *currents, inv))
                order_remap += promote

        cur.execute(f"DELETE FROM {TABLE_SCANLOG} WHERE OrderNbr=? AND ShipmentNbr IN ({old_ph})", (on, *olds))
        d_log = cur.rowcount
        cur.execute(f"DELETE FROM {TABLE_SHIPMENTS} WHERE OrderNbr=? AND ShipmentNbr IN ({old_ph})", (on, *olds))
        d_ship = cur.rowcount
        tot_remap += order_remap; tot_log += d_log; tot_ship += d_ship
        print(f"[PURGE] {on}: {olds} -> {currents} | remap {order_remap} | del {d_log} log, {d_ship} ship")

    conn.commit()
    cur.close()
    print(f"[PURGE] {now.strftime('%H:%M:%S')} — {len(phantoms)} orders, "
          f"remap {tot_remap}, deleted {tot_log} log + {tot_ship} ship")


# ---- Reconcile rows that left the Acumatica feed ----
# The OData view only exposes orders in Open / Confirmed / Invoiced. When an order moves
# to any other status (On Hold, Cancelled, …) it silently disappears from the feed, and
# the upsert-only MERGE above would otherwise leave the local rows alive forever with a
# stale Status. This flips InFeed to 0 on every row the current feed did not carry.
# Nothing is deleted — scan history stays intact — and a row that comes back in a later
# feed is flipped to InFeed = 1 again by the MERGE.
RECONCILE_FEED = True

# If the feed returns less than this fraction of the rows currently marked live, treat it
# as a truncated/partial feed (permissions, view edited, OData paging cut short) and skip
# the reconcile entirely rather than flag thousands of good rows as gone.
FEED_SHRINK_GUARD = 0.5


def _reconcile_feed(conn, sync_stamp, feed_count):
    """Mark local rows absent from this sync's feed as InFeed = 0. Returns rows flagged."""
    cur = conn.cursor()
    cur.execute(f"SELECT COUNT(*) FROM {TABLE_SHIPMENTS} WHERE InFeed = 1")
    live_before = cur.fetchone()[0] or 0

    if live_before and feed_count < live_before * FEED_SHRINK_GUARD:
        print(f"[RECONCILE] SKIP — feed returned {feed_count} rows vs {live_before} live "
              f"locally (< {FEED_SHRINK_GUARD:.0%}). Feed looks partial.")
        cur.close()
        return 0

    cur.execute(f"""
        UPDATE {TABLE_SHIPMENTS}
        SET InFeed = 0
        WHERE InFeed = 1
          AND (LastSeenUTC IS NULL OR LastSeenUTC < ?)
    """, (sync_stamp,))
    dropped = cur.rowcount or 0

    # Sanity net: the feed just carried feed_count rows, so they must all still be live.
    # If nothing survived, the stamp comparison itself is broken (precision mismatch,
    # driver truncation) — undo rather than leave the whole scanner blank.
    cur.execute(f"SELECT COUNT(*) FROM {TABLE_SHIPMENTS} WHERE InFeed = 1")
    live_after = cur.fetchone()[0] or 0
    if feed_count and live_after == 0:
        conn.rollback()
        cur.close()
        print(f"[RECONCILE] ABORTED — flagged all {dropped} rows while the feed carried "
              f"{feed_count}. Stamp comparison is broken; no changes committed.")
        return 0

    if dropped:
        # Name the orders that just left, so the drop shows up in the server log
        # instead of being a silent state change.
        cur.execute(f"""
            SELECT DISTINCT OrderNbr FROM {TABLE_SHIPMENTS}
            WHERE InFeed = 0 AND LastSeenUTC IS NOT NULL AND LastSeenUTC < ?
              AND LastSeenUTC >= DATEADD(MINUTE, ?, ?)
        """, (sync_stamp, -(SYNC_INTERVAL_MINUTES * 3), sync_stamp))
        orders = sorted(r[0] for r in cur.fetchall())
        print(f"[RECONCILE] {dropped} rows left the feed | orders: {orders}")

    conn.commit()
    cur.close()
    return dropped


def sync_from_acumatica():
    """
    Pull all records from the Acumatica OData endpoint and upsert into AcuSoShipScan.
    Follows OData @odata.nextLink pagination automatically.
    Returns number of records upserted, or None on failure.
    """
    global _last_sync_time, _last_sync_count, _last_sync_error

    with _sync_lock:
        session = http_requests.Session()
        session.headers.update({
            "Accept": "application/json",
            "Authorization": f"Bearer {_get_access_token()}",
        })

        records = []
        url = ODATA_URL

        # Follow pagination
        while url:
            try:
                resp = session.get(url, timeout=60)
                resp.raise_for_status()
                data  = resp.json()
                records.extend(data.get("value", []))
                url   = data.get("@odata.nextLink")
            except Exception as exc:
                msg = f"OData fetch error: {exc}"
                print(f"[SYNC] {msg}")
                _last_sync_error = msg
                return None

        if not records:
            _last_sync_time  = datetime.now()
            _last_sync_count = 0
            _last_sync_error = None
            return 0

        conn = None
        try:
            conn   = pyodbc.connect(SQL_CONN_STR)
            cursor = conn.cursor()

            # Stamp taken from SQL Server (not the app server) so LastSeenUTC and the
            # reconcile comparison below can never disagree because of clock skew.
            # Every row this feed carries gets this exact value; anything left with an
            # older stamp is provably absent from the feed.
            #
            # Truncated to whole seconds on purpose: pyodbc drops sub-second precision
            # when binding a datetime parameter, so the value that lands in LastSeenUTC
            # would be SMALLER than the stamp we compare against in _reconcile_feed and
            # every row the feed just carried would be flagged as gone. Storing and
            # comparing the same second-precision value keeps the two exactly equal.
            cursor.execute("SELECT SYSUTCDATETIME()")
            sync_stamp = cursor.fetchone()[0].replace(microsecond=0)

            # Normalize all dates and build params list before touching the DB
            params_list = []
            for rec in records:
                ship_date = rec.get("ShipmentDate")
                if isinstance(ship_date, str) and ship_date:
                    try:
                        ship_date = datetime.fromisoformat(
                            ship_date.rstrip("Z").split("+")[0]
                        ).date()
                    except Exception:
                        ship_date = None

                params_list.append((
                    rec.get("ShipmentNbr"), rec.get("OrderNbr"),
                    rec.get("InventoryID"),  rec.get("LineNbr"),
                    rec.get("ShippedQty"),   rec.get("Status"),
                    ship_date,               rec.get("OrderType"),
                    rec.get("AccountID"),    rec.get("WarehouseID"),
                    rec.get("CustomerID"),   rec.get("ShipmentNbr_2"),
                    rec.get("InventoryID_2"), sync_stamp,
                ))

            cursor.fast_executemany = True   # bulk-sends all rows in one network trip

            cursor.executemany(f"""
                MERGE {TABLE_SHIPMENTS} AS target
                USING (VALUES (?,?,?,?,?, ?,?,?,?,?,?, ?,?, ?)) AS source
                    (ShipmentNbr, OrderNbr, InventoryID, LineNbr, ShippedQty,
                     Status, ShipmentDate, OrderType,
                     AccountID, WarehouseID, CustomerID,
                     ShipmentNbr_2, InventoryID_2, LastSeenUTC)
                ON  target.ShipmentNbr = source.ShipmentNbr
                AND target.OrderNbr    = source.OrderNbr
                AND target.InventoryID = source.InventoryID
                AND target.LineNbr     = source.LineNbr
                WHEN MATCHED THEN UPDATE SET
                    ShippedQty    = source.ShippedQty,
                    Status        = source.Status,
                    ShipmentDate  = source.ShipmentDate,
                    OrderType     = source.OrderType,
                    AccountID     = source.AccountID,
                    WarehouseID   = source.WarehouseID,
                    CustomerID    = source.CustomerID,
                    ShipmentNbr_2 = COALESCE(source.ShipmentNbr_2, target.ShipmentNbr_2),
                    InventoryID_2 = COALESCE(source.InventoryID_2, target.InventoryID_2),
                    LastSeenUTC   = source.LastSeenUTC,
                    InFeed        = 1
                WHEN NOT MATCHED THEN INSERT
                    (ShipmentNbr, OrderNbr, InventoryID, LineNbr, ShippedQty,
                     Status, ShipmentDate, OrderType,
                     AccountID, WarehouseID, CustomerID,
                     ShipmentNbr_2, InventoryID_2, LastSeenUTC, InFeed)
                VALUES
                    (source.ShipmentNbr, source.OrderNbr, source.InventoryID,
                     source.LineNbr, source.ShippedQty,
                     source.Status, source.ShipmentDate, source.OrderType,
                     source.AccountID, source.WarehouseID, source.CustomerID,
                     source.ShipmentNbr_2, source.InventoryID_2,
                     source.LastSeenUTC, 1);
            """, params_list)

            conn.commit()

            # Keep log rows in sync with the latest Acumatica values on every sync —
            # ShipmentNbr_2/InventoryID_2 only fill in when still NULL, the rest always refresh.
            fix_cursor = conn.cursor()
            fix_cursor.execute(f"""
                UPDATE l
                SET l.Status        = s.Status,
                    l.ShippedQty    = s.ShippedQty,
                    l.ShipmentDate  = s.ShipmentDate,
                    l.OrderType     = s.OrderType,
                    l.AccountID     = s.AccountID,
                    l.WarehouseID   = s.WarehouseID,
                    l.CustomerID    = s.CustomerID,
                    l.ShipmentNbr_2 = COALESCE(l.ShipmentNbr_2, s.ShipmentNbr_2),
                    l.InventoryID_2 = COALESCE(l.InventoryID_2, s.InventoryID_2)
                FROM {TABLE_SCANLOG} l
                JOIN {TABLE_SHIPMENTS} s
                    ON  s.ShipmentNbr = l.ShipmentNbr
                    AND s.OrderNbr    = l.OrderNbr
                    AND s.InventoryID = l.InventoryID
                    AND s.LineNbr     = l.LineNbr
            """)
            fix_cursor.close()
            conn.commit()

            # Remove shipments Acumatica replaced (phantoms) so the scanner only ever
            # sees the current shipment. Failure here must not break the sync.
            if PURGE_REPLACED_SHIPMENTS:
                try:
                    _purge_replaced_shipments(conn, records)
                except Exception as exc:
                    print(f"[PURGE] error (sync continues): {exc}")

            # Flag whatever the feed no longer carries (order left Open/Confirmed/Invoiced).
            # Runs after the purge so rows about to be deleted aren't flagged first.
            if RECONCILE_FEED:
                try:
                    _reconcile_feed(conn, sync_stamp, len(records))
                except Exception as exc:
                    print(f"[RECONCILE] error (sync continues): {exc}")

            conn.close()

            _last_sync_time  = datetime.now()
            _last_sync_count = len(records)
            _last_sync_error = None
            print(f"[SYNC] {_last_sync_time.strftime('%H:%M:%S')} — upserted {len(records)} records")
            return len(records)

        except Exception as exc:
            msg = f"DB error: {exc}"
            print(f"[SYNC] {msg}")
            _last_sync_error = msg
            return None
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass


def _sync_loop():
    """Background thread: sync on startup then every SYNC_INTERVAL_MINUTES."""
    while True:
        sync_from_acumatica()
        _process_pending_actions()
        time.sleep(SYNC_INTERVAL_MINUTES * 60)


# ---- Order actions (ReopenOrder / UsrShipLoadDate / CompleteOrder) ----

# Persistent HTTP session for the contract API. Reusing one session keeps the
# Acumatica login cookie alive so ALL calls share a single server-side session
# (one license seat) instead of opening — and leaking — a new one per request.
_rest_session      = None
_rest_session_lock = threading.Lock()


def _get_rest_session():
    global _rest_session
    with _rest_session_lock:
        if _rest_session is None:
            _rest_session = http_requests.Session()
        return _rest_session


def _acumatica_rest(method, path, **kwargs):
    """Call Acumatica's contract-based REST API over the shared, cookie-persistent session."""
    session = _get_rest_session()
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {_get_access_token()}",
    }
    resp = session.request(method, f"{REST_BASE_URL}{path}", headers=headers, timeout=60, **kwargs)
    if not resp.ok:
        raise RuntimeError(f"{method} {path} -> {resp.status_code}: {resp.text[:500]}")
    return resp


def _logout_rest():
    """
    Explicitly close the Acumatica contract-API session (frees the license seat).
    Revoking the OAuth token does NOT end this session — logout must be called.
    Intended for one-off scripts; the long-running server keeps its session for reuse.
    """
    global _rest_session
    with _rest_session_lock:
        session = _rest_session
        _rest_session = None
    if session is None:
        return
    try:
        session.post(f"{ACUMATICA_BASE_URL}/entity/auth/logout", timeout=15)
    except Exception as exc:
        print(f"[REST] logout failed: {exc}")
    finally:
        try:
            session.close()
        except Exception:
            pass


def _enqueue_order_action(order_type, order_nbr, ship_load_date):
    """Mark an order as ready to be pushed to Acumatica (Reopen/Update/Complete)."""
    db_execute(f"""
        MERGE {TABLE_ACTIONS} AS target
        USING (VALUES (?, ?, ?)) AS source (OrderType, OrderNbr, ShipLoadDate)
        ON  target.OrderType = source.OrderType
        AND target.OrderNbr  = source.OrderNbr
        WHEN MATCHED THEN UPDATE SET
            Status       = CASE WHEN target.Status = 'Synced' THEN 'Pending' ELSE target.Status END,
            Attempts     = CASE WHEN target.Status = 'Synced' THEN 0 ELSE target.Attempts END,
            LastError    = CASE WHEN target.Status = 'Synced' THEN NULL ELSE target.LastError END,
            ShipLoadDate = source.ShipLoadDate,
            UpdatedAt    = GETDATE()
        WHEN NOT MATCHED THEN INSERT (OrderType, OrderNbr, Status, ShipLoadDate)
            VALUES (source.OrderType, source.OrderNbr, 'Pending', source.ShipLoadDate);
    """, (order_type, order_nbr, ship_load_date))
    get_db().commit()


def _process_order_action(order_type, order_nbr, ship_load_date):
    """Reopen (if needed), set UsrShipLoadDate, and re-complete a Sales Order."""
    resp = _acumatica_rest("GET", f"/SalesOrder/{order_type}/{order_nbr}?$select=Status")
    status = (resp.json().get("Status") or {}).get("value")

    # If it's still Completed, reopen it first. Any other status (Open, Back Order, etc.)
    # means a previous attempt already reopened it — skip straight to Update + Complete.
    if status == "Completed":
        _acumatica_rest("POST", "/SalesOrder/ReopenSalesOrder", json={
            "entity": {"OrderType": {"value": order_type}, "OrderNbr": {"value": order_nbr}},
            "parameters": {}
        })

    _acumatica_rest("PUT", "/SalesOrder", json={
        "OrderType": {"value": order_type},
        "OrderNbr":  {"value": order_nbr},
        "custom": {"Document": {"UsrShipLoadDate": {
            "type": "CustomDateTimeField",
            "value": ship_load_date.isoformat()
        }}}
    })

    _acumatica_rest("POST", "/SalesOrder/CompleteOrder", json={
        "entity": {"OrderType": {"value": order_type}, "OrderNbr": {"value": order_nbr}},
        "parameters": {}
    })


def _process_pending_actions():
    """Push every order that reached 100% scanned to Acumatica (Reopen/Update/Complete)."""
    cursor = db_execute(f"""
        SELECT OrderType, OrderNbr, ShipLoadDate FROM {TABLE_ACTIONS}
        WHERE Status IN ('Pending', 'Failed') AND Attempts < ?
        ORDER BY UpdatedAt ASC
    """, (ACTION_MAX_ATTEMPTS,))
    pending = cursor.fetchall()
    cursor.close()

    for order_type, order_nbr, ship_load_date in pending:
        # Order no longer in the Acumatica view (On Hold, Cancelled, …): pushing
        # Reopen/Complete would just fail. Leave it Pending without burning an attempt —
        # it retries by itself next cycle if the order returns to the view.
        cursor = db_execute(f"""
            SELECT
                SUM(CASE WHEN InFeed = 1 THEN 1 ELSE 0 END),
                COUNT(*)
            FROM {TABLE_SHIPMENTS} WITH (NOLOCK)
            WHERE OrderNbr = ?
        """, (order_nbr,))
        live, known = cursor.fetchone()
        cursor.close()
        if known and not (live or 0):
            print(f"[ACTION] {order_type}\\{order_nbr} deferred — order not in Acumatica feed")
            continue

        db_execute(f"""
            UPDATE {TABLE_ACTIONS} SET Status = 'Processing', UpdatedAt = GETDATE()
            WHERE OrderType = ? AND OrderNbr = ?
        """, (order_type, order_nbr))
        get_db().commit()

        try:
            _process_order_action(order_type, order_nbr, ship_load_date)
            db_execute(f"""
                UPDATE {TABLE_ACTIONS}
                SET Status = 'Synced', LastError = NULL, LastAttemptAt = GETDATE(), UpdatedAt = GETDATE()
                WHERE OrderType = ? AND OrderNbr = ?
            """, (order_type, order_nbr))
        except Exception as exc:
            print(f"[ACTION] {order_type}/{order_nbr} failed: {exc}")
            db_execute(f"""
                UPDATE {TABLE_ACTIONS}
                SET Status = 'Failed', Attempts = Attempts + 1, LastError = ?,
                    LastAttemptAt = GETDATE(), UpdatedAt = GETDATE()
                WHERE OrderType = ? AND OrderNbr = ?
            """, (str(exc)[:1000], order_type, order_nbr))
        get_db().commit()


# ---- API key guard ----
def require_api_key(f):
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        if request.headers.get("X-API-Key") != API_KEY:
            return jsonify({"error": "Unauthorized"}), 401
        return f(*args, **kwargs)
    return decorated


# ---- Company network guard (scanner actions only — /log stays open everywhere) ----
_ALLOWED_NETWORKS = [ipaddress.ip_network(n) for n in ALLOWED_NETWORKS]


def require_company_network(f):
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        # Feature-flag: si está desactivado, no se restringe por red (ver config.py).
        if not RESTRICT_TO_COMPANY_NETWORK:
            return f(*args, **kwargs)
        client_ip = (request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
                     or request.remote_addr)
        try:
            ip = ipaddress.ip_address(client_ip)
        except ValueError:
            ip = None
        if ip is None or not any(ip in net for net in _ALLOWED_NETWORKS):
            return jsonify({"error": "This feature is only available on the company WiFi network"}), 403
        return f(*args, **kwargs)
    return decorated


# ---- Connection pool (one persistent connection per thread) ----
# Avoids the TCP handshake + auth cost on every request.
_local = threading.local()


def get_db():
    conn = getattr(_local, 'conn', None)
    if conn is None:
        _local.conn = pyodbc.connect(SQL_CONN_STR)
    return _local.conn


def _reset_db():
    """Drop the thread-local connection so the next get_db() reconnects."""
    try:
        conn = getattr(_local, 'conn', None)
        if conn:
            conn.close()
    except Exception:
        pass
    _local.conn = None


def db_execute(sql, params=None):
    """Run a query with automatic one-retry on broken connection."""
    params = params or []
    for attempt in range(2):
        try:
            conn   = get_db()
            cursor = conn.cursor()
            cursor.execute(sql, params)
            return cursor
        except pyodbc.Error:
            if attempt == 0:
                _reset_db()   # force reconnect on next attempt
            else:
                raise


def rows_to_dicts(cursor):
    columns = [col[0] for col in cursor.description]
    rows = []
    for row in cursor.fetchall():
        d = {}
        for i, val in enumerate(row):
            if isinstance(val, (datetime, date)):
                d[columns[i]] = val.isoformat()
            else:
                d[columns[i]] = val
        rows.append(d)
    return rows


# ---- Pages ----

@app.context_processor
def inject_base_url():
    # api_key is rendered into the page instead of being hardcoded in the templates:
    # config.py is gitignored, the templates are not, and this repo is public.
    return {
        'base_url': app.config.get('APPLICATION_ROOT', '').rstrip('/'),
        'api_key':  API_KEY,
        'asset_v':  asset_version(),
    }


@app.route("/")
def index():
    return render_template("index.html", view="scanner")


@app.route("/log")
def log_view():
    return render_template("log.html", view="log")


# ---- API ----

@app.route("/api/search")
@require_company_network
@require_api_key
def api_search():
    """Search order and insert all items into log as Pending."""
    q = (request.args.get("q") or "").strip()
    if not q:
        return jsonify({"error": "Parameter 'q' required"}), 400

    # 1. Get items from Acumatica table — search by OrderNbr only
    # InFeed = 1 keeps orders that left the Acumatica view (On Hold, Cancelled, …)
    # out of the scanner even though their rows are still stored locally.
    # NOLOCK avoids waiting for the background sync MERGE lock
    cursor = db_execute(f"""
        SELECT * FROM {TABLE_SHIPMENTS} WITH (NOLOCK)
        WHERE OrderNbr = ? AND InFeed = 1
        ORDER BY LineNbr
    """, (q,))
    acumatica_items = rows_to_dicts(cursor)
    cursor.close()

    if not acumatica_items:
        # Distinguish "never existed" from "was pulled out of the Acumatica view",
        # otherwise the operator just sees a confusing 'not found'.
        cursor = db_execute(f"""
            SELECT TOP 1 1 FROM {TABLE_SHIPMENTS} WITH (NOLOCK)
            WHERE OrderNbr = ? AND InFeed = 0
        """, (q,))
        dropped = cursor.fetchone() is not None
        cursor.close()
        msg = f"Order not available: {q}" if dropped else f"Order not found: {q}"
        return jsonify({"query": q, "total": 0, "done": 0, "items": [],
                        "in_feed": not dropped, "message": msg})

    # 2. Get all existing counts for this order in ONE query
    cursor = db_execute(f"""
        SELECT ShipmentNbr, OrderNbr, InventoryID, LineNbr, COUNT(*) AS cnt
        FROM {TABLE_SCANLOG}
        WHERE OrderNbr = ?
        GROUP BY ShipmentNbr, OrderNbr, InventoryID, LineNbr
    """, (q,))
    existing_counts = {
        (r[0], r[1], r[2], r[3]): r[4]
        for r in cursor.fetchall()
    }
    cursor.close()

    # 3. Build all missing rows in Python, then INSERT in one batch
    rows_to_insert = []
    for item in acumatica_items:
        qty     = int(item.get("ShippedQty") or 1)
        key     = (item["ShipmentNbr"], item["OrderNbr"], item["InventoryID"], item["LineNbr"])
        missing = qty - existing_counts.get(key, 0)
        for _ in range(missing):
            rows_to_insert.append((
                item["ShipmentNbr"], item["OrderNbr"], item["InventoryID"],
                item["LineNbr"],     item["ShippedQty"],
                item.get("Status"),  item.get("ShipmentDate"), item.get("OrderType"),
                item.get("AccountID"), item.get("WarehouseID"), item.get("CustomerID"),
                item.get("ShipmentNbr_2"), item.get("InventoryID_2")
            ))

    if rows_to_insert:
        conn   = get_db()
        cursor = conn.cursor()
        cursor.executemany(f"""
            INSERT INTO {TABLE_SCANLOG}
            (ShipmentNbr, OrderNbr, InventoryID, LineNbr, ShippedQty,
             Status, ShipmentDate, OrderType, AccountID, WarehouseID, CustomerID,
             ShipmentNbr_2, InventoryID_2,
             ScanDate, ScanUser, ScanStatus)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, 'Pending')
        """, rows_to_insert)
        conn.commit()

    # Fill ShipmentNbr_2 / InventoryID_2 on any existing log rows that still have NULLs
    # (covers rows inserted before these columns were added)
    cursor = db_execute(f"""
        UPDATE l
        SET l.ShipmentNbr_2 = s.ShipmentNbr_2,
            l.InventoryID_2  = s.InventoryID_2
        FROM {TABLE_SCANLOG} l
        JOIN {TABLE_SHIPMENTS} s
            ON  s.ShipmentNbr = l.ShipmentNbr
            AND s.OrderNbr    = l.OrderNbr
            AND s.InventoryID = l.InventoryID
            AND s.LineNbr     = l.LineNbr
        WHERE l.OrderNbr = ?
          AND (l.ShipmentNbr_2 IS NULL OR l.InventoryID_2 IS NULL)
    """, (q,))
    cursor.close()
    get_db().commit()

    # 3. Read back all log rows for this order
    cursor = db_execute(f"""
        SELECT * FROM {TABLE_SCANLOG}
        WHERE OrderNbr = ?
        ORDER BY LineNbr, ScanID
    """, (q,))
    log_rows = rows_to_dicts(cursor)
    cursor.close()

    # 4. Build response
    items = []
    unit_counters = {}  # track unit number per LineNbr

    for row in log_rows:
        key = (row["ShipmentNbr"], row["OrderNbr"], row["InventoryID"], row["LineNbr"])
        unit_counters[key] = unit_counters.get(key, 0) + 1

        items.append({
            "ScanID": row["ScanID"],
            "ShipmentNbr": row["ShipmentNbr"],
            "OrderNbr": row["OrderNbr"],
            "InventoryID": row["InventoryID"],
            "LineNbr": row["LineNbr"],
            "ShippedQty": row["ShippedQty"],
            "Status": row.get("Status"),
            "ShipmentDate": row.get("ShipmentDate"),
            "OrderType": row.get("OrderType"),
            "AccountID": row.get("AccountID"),
            "WarehouseID": row.get("WarehouseID"),
            "CustomerID": row.get("CustomerID"),
            "ScanStatus": row.get("ScanStatus"),
            "ScanDate": row.get("ScanDate"),
            "ScanUser": row.get("ScanUser"),
            "ShipmentNbr_2": row.get("ShipmentNbr_2"),
            "InventoryID_2": row.get("InventoryID_2"),
            "unit": unit_counters[key],
            "scanned": row.get("ScanStatus") == "Scanned"
        })

    total = len(items)
    done = sum(1 for x in items if x["scanned"])

    return jsonify({"query": q, "total": total, "done": done, "items": items})


@app.route("/api/scan", methods=["POST"])
@require_company_network
@require_api_key
def api_scan():
    """Update a Pending log row to Scanned."""
    data = request.json or {}
    order_nbr  = (data.get("OrderNbr")  or "").strip()
    order_type = (data.get("OrderType") or "").strip()
    scan_sku   = (data.get("InventoryID") or "").strip()
    scan_user  = (data.get("ScanUser") or "").strip()

    if not order_nbr or not order_type or not scan_sku:
        return jsonify({"error": "Missing required fields"}), 400

    now = datetime.now()

    # Block scans on an order that left the Acumatica view mid-session (e.g. put On Hold
    # between the search and this scan) — the order is no longer shippable.
    cursor = db_execute(f"""
        SELECT
            SUM(CASE WHEN InFeed = 1 THEN 1 ELSE 0 END),
            COUNT(*)
        FROM {TABLE_SHIPMENTS} WITH (NOLOCK)
        WHERE OrderNbr = ?
    """, (order_nbr,))
    live, known = cursor.fetchone()
    cursor.close()
    if known and not (live or 0):
        return jsonify({"error": f"Order not available: {order_nbr}"}), 409

    # Find the first Pending row for this SKU in this order
    cursor = db_execute(f"""
        SELECT TOP 1 ScanID FROM {TABLE_SCANLOG}
        WHERE OrderNbr = ?
          AND OrderType = ?
          AND InventoryID = ?
          AND ScanStatus = 'Pending'
        ORDER BY LineNbr, ScanID
    """, (order_nbr, order_type, scan_sku))

    row = cursor.fetchone()
    cursor.close()

    if not row:
        return jsonify({"error": f"Item {scan_sku} not found or already fully scanned"}), 404

    scan_id = row[0]

    # Update Pending → Scanned, and fill ShipmentNbr_2/InventoryID_2 if still NULL
    cursor = db_execute(f"""
        UPDATE l
        SET l.ScanStatus     = 'Scanned',
            l.ScanDate       = ?,
            l.ScanUser       = ?,
            l.ShipmentNbr_2  = COALESCE(l.ShipmentNbr_2, s.ShipmentNbr_2),
            l.InventoryID_2  = COALESCE(l.InventoryID_2,  s.InventoryID_2)
        FROM {TABLE_SCANLOG} l
        JOIN {TABLE_SHIPMENTS} s
            ON  s.ShipmentNbr = l.ShipmentNbr
            AND s.OrderNbr    = l.OrderNbr
            AND s.InventoryID = l.InventoryID
            AND s.LineNbr     = l.LineNbr
        WHERE l.ScanID = ?
    """, (now, scan_user, scan_id))
    cursor.close()
    get_db().commit()

    # Recalculate totals for this order
    cursor = db_execute(f"""
        SELECT
            COUNT(*) AS total,
            SUM(CASE WHEN ScanStatus = 'Scanned' THEN 1 ELSE 0 END) AS done
        FROM {TABLE_SCANLOG}
        WHERE OrderNbr = ? AND OrderType = ?
    """, (order_nbr, order_type))

    totals = cursor.fetchone()
    cursor.close()
    total = totals[0] or 0
    done  = totals[1] or 0

    if total > 0 and done >= total:
        _enqueue_order_action(order_type, order_nbr, now)

    return jsonify({
        "success": True,
        "sku": scan_sku,
        "total": total,
        "done": done,
        "completed": done >= total
    })


@app.route("/api/unscan", methods=["POST"])
@require_company_network
@require_api_key
def api_unscan():
    """Revert a Scanned log row back to Pending."""
    data       = request.json or {}
    scan_id    = data.get("ScanID")
    order_nbr  = (data.get("OrderNbr")  or "").strip()
    order_type = (data.get("OrderType") or "").strip()

    if not scan_id or not order_nbr or not order_type:
        return jsonify({"error": "Missing fields"}), 400

    cursor = db_execute(f"""
        UPDATE {TABLE_SCANLOG}
        SET ScanStatus = 'Pending', ScanDate = NULL, ScanUser = NULL
        WHERE ScanID = ? AND ScanStatus = 'Scanned'
    """, (scan_id,))
    rows_affected = cursor.rowcount
    cursor.close()
    get_db().commit()

    if rows_affected == 0:
        return jsonify({"error": "Item not found or already Pending"}), 404

    cursor = db_execute(f"""
        SELECT
            COUNT(*) AS total,
            SUM(CASE WHEN ScanStatus = 'Scanned' THEN 1 ELSE 0 END) AS done
        FROM {TABLE_SCANLOG}
        WHERE OrderNbr = ? AND OrderType = ?
    """, (order_nbr, order_type))
    totals = cursor.fetchone()
    cursor.close()

    return jsonify({
        "success": True,
        "total": totals[0] or 0,
        "done":  totals[1] or 0
    })


@app.route("/api/complete-order", methods=["POST"])
@require_company_network
@require_api_key
def api_complete_order():
    """Bulk-fill every remaining Pending log row for an order as Scanned."""
    data       = request.json or {}
    order_nbr  = (data.get("OrderNbr")  or "").strip()
    order_type = (data.get("OrderType") or "").strip()

    if not order_nbr or not order_type:
        return jsonify({"error": "Missing required fields"}), 400

    now = datetime.now()

    # Mark all Pending rows for this order as Scanned in one pass.
    # 'bulk' sentinel in ScanUser keeps these distinguishable from real scans
    # in the Scan Log (same idea as the 'remap' sentinel in _purge_replaced_shipments).
    cursor = db_execute(f"""
        UPDATE l
        SET l.ScanStatus     = 'Scanned',
            l.ScanDate       = ?,
            l.ScanUser       = 'bulk',
            l.ShipmentNbr_2  = COALESCE(l.ShipmentNbr_2, s.ShipmentNbr_2),
            l.InventoryID_2  = COALESCE(l.InventoryID_2,  s.InventoryID_2)
        FROM {TABLE_SCANLOG} l
        JOIN {TABLE_SHIPMENTS} s
            ON  s.ShipmentNbr = l.ShipmentNbr
            AND s.OrderNbr    = l.OrderNbr
            AND s.InventoryID = l.InventoryID
            AND s.LineNbr     = l.LineNbr
        WHERE l.OrderNbr   = ?
          AND l.OrderType  = ?
          AND l.ScanStatus = 'Pending'
    """, (now, order_nbr, order_type))
    filled = cursor.rowcount
    cursor.close()
    get_db().commit()

    # Recalculate totals for this order
    cursor = db_execute(f"""
        SELECT
            COUNT(*) AS total,
            SUM(CASE WHEN ScanStatus = 'Scanned' THEN 1 ELSE 0 END) AS done
        FROM {TABLE_SCANLOG}
        WHERE OrderNbr = ? AND OrderType = ?
    """, (order_nbr, order_type))

    totals = cursor.fetchone()
    cursor.close()
    total = totals[0] or 0
    done  = totals[1] or 0

    if total > 0 and done >= total:
        _enqueue_order_action(order_type, order_nbr, now)

    return jsonify({
        "success":   True,
        "filled":    filled if filled and filled > 0 else 0,
        "total":     total,
        "done":      done,
        "completed": total > 0 and done >= total
    })


@app.route("/api/log")
@require_api_key
def api_log():
    """
    Unit-level scan status view — all order types.

    Default (no search query): last 2 months by ShipmentDate.
    With search query: full history, no date restriction.

    Part 1 — rows that exist in TABLE_SCANLOG (unit rows inserted by api_search).
    Part 2 — Acumatica lines with zero log entries → shown as Pending.
    """
    q      = (request.args.get("q")      or "").strip()
    status = (request.args.get("status") or "").strip()
    try:
        page  = max(1, int(request.args.get("page",  1)))
        # The client sends exactly the rows that fit its window (can be < 10).
        limit = max(1, min(500, int(request.args.get("limit", 50))))
    except ValueError:
        page, limit = 1, 50

    # Server-side sort — whitelist to prevent SQL injection
    ALLOWED_COLS = {
        "ScanStatus", "ScanDate", "ScanUser",
        "OrderNbr", "ShipmentNbr", "ShipmentDate",
        "InventoryID", "Status"
    }
    sort_col = request.args.get("sort_col", "ScanDate")
    sort_dir = request.args.get("sort_dir", "desc").lower()
    if sort_col not in ALLOWED_COLS:
        sort_col = "ScanDate"
    if sort_dir not in ("asc", "desc"):
        sort_dir = "desc"

    offset = (page - 1) * limit
    like   = f"%{q}%" if q else None

    # q filter — applied separately in each UNION branch
    q_clause   = "AND (s.OrderNbr LIKE ? OR s.ShipmentNbr LIKE ? OR s.InventoryID LIKE ?)" if q else ""
    log_params = [like, like, like] if q else []
    shi_params = [like, like, like] if q else []

    # Date filter — only when no search query (last 2 months)
    date_clause = "" if q else "AND s.ShipmentDate >= DATEADD(MONTH, -2, GETDATE())"

    # Status filter on outer query
    outer_where  = "WHERE 1=1"
    outer_params = []
    if status:
        outer_where += " AND ScanStatus = ?"
        outer_params.append(status)

    # NULL-safe sort: NULLs always go last regardless of direction
    null_last = f"CASE WHEN {sort_col} IS NULL THEN 1 ELSE 0 END"

    cursor = db_execute(f"""
        WITH combined AS (
            -- Branch 1: unit rows already in the scan log
            SELECT
                l.ScanStatus,
                l.ScanDate,
                l.ScanUser,
                s.OrderNbr,
                s.ShipmentNbr,
                s.ShipmentDate,
                s.InventoryID,
                s.ShippedQty,
                s.Status
            FROM {TABLE_SCANLOG} l
            JOIN {TABLE_SHIPMENTS} s
                ON  s.ShipmentNbr = l.ShipmentNbr
                AND s.OrderNbr    = l.OrderNbr
                AND s.InventoryID = l.InventoryID
                AND s.LineNbr     = l.LineNbr
            WHERE 1=1
            {date_clause} {q_clause}

            UNION ALL

            -- Branch 2: lines with zero log entries → Pending
            SELECT
                'Pending'  AS ScanStatus,
                NULL       AS ScanDate,
                NULL       AS ScanUser,
                s.OrderNbr,
                s.ShipmentNbr,
                s.ShipmentDate,
                s.InventoryID,
                s.ShippedQty,
                s.Status
            FROM {TABLE_SHIPMENTS} s
            WHERE NOT EXISTS (
                SELECT 1 FROM {TABLE_SCANLOG} l
                WHERE l.ShipmentNbr = s.ShipmentNbr
                  AND l.OrderNbr    = s.OrderNbr
                  AND l.InventoryID = s.InventoryID
                  AND l.LineNbr     = s.LineNbr
            ) {date_clause} {q_clause}
        )
        SELECT COUNT(*) OVER() AS _total,
               ScanStatus, ScanDate, ScanUser,
               OrderNbr, ShipmentNbr, ShipmentDate,
               InventoryID, ShippedQty, Status
        FROM combined
        {outer_where}
        ORDER BY {null_last}, {sort_col} {sort_dir}
        OFFSET ? ROWS FETCH NEXT ? ROWS ONLY
    """, log_params + shi_params + outer_params + [offset, limit])

    logs = rows_to_dicts(cursor)
    cursor.close()

    total = logs[0]["_total"] if logs else 0
    for row in logs:
        del row["_total"]

    pages = max(1, -(-total // limit))
    return jsonify({
        "total":    total,
        "page":     page,
        "pages":    pages,
        "limit":    limit,
        "sort_col": sort_col,
        "sort_dir": sort_dir,
        "logs":     logs
    })


@app.route("/api/sync", methods=["POST"])
@require_api_key
def api_sync_manual():
    """Trigger an immediate sync from Acumatica (manual / on-demand)."""
    count = sync_from_acumatica()
    if count is None:
        return jsonify({"error": "Sync failed — check server logs"}), 500
    _process_pending_actions()
    return jsonify({"success": True, "synced": count})


@app.route("/api/debug")
@require_api_key
def api_debug():
    """Diagnostic snapshot — read-only."""
    results = {
        "last_sync": {
            "time":  _last_sync_time.isoformat() if _last_sync_time else None,
            "count": _last_sync_count,
            "error": _last_sync_error,
        }
    }

    # Shipments table summary
    cursor = db_execute(f"""
        SELECT
            COUNT(*)                        AS total_rows,
            COUNT(DISTINCT OrderNbr)        AS distinct_orders,
            MIN(ShipmentDate)               AS oldest_shipment,
            MAX(ShipmentDate)               AS newest_shipment,
            SUM(CASE WHEN ShipmentDate IS NULL THEN 1 ELSE 0 END) AS null_shipment_dates
        FROM {TABLE_SHIPMENTS}
    """)
    row = cursor.fetchone()
    cursor.close()
    results["shipments_table"] = {
        "total_rows":          row[0],
        "distinct_orders":     row[1],
        "oldest_shipment":     row[2].isoformat() if row[2] else None,
        "newest_shipment":     row[3].isoformat() if row[3] else None,
        "null_shipment_dates": row[4],
    }

    # Distinct OrderType values
    cursor = db_execute(f"""
        SELECT OrderType, COUNT(*) AS cnt
        FROM {TABLE_SHIPMENTS}
        GROUP BY OrderType
        ORDER BY cnt DESC
    """)
    results["order_types"] = [
        {"OrderType": r[0], "count": r[1]} for r in cursor.fetchall()
    ]
    cursor.close()

    # Scan log summary
    cursor = db_execute(f"""
        SELECT
            COUNT(*)  AS total_rows,
            SUM(CASE WHEN ScanStatus = 'Scanned' THEN 1 ELSE 0 END) AS scanned,
            SUM(CASE WHEN ScanStatus = 'Pending' THEN 1 ELSE 0 END) AS pending
        FROM {TABLE_SCANLOG}
    """)
    row = cursor.fetchone()
    cursor.close()
    results["scanlog_table"] = {
        "total_rows": row[0],
        "scanned":    row[1],
        "pending":    row[2],
    }

    # SOS rows in last 2 months
    cursor = db_execute(f"""
        SELECT COUNT(*) FROM {TABLE_SHIPMENTS}
        WHERE OrderType = 'SO'
          AND ShipmentDate >= DATEADD(MONTH, -2, GETDATE())
    """)
    results["sos_last_2_months"] = cursor.fetchone()[0]
    cursor.close()

    return jsonify(results)


if __name__ == "__main__":
    threading.Thread(target=_sync_loop, daemon=True).start()
    print(f"SoShipScan running at http://{FLASK_HOST}:{FLASK_PORT}")
    print(f"From scanner: http://<SERVER-IP>:{FLASK_PORT}")
    app.run(host=FLASK_HOST, port=FLASK_PORT, debug=False, use_reloader=False)