$ErrorActionPreference = 'Stop'

py -3.12 -m venv .venv
& .\.venv\Scripts\python.exe -m pip install --upgrade pip
& .\.venv\Scripts\python.exe -m pip install -e '.[dev,browser,desktop,vision]'
& .\.venv\Scripts\python.exe -m playwright install chromium

Write-Host 'PANGU Python, desktop, vision, browser, and Chromium dependencies installed.'
Write-Host 'Run .\scripts\readiness.ps1 next. It will report external model/binary/API/phone setup still required.'
