@echo off
REM ============================================================
REM  Bank Branch Operations Monitoring - one-click reproduce
REM
REM  This file is intentionally ASCII-ONLY.
REM  Reason: cmd.exe parses .bat files using the console code page.
REM  Chinese text inside a .bat file breaks parsing on some machines
REM  (observed: "'xx' is not recognized as an internal or external
REM  command"), and that happens before chcp can take effect.
REM  Chinese documentation lives in README.md and run_all.ps1.
REM
REM  Python selection: prefer the interpreter that actually has the
REM  dependencies. On this machine the system Python 3.13 lacks
REM  duckdb/dotenv while the runtime Python has them, so picking
REM  "whatever `python` resolves to" silently breaks step 6.
REM ============================================================
setlocal
cd /d "%~dp0"

echo ============================================================
echo  Bank Branch Operations Monitoring - reproduce pipeline
echo ============================================================
echo.

REM Interpreter selection: try candidates in order, pick the first one
REM that actually has the dependencies. Do NOT just use whatever `python`
REM resolves to -- a bare system Python may lack duckdb/dotenv and will
REM silently break step 6.
REM
REM Portability: set OPS_PYTHON to your own interpreter to override.
REM The second candidate is the DSH bundled runtime path used during
REM development; it simply will not exist on other machines, which is fine.
set "PY="
if defined OPS_PYTHON (
  if exist "%OPS_PYTHON%" (
    "%OPS_PYTHON%" -c "import duckdb" >nul 2>nul
    if not errorlevel 1 set "PY=%OPS_PYTHON%"
  )
)
set "RUNTIME_PY=%USERPROFILE%\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe"
if not defined PY (
  if exist "%RUNTIME_PY%" (
    "%RUNTIME_PY%" -c "import duckdb" >nul 2>nul
    if not errorlevel 1 set "PY=%RUNTIME_PY%"
  )
)
if not defined PY (
  where python >nul 2>nul
  if not errorlevel 1 set "PY=python"
)
if not defined PY (
  where py >nul 2>nul
  if not errorlevel 1 set "PY=py"
)
if not defined PY (
  echo [ERROR] No usable Python found.
  echo         Set OPS_PYTHON to your interpreter, e.g.
  echo           set OPS_PYTHON=C:\Python313\python.exe
  exit /b 1
)
echo Python: %PY%
"%PY%" -c "import duckdb" >nul 2>nul
if errorlevel 1 (
  echo [WARN] This Python has no duckdb; step 6 will fail.
  echo        Install with: "%PY%" -m pip install duckdb pymysql python-dotenv
)
echo.

echo [1/8] Environment self-check ...
"%PY%" src\py\check_env.py
if errorlevel 1 echo      (not ready - see FAIL above; later steps may fail)
echo.

echo [2/8] Generate simulated data (with injection self-check) ...
"%PY%" src\py\gen_operation_data.py || goto fail
echo.

echo [3/8] Detect anomalies, grade, classify, evaluate ...
"%PY%" src\py\analyze_ops.py || goto fail
echo.

echo [4/8] Build interactive dashboard ...
"%PY%" src\py\make_dashboard.py || goto fail
echo.

echo [5/8] Generate operations report ...
"%PY%" src\py\gen_report.py || goto fail
echo.

echo [6/8] Load into database and EXECUTE the SQL indicator layer ...
"%PY%" src\py\load_to_db.py || goto fail
echo.

echo [7/8] Build single-file web agent (Text-to-Excel) ...
"%PY%" src\py\build_web_agent.py || goto fail
echo.

echo [8/8] Verify web agent JS in Node sandbox ...
where node >nul 2>nul
if errorlevel 1 (
  echo      (node not found - skipping JS verification^)
) else (
  node src\py\verify_agent.js || goto fail
)
echo.

echo [*] Convert reports to Word ...
REM Do not pass Chinese filenames as arguments: the console code page
REM conversion corrupts them. Let the script discover the files itself.
"%PY%" src\py\md2docx.py --all || goto fail
echo.

echo ============================================================
echo  DONE. Deliverables:
echo    results\  dashboard html + operations report docx
echo    docs\     method docx
echo    tools\    pingan_ops.duckdb (5 tables loaded)
echo ============================================================
goto :eof

:fail
echo.
echo *** FAILED - see the error message above ***
exit /b 1
