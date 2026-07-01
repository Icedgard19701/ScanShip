# ============================================================
# sync_odata.py — Downloads data from Acumatica OData and
# inserts/updates it in SQL Server (UPSERT)
# Run manually or schedule in Task Scheduler every 10 min
# ============================================================

import requests
import pyodbc
from datetime import datetime
from config import (
    ODATA_URL, ODATA_USER, ODATA_PASS,
    SQL_CONN_STR, TABLE_SHIPMENTS
)


def fetch_odata():
    """Calls Acumatica OData and returns the list of records."""
    print(f"[{datetime.now()}] Connecting to Acumatica OData...")

    all_records = []
    url = ODATA_URL

    while url:
        response = requests.get(
            url,
            auth=(ODATA_USER, ODATA_PASS),
            headers={"Accept": "application/json"},
            timeout=120
        )
        response.raise_for_status()
        data = response.json()

        records = data.get("value", [])
        all_records.extend(records)

        # OData pagination: if there are more pages, @odata.nextLink is provided
        url = data.get("@odata.nextLink", None)

    print(f"  -> {len(all_records)} records downloaded from Acumatica.")
    return all_records


def clean(value):
    """Trims strings. Returns None if empty."""
    if value is None:
        return None
    if isinstance(value, str):
        value = value.strip()
        return value if value else None
    return value


def parse_date(value):
    """Converts OData date to date format or None."""
    if not value:
        return None
    try:
        # OData may come in ISO format: 2026-04-13T00:00:00
        return datetime.fromisoformat(value.replace("Z", "")).date()
    except (ValueError, AttributeError):
        return None


def upsert_to_sql(records):
    """Inserts or updates records in SQL Server."""
    print(f"[{datetime.now()}] Connecting to SQL Server...")
    conn = pyodbc.connect(SQL_CONN_STR)
    cursor = conn.cursor()

    # MERGE (UPSERT): if the record already exists, it updates it; otherwise, it inserts it
    merge_sql = f"""
    MERGE {TABLE_SHIPMENTS} AS target
    USING (SELECT
        ? AS ShipmentNbr,
        ? AS OrderNbr,
        ? AS InventoryID,
        ? AS LineNbr
    ) AS source
    ON (
        target.ShipmentNbr = source.ShipmentNbr
        AND target.OrderNbr = source.OrderNbr
        AND target.InventoryID = source.InventoryID
        AND target.LineNbr = source.LineNbr
    )
    WHEN MATCHED THEN UPDATE SET
        Status       = ?,
        ShippedQty   = ?,
        ShipmentDate = ?,
        OrderType    = ?,
        ShipmentType = ?,
        AccountID    = ?,
        WarehouseID  = ?,
        CustomerID   = ?,
        BranchID     = ?,
        UpdatedAt    = GETDATE()
    WHEN NOT MATCHED THEN INSERT (
        ShipmentNbr, OrderNbr, InventoryID, LineNbr,
        Status, ShippedQty, ShipmentDate, OrderType,
        ShipmentType, AccountID, WarehouseID, CustomerID, BranchID
    ) VALUES (
        ?, ?, ?, ?,
        ?, ?, ?, ?,
        ?, ?, ?, ?, ?
    );
    """

    # Mark sync start to delete old records later
    cursor.execute(f"SELECT GETDATE()")
    sync_start = cursor.fetchone()[0]

    inserted = 0
    errors = 0

    for r in records:
        try:
            shipment_nbr  = clean(r.get("ShipmentNbr"))
            order_nbr     = clean(r.get("OrderNbr"))
            inventory_id  = clean(r.get("InventoryID"))
            line_nbr      = r.get("LineNbr")
            status        = clean(r.get("Status"))
            shipped_qty   = r.get("ShippedQty")
            shipment_date = parse_date(r.get("ShipmentDate"))
            order_type    = clean(r.get("OrderType"))
            shipment_type = clean(r.get("ShipmentType"))
            account_id    = clean(r.get("AccountID"))
            warehouse_id  = clean(r.get("WarehouseID"))
            customer_id   = clean(r.get("CustomerID"))
            branch_id     = clean(r.get("BranchID"))

            # Skip records without key data
            if not shipment_nbr or not order_nbr or not inventory_id or line_nbr is None:
                errors += 1
                continue

            cursor.execute(merge_sql, (
                # -- For the ON (match keys) --
                shipment_nbr, order_nbr, inventory_id, line_nbr,
                # -- For the UPDATE --
                status, shipped_qty, shipment_date, order_type,
                shipment_type, account_id, warehouse_id, customer_id, branch_id,
                # -- For the INSERT --
                shipment_nbr, order_nbr, inventory_id, line_nbr,
                status, shipped_qty, shipment_date, order_type,
                shipment_type, account_id, warehouse_id, customer_id, branch_id
            ))
            inserted += 1

        except Exception as e:
            errors += 1
            print(f"  Error in record {r.get('ShipmentNbr')}/{r.get('LineNbr')}: {e}")

    conn.commit()

    # Delete records that no longer come from Acumatica (exact replica)
    cursor.execute(
        f"DELETE FROM {TABLE_SHIPMENTS} WHERE UpdatedAt < ?",
        (sync_start,)
    )
    deleted = cursor.rowcount
    conn.commit()

    cursor.close()
    conn.close()

    print(f"  -> {inserted} records processed, {errors} errors, {deleted} deleted.")


def main():
    print("=" * 50)
    print("SYNC ODATA - SoShipScan")
    print("=" * 50)

    try:
        records = fetch_odata()
        if records:
            upsert_to_sql(records)
        else:
            print("  No records found in OData.")
    except requests.exceptions.RequestException as e:
        print(f"  Acumatica connection ERROR: {e}")
    except pyodbc.Error as e:
        print(f"  SQL Server ERROR: {e}")
    except Exception as e:
        print(f"  Unexpected ERROR: {e}")

    print(f"[{datetime.now()}] Sync completed.")
    print("=" * 50)


if __name__ == "__main__":
    main()