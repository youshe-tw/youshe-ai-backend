@echo off
setlocal EnableExtensions EnableDelayedExpansion
title YUSHE AI Backend v4.5.0
chcp 65001 >nul 2>&1

pushd "%~dp0"
if errorlevel 1 (
  echo [ERROR] Cannot enter backend folder.
  pause
  exit /b 1
)

echo ==========================================
echo YUSHE AI Backend v4.5.0
echo ==========================================
echo.

echo [1/4] Checking port 8000...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$pids = Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess -Unique; " ^
  "foreach($pid in $pids){ try { Stop-Process -Id $pid -Force -ErrorAction Stop; Write-Host ('Stopped old backend PID ' + $pid) } catch { Write-Host ('Could not stop PID ' + $pid) } }"

timeout /t 1 /nobreak >nul

for /L %%N in (1,1,5) do (
  powershell -NoProfile -ExecutionPolicy Bypass -Command ^
    "if(Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue){ exit 1 } else { exit 0 }"
  if not errorlevel 1 goto PORT_FREE
  echo Waiting for port 8000 to become free...
  timeout /t 1 /nobreak >nul
)

echo [ERROR] Port 8000 is still in use.
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue | Select-Object LocalAddress,LocalPort,OwningProcess | Format-Table -AutoSize"
echo.
echo Close the process shown above and run START_BACKEND.bat again.
pause
popd
exit /b 2

:PORT_FREE
echo [OK] Port 8000 is free.
echo.

echo [2/4] Checking Python...
python --version
if errorlevel 1 (
  echo [ERROR] Python was not found.
  pause
  popd
  exit /b 3
)

echo.
echo [3/4] Checking packages...
python -m pip install -r requirements.txt
if errorlevel 1 (
  echo [ERROR] Package installation failed.
  pause
  popd
  exit /b 4
)

echo.
echo [4/4] Starting backend...
start "YUSHE_BACKEND_PROCESS" /B python -m uvicorn app:app --host 127.0.0.1 --port 8000

echo Waiting for backend health check...
set "READY=0"
for /L %%N in (1,1,15) do (
  timeout /t 1 /nobreak >nul
  powershell -NoProfile -ExecutionPolicy Bypass -Command ^
    "try { $r = Invoke-RestMethod -Uri 'http://127.0.0.1:8000/billing/health' -TimeoutSec 2; if($r.version -eq '4.5.0'){ exit 0 } else { exit 1 } } catch { exit 1 }"
  if not errorlevel 1 (
    set "READY=1"
    goto BACKEND_READY
  )
)

:BACKEND_READY
if "%READY%"=="1" (
  echo.
  echo ==========================================
  echo BACKEND READY - v4.5.0
  echo http://127.0.0.1:8000
  echo ==========================================
  echo Keep this window open while using SketchUp.
  echo.
  powershell -NoProfile -Command "while($true){ Start-Sleep -Seconds 3600 }"
) else (
  echo.
  echo [ERROR] Backend failed health check.
  echo Check the errors above.
  pause
)

popd
endlocal
