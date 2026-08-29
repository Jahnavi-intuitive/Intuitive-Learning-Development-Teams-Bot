<#
.SYNOPSIS
  Refreshes scripts/db_dump/full_dump.sql from whatever's in the live
  database right now. Run this after seeding more data (new certs, more
  test employees, real submissions, etc.) if you want setup_new_machine.ps1
  to reproduce that state on another machine too.

.EXAMPLE
  scripts\export_db_dump.ps1
#>
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$pg0Bin = Join-Path $env:USERPROFILE ".pg0\installation\18.1.0\bin\pg_dump.exe"
$dumpDir = Join-Path $root "scripts\db_dump"
$dumpPath = Join-Path $dumpDir "full_dump.sql"

if (-not (Test-Path $pg0Bin)) {
    Write-Error "Can not find pg_dump at $pg0Bin - has pg0 been started at least once yet (installation happens on first 'pg0 start')?"
    exit 1
}

New-Item -ItemType Directory -Force -Path $dumpDir | Out-Null

Write-Host "Dumping lnd_certification (schema + data) to $dumpPath ..."
$env:PGPASSWORD = "change_me_locally"
& $pg0Bin -h 127.0.0.1 -p 5432 -U lnd_app -d lnd_certification --no-owner --no-privileges -f $dumpPath
Remove-Item Env:\PGPASSWORD

Write-Host "Done. This file is gitignored (real employee PII) - restore it directly with psql, never commit it." -ForegroundColor Green
