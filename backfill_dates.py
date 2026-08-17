# ============================================================
# backfill_dates.py - Backfill UsrShipLoadDate in Acumatica for historical
# fully-scanned orders that were never pushed.
#
# Per order it runs 3 phases: GET (status) -> PUT (set date) -> GET (verify).
# Reopen/Complete happen inside the PUT phase as needed. Only orders that end
# up 'Completed' are recorded as 'Synced' (resumable; a re-run skips them).
# Genuinely non-completable states (Canceled, Shipping) are skipped.
#
# Usage (PowerShell, from C:\Publish\ScanShip):
#   .\.venv\Scripts\python.exe backfill_dates.py <LIMIT>        -> process up to LIMIT
#   .\.venv\Scripts\python.exe backfill_dates.py <LIMIT> DRY    -> read-only preview
# ============================================================
import sys
import time
from datetime import datetime

import requests as rq
import pyodbc
import app
from config import SQL_CONN_STR, TABLE_SCANLOG, TABLE_ACTIONS, REST_BASE_URL

LIMIT         = int(sys.argv[1]) if len(sys.argv) > 1 else 25
DRY_RUN       = (len(sys.argv) > 2 and sys.argv[2].upper() == "DRY")
GET_TIMEOUT   = 60
WRITE_TIMEOUT = 180
MAX_TRIES     = 3
SKIP_STATUSES = {"Canceled", "Cancelled", "Shipping"}


def ts():
    return datetime.now().strftime("%m/%d/%Y %H:%M:%S")

def as_mdy(iso_or_dt):
    """Format a date/datetime or Acumatica ISO string as mm/dd/yyyy."""
    if not iso_or_dt:
        return "-"
    if isinstance(iso_or_dt, str):
        d = iso_or_dt[:10]                       # '2026-06-17'
        y, m, dd = d.split("-")
        return f"{m}/{dd}/{y}"
    return iso_or_dt.strftime("%m/%d/%Y")


class Saturated(Exception):
    """Acumatica returned HTTP 200 with an empty body (session limit hit)."""


def _rest(method, path, json=None, timeout=GET_TIMEOUT):
    s = app._get_rest_session()
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {app._get_access_token()}",
    }
    return s.request(method, f"{REST_BASE_URL}{path}", headers=headers, timeout=timeout, json=json)


def get_status_date(ot, on):
    r = _rest("GET", f"/SalesOrder/{ot}/{on}?$select=Status&$custom=Document.UsrShipLoadDate")
    if not r.ok:
        raise RuntimeError(f"GET -> {r.status_code}: {r.text[:150]}")
    if len(r.text) == 0:
        raise Saturated()
    j = r.json()
    st = (j.get("Status") or {}).get("value")
    dt = ((j.get("custom") or {}).get("Document", {}).get("UsrShipLoadDate") or {}).get("value")
    return st, dt


def _write(method, path, json):
    """Return 'ok' | 'timeout'. Raise on a real (non-timeout) error."""
    try:
        r = _rest(method, path, json=json, timeout=WRITE_TIMEOUT)
    except (rq.exceptions.Timeout, rq.exceptions.ConnectionError):
        return "timeout"   # server often finishes anyway; verify step will confirm
    if not r.ok:
        raise RuntimeError(f"{method} {path} -> {r.status_code}: {r.text[:150]}")
    if len(r.text) == 0 and method != "PUT":
        raise Saturated()
    return "ok"


def _ent(ot, on):
    return {"entity": {"OrderType": {"value": ot}, "OrderNbr": {"value": on}}, "parameters": {}}


def do_write_phase(ot, on, status, ship_date):
    """Reopen (if Completed) -> PUT date -> Complete. Returns a short label."""
    steps = []
    if status == "Completed":
        _write("POST", "/SalesOrder/ReopenSalesOrder", _ent(ot, on)); steps.append("reopen")
    _write("PUT", "/SalesOrder", {
        "OrderType": {"value": ot}, "OrderNbr": {"value": on},
        "custom": {"Document": {"UsrShipLoadDate": {
            "type": "CustomDateTimeField", "value": ship_date.isoformat()}}},
    }); steps.append("put")
    _write("POST", "/SalesOrder/CompleteOrder", _ent(ot, on)); steps.append("complete")
    return "+".join(steps)


