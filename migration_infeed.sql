-- ============================================================
-- Migration: track rows that leave the Acumatica OData feed
--
-- Problem this fixes: the SoShipScan view only exposes orders in
-- Open / Confirmed / Invoiced. When an order changes to any other status
-- (On Hold, Cancelled, ...) it disappears from the feed, and the sync's
-- upsert-only MERGE left the local rows alive forever with a stale Status
-- (e.g. SOS142074 stayed scannable after going On Hold).
--
-- LastSeenUTC = stamp of the last sync that carried the row.
-- InFeed      = 1 while the row is in the feed, 0 once it drops out.
--               Nothing is deleted; scan history stays intact and a row
--               that returns to the feed is flipped back to 1 by the sync.
--
-- Run ONCE in SQL Server Management Studio, then restart the app.
-- ============================================================

-- Pinned so the script does not depend on the database dropdown in SSMS.
USE [Dev];
GO

ALTER TABLE dbo.AcuSoShipScan
    ADD LastSeenUTC DATETIME2 NULL,
        InFeed BIT NOT NULL CONSTRAINT DF_AcuSoShipScan_InFeed DEFAULT 1;
GO

-- Baseline: treat everything currently stored as live. The first sync after this
-- migration re-stamps whatever is really in the feed and flags the rest.
UPDATE dbo.AcuSoShipScan
SET LastSeenUTC = SYSUTCDATETIME(),
    InFeed      = 1
WHERE LastSeenUTC IS NULL;
GO

-- /api/search and /api/scan filter by InFeed on top of OrderNbr.
CREATE NONCLUSTERED INDEX IX_AcuSoShipScan_OrderNbr_InFeed
    ON dbo.AcuSoShipScan (OrderNbr, InFeed)
    INCLUDE (ShipmentNbr, InventoryID, LineNbr, LastSeenUTC);
GO

-- The reconcile pass scans live rows with an older stamp.
CREATE NONCLUSTERED INDEX IX_AcuSoShipScan_InFeed_LastSeen
    ON dbo.AcuSoShipScan (InFeed, LastSeenUTC);
GO

-- ---- Handy checks after the first sync ----
-- Orders that just dropped out of the view:
-- SELECT DISTINCT OrderNbr, MAX(LastSeenUTC) AS last_seen
-- FROM dbo.AcuSoShipScan WHERE InFeed = 0 GROUP BY OrderNbr ORDER BY last_seen DESC;
--
-- The order from the incident:
-- SELECT OrderNbr, ShipmentNbr, InventoryID, Status, InFeed, LastSeenUTC
-- FROM dbo.AcuSoShipScan WHERE OrderNbr = 'SOS142074';
