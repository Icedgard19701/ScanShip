-- ============================================================
-- Recommended indexes for AcuSoShipScanLog
-- Run once in SQL Server Management Studio.
-- Each index targets a specific query pattern in app.py.
-- ============================================================

-- 1. Default log view + pagination
--    Query:  ORDER BY ScanID DESC  (already covered if ScanID is the PK)
--    If ScanID is NOT the PK / clustered index, create this:
-- CREATE CLUSTERED INDEX CX_ScanLog_ScanID
--     ON dbo.AcuSoShipScanLog (ScanID DESC);

-- 2. General search  (q param: OrderNbr LIKE, ShipmentNbr LIKE, InventoryID LIKE)
--    Covers equality lookups; LIKE '%x%' still scans but the index is narrower.
CREATE NONCLUSTERED INDEX IX_ScanLog_OrderNbr
    ON dbo.AcuSoShipScanLog (OrderNbr)
    INCLUDE (ScanID, ScanStatus, ScanDate, ScanUser,
             ShipmentNbr, ShipmentDate, InventoryID, Status);

CREATE NONCLUSTERED INDEX IX_ScanLog_ShipmentNbr
    ON dbo.AcuSoShipScanLog (ShipmentNbr)
    INCLUDE (ScanID, ScanStatus, ScanDate, ScanUser,
             OrderNbr, ShipmentDate, InventoryID, Status);

CREATE NONCLUSTERED INDEX IX_ScanLog_InventoryID
    ON dbo.AcuSoShipScanLog (InventoryID)
    INCLUDE (ScanID, ScanStatus, ScanDate, ScanUser,
             OrderNbr, ShipmentNbr, ShipmentDate, Status);

-- 3. ScanStatus filter  (status=Scanned / Pending)
CREATE NONCLUSTERED INDEX IX_ScanLog_ScanStatus
    ON dbo.AcuSoShipScanLog (ScanStatus, ScanID DESC)
    INCLUDE (ScanDate, ScanUser, OrderNbr, ShipmentNbr,
             ShipmentDate, InventoryID, Status);

-- 4. api/search — COUNT by (ShipmentNbr, OrderNbr, InventoryID, LineNbr)
CREATE NONCLUSTERED INDEX IX_ScanLog_LineKey
    ON dbo.AcuSoShipScanLog (ShipmentNbr, OrderNbr, InventoryID, LineNbr)
    INCLUDE (ScanStatus, ScanID);

-- 5. api/scan — find TOP 1 Pending row fast
CREATE NONCLUSTERED INDEX IX_ScanLog_Pending
    ON dbo.AcuSoShipScanLog (OrderNbr, InventoryID, ScanStatus, LineNbr, ScanID)
    WHERE ScanStatus = 'Pending';   -- filtered index: only indexes Pending rows