def process(ot, on, ship_date):
    """Run the 3-phase flow with soft retries.
    Returns (result, info) where result is 'ok' | 'skip' | 'failed' | 'saturated'."""
    last = "unknown"
    for attempt in range(1, MAX_TRIES + 1):
        tag = "" if attempt == 1 else f"  (retry {attempt}/{MAX_TRIES})"
        # -- phase 1: GET --
        try:
            st, dt = get_status_date(ot, on)
        except Saturated:
            print(f"  {ts()}  GET   empty response (saturated) - waiting 30s{tag}", flush=True)
            last = "saturated"; time.sleep(30); continue
        print(f"  {ts()}  GET   status={st} date={as_mdy(dt)}{tag}", flush=True)

        if st in SKIP_STATUSES:
            return ("skip", st)
        if st == "Completed" and dt:
            return ("ok", st)                      # already backfilled

        # -- phase 2: PUT (reopen/put/complete) --
        try:
            label = do_write_phase(ot, on, st, ship_date)
            print(f"  {ts()}  PUT   date={as_mdy(ship_date)} [{label}]", flush=True)
        except Saturated:
            print(f"  {ts()}  PUT   empty response (saturated) - waiting 30s{tag}", flush=True)
            last = "saturated"; time.sleep(30); continue
        except Exception as exc:
            print(f"  {ts()}  PUT   ERROR: {str(exc)[:130]}", flush=True)
            return ("failed", str(exc)[:130])

        # -- phase 3: GET verify --
        try:
            st2, dt2 = get_status_date(ot, on)
        except Saturated:
            print(f"  {ts()}  GET   empty response (saturated) - waiting 30s{tag}", flush=True)
            last = "saturated"; time.sleep(30); continue
        ok = (st2 == "Completed" and bool(dt2))
        print(f"  {ts()}  GET   status={st2} date={as_mdy(dt2)}  {'OK' if ok else 'not completed, retrying'}", flush=True)
        if ok:
            return ("ok", st2)
        last = f"still {st2}"

    if last == "saturated":
        return ("saturated", "empty responses (Acumatica session limit / instance busy)")
    return ("failed", f"{last} after {MAX_TRIES} tries")


# ---- DB helpers ----
conn = pyodbc.connect(SQL_CONN_STR)

def fetch_orders():
    cur = conn.cursor()
    cur.execute(f"""
        SELECT TOP {LIMIT} t.OrderType, t.OrderNbr, t.last_scan
        FROM (
            SELECT OrderType, OrderNbr,
                   COUNT(*) AS total,
                   SUM(CASE WHEN ScanStatus = 'Scanned' THEN 1 ELSE 0 END) AS done,
                   MAX(ScanDate) AS last_scan
            FROM {TABLE_SCANLOG}
            GROUP BY OrderType, OrderNbr
        ) t
        WHERE t.done = t.total
          AND NOT EXISTS (SELECT 1 FROM {TABLE_ACTIONS} a
                          WHERE a.OrderType = t.OrderType AND a.OrderNbr = t.OrderNbr)
        ORDER BY t.last_scan ASC
    """)
    rows = cur.fetchall(); cur.close(); return rows

def record_synced(ot, on, d):
    cur = conn.cursor()
    cur.execute(f"""
        MERGE {TABLE_ACTIONS} AS t USING (VALUES (?, ?, ?)) AS s (OrderType, OrderNbr, ShipLoadDate)
        ON t.OrderType = s.OrderType AND t.OrderNbr = s.OrderNbr
        WHEN MATCHED THEN UPDATE SET Status='Synced', ShipLoadDate=s.ShipLoadDate,
            LastError=NULL, LastAttemptAt=GETDATE(), UpdatedAt=GETDATE()
        WHEN NOT MATCHED THEN INSERT (OrderType, OrderNbr, Status, ShipLoadDate, Attempts)
            VALUES (s.OrderType, s.OrderNbr, 'Synced', s.ShipLoadDate, 0);
    """, (ot, on, d)); conn.commit(); cur.close()


# ---- main ----
orders = fetch_orders()
print(f"backfill dates | batch={LIMIT} | orders={len(orders)} | "
      f"skip={sorted(SKIP_STATUSES)} | dry={DRY_RUN}\n", flush=True)

done, skipped, failed = 0, [], []
try:
    for i, (ot, on, last) in enumerate(orders, 1):
        print(f"[{i}/{len(orders)}] {ot}/{on}", flush=True)

        if DRY_RUN:
            try:
                st, dt = get_status_date(ot, on)
                verb = "SKIP" if st in SKIP_STATUSES else "would process"
                print(f"  {ts()}  GET   status={st} date={as_mdy(dt)}  ({verb})", flush=True)
            except Exception as exc:
                print(f"  {ts()}  GET   ERROR: {str(exc)[:120]}", flush=True)
            continue

        result, info = process(ot, on, last)
        if result == "ok":
            record_synced(ot, on, last); done += 1
        elif result == "skip":
            skipped.append((ot, on, info)); print(f"  -> skipped ({info})", flush=True)
        elif result == "saturated":
            print(f"\n>> Acumatica saturated ({info}).", flush=True)
            print(">> Stopping. Wait a few minutes and re-run - it resumes where it left off.", flush=True)
            break
        else:
            failed.append((ot, on, info)); print(f"  -> FAILED ({info})", flush=True)
finally:
    app._logout_rest()
    app._revoke_access_token()
    conn.close()

print(f"\ndone={done} | skipped={len(skipped)} | failed={len(failed)}", flush=True)
if skipped:
    print("\nSKIPPED:")
    for ot, on, st in skipped:
        print(f"  {ot}/{on}  {st}")
if failed:
    print("\nFAILED (left not completed - will retry on next run):")
    for ot, on, err in failed:
        print(f"  {ot}/{on}  {err}")
