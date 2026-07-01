# ============================================================
# sync_data_provider.py — SQL Server → SharePoint CSV Sync
# ============================================================

import os
import shutil
import sys
from datetime import datetime

import pyodbc
import pandas as pd
from config import SQL_CONN_STR, SHAREPOINT_DIR, SQL_QUERY

OUTPUT_DIR   = r"C:\Publish\ScanShip\exports"
FINAL_FILE   = os.path.join(OUTPUT_DIR, "SoScanShip-DataProvider.csv")
TEMP_FILE    = os.path.join(OUTPUT_DIR, "SoScanShip-DataProvider_temp.csv")

SHAREPOINT_FILE = os.path.join(SHAREPOINT_DIR, "SoScanShip-DataProvider.csv")

OUTPUT_COLUMNS = [
    "ShipmentNbr", "OrderNbr", "Status", "InventoryID", "ShippedQty",
    "ShipmentDate", "OrderType", "ShipmentNbr_2", "ShipmentType", "LineNbr",
    "AccountID", "WarehouseID", "CustomerID", "BranchID", "InventoryID_2",
    "ScanDate", "ScanUser", "ScanStatus", "__PowerAppsId__",
]

FIXED_FIELDS = {"ShipmentType": "I", "BranchID": "MAIN"}


def log(msg: str):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def fetch_data() -> pd.DataFrame:
    conn = pyodbc.connect(SQL_CONN_STR)
    try:
        cursor = conn.cursor()
        cursor.execute(SQL_QUERY)
        columns = [col[0] for col in cursor.description]
        return pd.DataFrame.from_records(cursor.fetchall(), columns=columns)
    finally:
        conn.close()


def _fmt_scan_date(val) -> str:
    if val is None or (hasattr(val, '__class__') and val.__class__.__name__ == 'NaTType'):
        return ""
    try:
        import pandas as _pd
        if _pd.isna(val):
            return ""
    except Exception:
        pass
    if isinstance(val, str):
        try:
            val = datetime.fromisoformat(val)
        except Exception:
            return val
    try:
        return val.strftime("%-m/%-d/%Y  %-I:%M:%S %p")
    except ValueError:
        return val.strftime("%#m/%#d/%Y  %#I:%M:%S %p")


def transform(df: pd.DataFrame) -> pd.DataFrame:
    if "ScanID" in df.columns:
        df["__PowerAppsId__"] = "ScannerApp" + df["ScanID"].astype(str)
    else:
        log("WARNING: ScanID not found in view — __PowerAppsId__ will be empty")
        df["__PowerAppsId__"] = ""

    for field, value in FIXED_FIELDS.items():
        if field not in df.columns:
            df[field] = value

    for col in OUTPUT_COLUMNS:
        if col not in df.columns:
            log(f"WARNING: column '{col}' not in view — added as empty")
            df[col] = ""

    if "ScanDate" in df.columns:
        df["ScanDate"] = df["ScanDate"].apply(_fmt_scan_date)

    return df[OUTPUT_COLUMNS]


def write_csv(df: pd.DataFrame):
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    df.to_csv(TEMP_FILE, index=False, sep=",", encoding="ascii", errors="replace")
    try:
        os.replace(TEMP_FILE, FINAL_FILE)
    except PermissionError:
        shutil.copy2(TEMP_FILE, FINAL_FILE)
        os.remove(TEMP_FILE)


def copy_to_sharepoint():
    """Best-effort copy of the local CSV to the SharePoint sync folder."""
    try:
        shutil.copy2(FINAL_FILE, SHAREPOINT_FILE)
        print(f"[EXPORT] copied to SharePoint folder", flush=True)
    except Exception as exc:
        print(f"[EXPORT] SharePoint copy failed — {exc}", flush=True)


def export_to_excel() -> int | None:
    """Fetch, transform, and write the CSV data provider. Returns row count or None on error."""
    try:
        df = fetch_data()
        df = transform(df)
        write_csv(df)
        row_count = len(df)
        print(
            f"[EXPORT] {datetime.now().strftime('%H:%M:%S')} "
            f"— exported {row_count} rows to CSV",
            flush=True,
        )
        copy_to_sharepoint()
        return row_count

    except PermissionError as exc:
        print(f"[EXPORT] FILE LOCKED — {exc}", flush=True)
        return None

    except Exception as exc:
        print(f"[EXPORT] ERROR — {exc}", flush=True)
        return None


def main():
    result = export_to_excel()
    if result is None:
        sys.exit(1)


if __name__ == "__main__":
    main()
