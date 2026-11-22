@echo off
rem Owner 2026-11-22: discovery round 2, one job at a time (memory), output on screen; per-run progress.log keeps
rem the record. 1) move-first discovery (see a move -> build a strategy) on 1d, 15m, 5m, 60m; 2) probe discovery
rem with event features + 3-condition combinations (discover2) on 1d and 15m. Resumable - rerun to continue.
rem Run it AFTER the tuning chain has finished (or stop the chain first).
cd /d H:\Guru\Learning\StockGLPM\app
set PYTHONPATH=src
set PYTHONIOENCODING=utf-8
set PY=E:\Guru\Learning\Alekhya\venv\Scripts\python.exe
for %%T in (1d 15m 5m 60m) do (
  echo ==== %date% %time% moves %%T
  %PY% -m stock_gplm.app.discover --tf %%T --mode moves --workers 16
)
for %%T in (1d 15m) do (
  echo ==== %date% %time% discover2 %%T
  %PY% -m stock_gplm.app.discover --tf %%T --name discover2-%%T --workers 16
)
echo ==== %date% %time% discovery round 2 done
