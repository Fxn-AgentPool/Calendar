# Builds dist\CalendarSend.exe from clients\calsend_tray.py (run in PowerShell
# from the repository root). Needs Python 3.10+ from python.org.
$ErrorActionPreference = "Stop"
python -m venv .venv-win
.\.venv-win\Scripts\python -m pip install --upgrade pip
.\.venv-win\Scripts\python -m pip install -r clients\requirements-windows.txt pyinstaller==6.16.0
.\.venv-win\Scripts\pyinstaller --noconfirm --onefile --noconsole --name CalendarSend `
    --paths clients --hidden-import pystray._win32 clients\calsend_tray.py
Write-Host "Built dist\CalendarSend.exe - start it once, then enable 'Start with Windows' in Settings."
