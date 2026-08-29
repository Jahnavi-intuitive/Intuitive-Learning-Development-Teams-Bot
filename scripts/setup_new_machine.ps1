<#
.SYNOPSIS
  Recreates the full local database (schema + all seeded data) on a new
  Windows machine from scripts/db_dump/full_dump.sql - the exact database
  this project has been running against, exported with pg_dump.

.DESCRIPTION
  Does NOT touch Python venvs, the backend, or the agent - this is only
  the "get the database into the same state" step. See README.md for the
  remaining steps (install each component's requirements.txt, start the
  backend/agent). Safe to re-run: it checks whether the target database
  already has tables before restoring, and refuses to overwrite existing
  data unless -Fresh is passed.

.PARAMETER Fresh
  Drop any existing pg0 "default" instance first, so the restore always
  lands on a truly empty database. DESTROYS whatever is currently in it -
  only pass this if you mean to start over.

.EXAMPLE
  scripts\setup_new_machine.ps1
#>
param(
    [switch]$Fresh
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$toolsDir = Join-Path $root "tools"
$pg0Path = Join-Path $toolsDir "pg0.exe"
$dumpPath = Join-Path $root "scripts\db_dump\full_dump.sql"

Write-Host "== L&D and Certification Agent: database setup on this machine ==" -ForegroundColor Cyan

if (-not (Test-Path $dumpPath)) {
    Write-Error "Can not find $dumpPath - this script expects to run from a copy of the project that includes scripts/db_dump/full_dump.sql."
    exit 1
}

# --- 1. Get pg0.exe (embedded PostgreSQL 18 + pgvector, no install needed) ---
if (-not (Test-Path $pg0Path)) {
    Write-Host "tools/pg0.exe not found - downloading it (single ~57MB binary, no installer)..."
    New-Item -ItemType Directory -Force -Path $toolsDir | Out-Null
    try {
        Invoke-WebRequest -Uri "https://github.com/vectorize-io/pg0/releases/download/v0.15.1/pg0-windows-x86_64.exe" -OutFile $pg0Path
    } catch {
        Write-Error "Could not download pg0.exe automatically ($_). Get it by hand from https://github.com/vectorize-io/pg0/releases and save it as $pg0Path, then re-run this script."
        exit 1
    }
}

if ($Fresh) {
    Write-Host "Dropping any existing 'default' pg0 instance (as requested via -Fresh)..."
    & $pg0Path drop 2>&1 | Out-Null
}

# --- 2. Start Postgres, detached, so it keeps running after this script exits ---
Write-Host "Starting Postgres..."
Start-Process -FilePath $pg0Path -ArgumentList @(
    "start", "--port", "5432",
    "--username", "lnd_app",
    "--password", "change_me_locally",
    "--database", "lnd_certification"
) -WindowStyle Hidden

$ready = $false
for ($i = 0; $i -lt 30; $i++) {
    Start-Sleep -Seconds 1
    $conn = Test-NetConnection -ComputerName 127.0.0.1 -Port 5432 -WarningAction SilentlyContinue
    if ($conn.TcpTestSucceeded) { $ready = $true; break }
}
if (-not $ready) {
    Write-Error "Postgres did not come up on port 5432 after 30s. Check status with: $pg0Path info"
    exit 1
}
Write-Host "Postgres is up on port 5432." -ForegroundColor Green

# --- 3. Restore the dump, but only into a genuinely empty database ---
$tableCountRaw = & $pg0Path psql -t -c "SELECT count(*) FROM information_schema.tables WHERE table_schema='public';" 2>&1
$tableCount = ($tableCountRaw | Select-String '\d+' | ForEach-Object { $_.Matches[0].Value } | Select-Object -First 1)

if ($tableCount -eq "0") {
    Write-Host "Restoring schema + all seeded data from scripts/db_dump/full_dump.sql..."
    & $pg0Path psql -f $dumpPath
    Write-Host "Restore complete." -ForegroundColor Green
} else {
    Write-Host "This database already has $tableCount table(s) - skipping restore so nothing gets overwritten." -ForegroundColor Yellow
    Write-Host "Re-run with -Fresh to wipe it and restore cleanly, if that is what you want."
}

Write-Host ""
Write-Host "Database is ready. Remaining steps (see README.md for full detail):" -ForegroundColor Cyan
Write-Host "  1. Fill in GEMINI_API_KEY (backend/.env) and GOOGLE_API_KEY (agent/.env) - see the .env.example files."
Write-Host "  2. backend: python -m venv .venv; .venv\Scripts\pip install -r requirements.txt; .venv\Scripts\python -m uvicorn app.main:app --host 0.0.0.0 --port 8000"
Write-Host "  3. agent:   python -m venv .venv; .venv\Scripts\pip install -r requirements.txt; .venv\Scripts\adk telemetry disable; .venv\Scripts\adk api_server --port 8001 ."
Write-Host "  4. Open http://localhost:8000/"
Write-Host ""
Write-Host "Note: if Python's HTTPS to Gemini gets silently killed on this machine too (see DEVELOPMENT_LOG.md's 2026-08-24 TLS-blocker entries), the curl_transport.py workaround in backend/ and agent/ is already in place and will just work. If Gemini calls work fine here instead, those patches are harmless no-ops (they just add subprocess overhead) - safe to leave alone either way."
