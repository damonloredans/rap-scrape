@echo off
setlocal
cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" (set "PY=.venv\Scripts\python.exe") else (set "PY=python")

if not exist "data\catalog.json" (
    echo.
    echo   First run: downloading the catalogue ^(about a minute^)...
    %PY% scraparts.py sync
    if errorlevel 1 goto :fail
)

echo.
echo   scraparts - opening the search page. Close this window to stop.
%PY% scraparts.py serve --open
goto :end

:fail
echo.
echo   sync failed - see the message above.

:end
echo.
pause
