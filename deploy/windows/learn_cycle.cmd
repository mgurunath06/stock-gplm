@echo off
rem Learning cycle (owner 2026-11-21), run by Task Scheduler outside market hours. Arg: daily | weekly | monthly
cd /d h:\Guru\Learning\StockGLPM\app
set PYTHONPATH=src
set PYTHONIOENCODING=utf-8
"E:\Guru\Learning\Alekhya\venv\Scripts\python.exe" -m stock_gplm.app.learn_cycle %1 --workers 20 >> "H:\Guru\Learning\StockGLPM\state\learning\task-%1.log" 2>&1
