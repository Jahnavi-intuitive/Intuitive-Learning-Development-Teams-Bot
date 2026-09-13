# Packages manifest.json + color.png + outline.png into the flat .zip
# structure Teams requires for app upload (custom sideload or catalog
# submission - same package shape either way).
#
# Refuses to package while any <<PLACEHOLDER>> token remains in
# manifest.json, on purpose: those are real values (tenant/App ID,
# display name, descriptions, legal URLs) that must never ship as
# placeholders - see the brief's own "don't leave a placeholder GUID in
# what actually gets uploaded" instruction.

$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$manifestPath = Join-Path $here "manifest.json"
$outPath = Join-Path $here "app-package.zip"

$manifestText = Get-Content $manifestPath -Raw
$placeholders = [regex]::Matches($manifestText, "<<[A-Z0-9_]+>>") | ForEach-Object { $_.Value } | Sort-Object -Unique

if ($placeholders.Count -gt 0) {
    Write-Host "Cannot package yet - manifest.json still has unfilled placeholders:" -ForegroundColor Red
    $placeholders | ForEach-Object { Write-Host ("  " + $_) -ForegroundColor Red }
    Write-Host "Fill these in with real values first, then re-run this script." -ForegroundColor Yellow
    exit 1
}

try {
    $manifestText | ConvertFrom-Json | Out-Null
} catch {
    Write-Host ("manifest.json is not valid JSON: " + $_) -ForegroundColor Red
    exit 1
}

foreach ($icon in @("color.png", "outline.png")) {
    if (-not (Test-Path (Join-Path $here $icon))) {
        Write-Host ("Missing required icon: " + $icon) -ForegroundColor Red
        exit 1
    }
}

if (Test-Path $outPath) { Remove-Item $outPath -Force }
Compress-Archive -Path (Join-Path $here "manifest.json"), (Join-Path $here "color.png"), (Join-Path $here "outline.png") -DestinationPath $outPath

Write-Host ("Packaged: " + $outPath) -ForegroundColor Green
Write-Host "Upload it at: Teams admin center -> Manage apps -> Upload new app" -ForegroundColor Green
Write-Host "Org-wide catalog submission also requires the Teams App Validation checks -" -ForegroundColor Yellow
Write-Host "see teams-app/AZURE_BOT_SETUP.md and the org-catalog notes in DEVELOPMENT_LOG.md" -ForegroundColor Yellow
