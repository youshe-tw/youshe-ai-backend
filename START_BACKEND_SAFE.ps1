$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot
Write-Host "YUSHE AI Backend v4.4.0"

$connections = Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue
if ($connections) {
    $pids = $connections | Select-Object -ExpandProperty OwningProcess -Unique
    foreach ($pidValue in $pids) {
        try {
            Stop-Process -Id $pidValue -Force -ErrorAction Stop
            Write-Host "Stopped old backend PID $pidValue"
        } catch {
            Write-Host "Could not stop PID $pidValue"
        }
    }
    Start-Sleep -Seconds 1
}

if (Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue) {
    Write-Host "ERROR: Port 8000 is still in use."
    Get-NetTCPConnection -LocalPort 8000 -State Listen | Format-Table LocalAddress,LocalPort,OwningProcess
    Read-Host "Press Enter to exit"
    exit 2
}

python --version
python -m pip install -r requirements.txt
Write-Host "Starting backend..."
$proc = Start-Process -FilePath "python" -ArgumentList "-m","uvicorn","app:app","--host","127.0.0.1","--port","8000" -PassThru -NoNewWindow

$ready = $false
for ($i=0; $i -lt 15; $i++) {
    Start-Sleep -Seconds 1
    try {
        $r = Invoke-RestMethod -Uri "http://127.0.0.1:8000/billing/health" -TimeoutSec 2
        if ($r.version -eq "4.4.0") {
            $ready = $true
            break
        }
    } catch {}
}

if ($ready) {
    Write-Host ""
    Write-Host "BACKEND READY - v4.4.0"
    Write-Host "http://127.0.0.1:8000"
    Write-Host "Keep this window open."
    Wait-Process -Id $proc.Id
} else {
    Write-Host "ERROR: Backend failed health check."
    if (!$proc.HasExited) { Stop-Process -Id $proc.Id -Force }
    Read-Host "Press Enter to exit"
}
