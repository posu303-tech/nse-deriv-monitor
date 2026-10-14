@echo off
REM NSE Derivatives Monitor - one-click run (full fetch + report)
cd /d "%~dp0"
python nse_deriv_monitor.py %*
if errorlevel 1 (
    echo.
    echo [ERROR] Monitor failed - see output above.
) else (
    echo.
    echo Done. Reports in "%~dp0output", history in "%~dp0data".
)
pause
