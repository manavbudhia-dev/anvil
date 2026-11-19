@echo off
REM Builds dist\Anvil.exe. Double-click this file on Windows (needs Python 3.10+ from python.org).
setlocal
cd /d "%~dp0"

where py >nul 2>nul && (set PY=py -3) || (set PY=python)
%PY% --version >nul 2>nul || (
  echo Python was not found. Install it from https://www.python.org/downloads/ and tick "Add python.exe to PATH".
  pause & exit /b 1
)

echo Creating build environment...
%PY% -m venv .build-venv || (pause & exit /b 1)
call .build-venv\Scripts\activate.bat
python -m pip install --upgrade pip >nul
python -m pip install . pywebview playwright pyinstaller || (pause & exit /b 1)

echo Building Anvil.exe...
pyinstaller --noconfirm --clean --onefile --windowed --name Anvil ^
  --icon packaging\anvil.ico --paths . ^
  --add-data "anvil\gui\static;anvil\gui\static" ^
  --add-data "anvil\builtin_skills;anvil\builtin_skills" ^
  --collect-all webview ^
  --collect-all playwright ^
  packaging\anvil_desktop.py || (pause & exit /b 1)

echo.
echo Done: %CD%\dist\Anvil.exe
echo You can move Anvil.exe anywhere (for example your Desktop) and double-click it.
pause
