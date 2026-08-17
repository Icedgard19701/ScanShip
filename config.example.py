# ============================================================
# config.example.py — template for config.py
#
# config.py holds live credentials and is gitignored. Copy this file to
# config.py and fill in the real values before running the app.
#
#     copy config.example.py config.py
# ============================================================

# --- Acumatica instance ---
ACUMATICA_BASE_URL     = "https://YOUR-TENANT.acumatica.com"
ACUMATICA_COMPANY      = "Your Company"
ACUMATICA_GI_NAME      = "SoShipScan"
ACUMATICA_REST_VERSION = "24.200.001"

# --- Acumatica OData (read-only feed the scanner is built on) ---
# The generic inquiry exposed here must only list orders that are actually
# shippable (Open / Confirmed / Invoiced). Orders that leave those statuses
# disappear from the feed and the sync flags them with InFeed = 0.
ODATA_URL  = "https://YOUR-TENANT.acumatica.com/odata/Your%20Company/SoShipScan"
ODATA_USER = "your-user"
ODATA_PASS = "your-password"

# --- Acumatica OAuth 2.0 (Connected Application — SM303010, password grant) ---
ODATA_TOKEN_URL     = f"{ACUMATICA_BASE_URL}/identity/connect/token"
ODATA_REVOKE_URL    = f"{ACUMATICA_BASE_URL}/identity/connect/revocation"
ODATA_CLIENT_ID     = "00000000-0000-0000-0000-000000000000@Your Company"
ODATA_CLIENT_SECRET = "your-client-secret"

# --- Acumatica REST contract API (ReopenSalesOrder / UsrShipLoadDate / CompleteOrder) ---
REST_BASE_URL = f"{ACUMATICA_BASE_URL}/entity/Default/{ACUMATICA_REST_VERSION}"

# --- SQL Server ---
SQL_SERVER   = r"YOUR-SERVER\SQLEXPRESS"
SQL_DATABASE = "YourDatabase"
# Leave both empty to use Windows Authentication
SQL_USER = ""
SQL_PASS = ""

# Connection string (automatically assembled)
if SQL_USER:
    SQL_CONN_STR = (
        f"DRIVER={{ODBC Driver 17 for SQL Server}};"
        f"SERVER={SQL_SERVER};"
        f"DATABASE={SQL_DATABASE};"
        f"UID={SQL_USER};"
        f"PWD={SQL_PASS};"
    )
else:
    SQL_CONN_STR = (
        f"DRIVER={{ODBC Driver 17 for SQL Server}};"
        f"SERVER={SQL_SERVER};"
        f"DATABASE={SQL_DATABASE};"
        f"Trusted_Connection=yes;"
        f"Connection Timeout=30;"
    )

# --- Tables (see migration_infeed.sql and indexes.sql) ---
TABLE_SHIPMENTS = "AcuSoShipScan"
TABLE_SCANLOG   = "AcuSoShipScanLog"
TABLE_ACTIONS   = "AcuSoShipScanAction"

# --- Sync ---
SYNC_INTERVAL_MINUTES = 5

# --- Order actions (Reopen/Update/Complete) retry cap ---
ACTION_MAX_ATTEMPTS = 5

# --- Flask ---
FLASK_HOST = "0.0.0.0"   # accepts connections from other devices on the network
FLASK_PORT = 5000

# --- API security ---
# Rendered into the page templates at request time. Never hardcode it in
# templates/ — those files are committed.
API_KEY = "change-me"

# --- Restrict the scanner (not /log) to the company network ---
# Requires IIS to forward the real client IP (URL Rewrite + X-Forwarded-For).
# Keep it False until that is configured.
RESTRICT_TO_COMPANY_NETWORK = False
ALLOWED_NETWORKS = []   # e.g. ["192.168.1.0/24"]
