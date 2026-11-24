@echo off
rem Start every live component for a trading day: Ollama, Oracle collector + cloud filler, PC live lab,
rem page in the browser, live scorecard (how many suggested setups hit target vs stop). PAPER/TEST only.
title GPLM trading day
setlocal
cd /d "%~dp0"
set "PY=E:\Guru\Learning\Alekhya\venv\Scripts\python.exe"
if not exist "%PY%" set "PY=%~dp0.venv\Scripts\python.exe"
set "PYTHONPATH=%~dp0src"
set "PYTHONUTF8=1"
set "CTL=~/stock-gplm-venv/bin/python ~/gplm-live/gplm_live_ctl.py"

echo.
echo [1/6] Local AI (Ollama qwen3:14b)
curl -s -o nul -m 3 http://127.0.0.1:11434/api/tags
if errorlevel 1 (
  echo     not running - starting Ollama
  start "Ollama" /min "%LOCALAPPDATA%\Programs\Ollama\ollama.exe" serve
) else (
  echo     already running
)

echo.
echo [2/6] Oracle cloud status (token, collector, filler)
ssh -o ConnectTimeout=20 sne-cloud "%CTL% status"
if errorlevel 1 echo     !! cannot reach sne-cloud - check internet / SSH, then rerun this file

echo.
echo [3/6] Oracle: start live collector + cloud filler (skips anything already running)
ssh -o ConnectTimeout=20 sne-cloud "%CTL% start"
echo     If it says "no fresh token": on the page click "Connect Shoonya" and paste today's code.
echo     The page login starts the collector and filler by itself.

echo.
echo [4/6] Tailscale Funnel (family / phone access)
if exist "H:\Guru\TailScale\tailscale.exe" ("H:\Guru\TailScale\tailscale.exe" funnel status) else (echo     tailscale.exe not found - local page only)

echo.
echo [5/6] PC live lab (own window, keep it open all day)
start "GPLM Live Lab" cmd /k ""%PY%" -m stock_gplm.app.live_lab --port 8765"
echo     waiting for http://127.0.0.1:8765 (a cold start can take a few minutes)
set /a TRIES=0
:wait
set /a TRIES+=1
curl -s -o nul -m 3 http://127.0.0.1:8765/
if not errorlevel 1 goto up
if %TRIES% GEQ 120 goto slow
timeout /t 5 /nobreak >nul
goto wait
:slow
echo     !! lab not answering after 10 min - look at the "GPLM Live Lab" window for the error
goto score
:up
echo     lab is up - opening the page
start "" http://127.0.0.1:8765

:score
echo.
echo [6/6] Live scorecard (refreshes every 5 min; also saved as var\live-lab\^<date^>\scorecard-live.md)
start "GPLM Scorecard" cmd /k ""%PY%" -m stock_gplm.app.live_scorecard --watch 300"

echo.
echo All started. Close the "GPLM Live Lab" window after 15:35 to stop the PC side.
echo End-of-day scorecard any time:  "%PY%" -m stock_gplm.app.live_scorecard
pause
