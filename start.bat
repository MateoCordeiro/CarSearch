@echo off
REM Force UTF-8 console so status output (checkmarks, dashes) never crashes on
REM a legacy Windows code page. config.py also guards this in-process.
set PYTHONIOENCODING=utf-8

echo === BumperScraper ===
echo.

echo Installing dependencies...
pip install -r requirements.txt
if errorlevel 1 (
    echo.
    echo ERROR: Some packages failed to install.
    echo This project targets pure-Python deps and runs on Python 3.9-3.14.
    echo Your current Python version:
    python --version
    pause
    exit /b 1
)

echo.
echo Preparing offline data ^(first run downloads ~2MB of ZIP coordinates^)...
python bootstrap.py

echo.
echo Starting server at http://localhost:5000
echo Press Ctrl+C to stop.
echo.
python app.py
pause
