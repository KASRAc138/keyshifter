@echo off
cd /d "%~dp0"
pyinstaller --noconfirm --onefile --noconsole --name KeyShift KeyShift.pyw
echo.
echo Done: dist\KeyShift.exe
pause
