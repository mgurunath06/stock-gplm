@echo off
rem GPLM correctness-verification suite: synthetic fixtures only, about a minute on CPU.
rem Writes VERIFY_REPORT.md (+ CSV/JSON evidence) under <home>\reports\verify\<UTC timestamp>.
rem Exit code 0 = PASS (warnings allowed), 1 = at least one FAIL.
rem Usage: windows\verify.cmd [--out DIR] [--json]
call "%~dp0gplm.cmd" verify %*
exit /b %ERRORLEVEL%
