# Keeps the 5 most recent log files in logs\ and deletes the rest.
# Also removes any stale python.log* from the root (old location).

$logsFolder = "C:\Publish\ScanShip\logs"
$rootFolder = "C:\Publish\ScanShip"

# Delete old logs from root (should not exist after web.config fix)
Get-ChildItem "$rootFolder\python.log*" -ErrorAction SilentlyContinue |
    Where-Object { -not $_.PSIsContainer } |
    Remove-Item -Force -ErrorAction SilentlyContinue

# Keep only the 5 most recent logs in logs\
Get-ChildItem "$logsFolder\app.log*" -ErrorAction SilentlyContinue |
    Sort-Object LastWriteTime -Descending |
    Select-Object -Skip 5 |
    Remove-Item -Force -ErrorAction SilentlyContinue
