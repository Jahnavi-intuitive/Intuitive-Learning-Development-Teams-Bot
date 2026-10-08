# Keeps the local stack up for a fixed time window: Postgres (pg0), the
# backend (8000), the agent (8001) and the "lnd-agent" dev tunnel. Checks
# once a minute and restarts whatever isn't running. While it runs it also
# tells Windows not to sleep (SetThreadExecutionState) - nothing in the
# power settings is changed, and normal sleep returns when it exits.
#
#   powershell -ExecutionPolicy Bypass -File scripts\keepalive.ps1 -Hours 48
#
# Stop it early: Stop-Process -Id (Get-Content "$env:TEMP\lnd_keepalive.pid")

param([double]$Hours = 48)

$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $PSScriptRoot
$log = Join-Path $env:TEMP "lnd_keepalive.log"
$PID | Set-Content (Join-Path $env:TEMP "lnd_keepalive.pid")
$until = (Get-Date).AddHours($Hours)

function Log($msg) { "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')  $msg" | Add-Content $log }
function Listening($port) { [bool](Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue) }
function Start-Logged([string]$what, [scriptblock]$start) {
    try { & $start; Log "$what - started" } catch { Log "$what - FAILED to start: $($_.Exception.Message)" }
}

Add-Type -Namespace Win32 -Name Power -MemberDefinition @'
[DllImport("kernel32.dll")] public static extern uint SetThreadExecutionState(uint esFlags);
'@
# ES_CONTINUOUS | ES_SYSTEM_REQUIRED - keep the machine awake (display may still turn off)
[Win32.Power]::SetThreadExecutionState([uint32]"0x80000001") | Out-Null

$devtunnel = (Get-ChildItem "$env:LOCALAPPDATA\Microsoft\WinGet\Packages" -Recurse -Filter devtunnel.exe -ErrorAction SilentlyContinue | Select-Object -First 1).FullName

Log "keepalive started, running until $until"
while ((Get-Date) -lt $until) {
    if (-not (Listening 5432)) {
        Log "postgres down - starting"
        Start-Logged "postgres" { Start-Process -ErrorAction Stop -FilePath "$root\tools\pg0.exe" -WorkingDirectory $root -WindowStyle Hidden `
            -ArgumentList "start","--port","5432","--username","lnd_app","--password","change_me_locally","--database","lnd_certification" }
        for ($i = 0; $i -lt 30 -and -not (Listening 5432); $i++) { Start-Sleep 2 }
    }
    if (-not (Listening 8000)) {
        Log "backend down - starting"
        Start-Logged "backend" { Start-Process -ErrorAction Stop -FilePath "$root\backend\.venv\Scripts\python.exe" -WorkingDirectory "$root\backend" -WindowStyle Hidden `
            -ArgumentList "-m","uvicorn","app.main:app","--host","127.0.0.1","--port","8000" `
            -RedirectStandardOutput "$env:TEMP\be.out" -RedirectStandardError "$env:TEMP\be.err" }
    }
    if (-not (Listening 8001)) {
        Log "agent down - starting"
        Start-Logged "agent" { Start-Process -ErrorAction Stop -FilePath "$root\agent\.venv\Scripts\python.exe" -WorkingDirectory "$root\agent" -WindowStyle Hidden `
            -ArgumentList "-m","google.adk.cli","api_server","--host","0.0.0.0","--port","8001","--session_service_uri=teamsmemory://local","." `
            -RedirectStandardOutput "$env:TEMP\adk.out" -RedirectStandardError "$env:TEMP\adk.err" }
    }
    if ($devtunnel -and -not (Get-Process devtunnel -ErrorAction SilentlyContinue)) {
        Log "dev tunnel down - starting"
        Start-Logged "dev tunnel" { Start-Process -ErrorAction Stop $devtunnel -ArgumentList "host","lnd-agent" -WindowStyle Hidden `
            -RedirectStandardOutput "$env:TEMP\dt_host.out" -RedirectStandardError "$env:TEMP\dt_host.err" }
    }
    Start-Sleep 60
}
[Win32.Power]::SetThreadExecutionState([uint32]"0x80000000") | Out-Null
Log "keepalive finished"
