$ErrorActionPreference = 'Stop'
py -3.12 -m venv .venv
& .\.venv\Scripts\python.exe -m pip install --upgrade pip
& .\.venv\Scripts\python.exe -m pip install -e '.[dev,browser]'
& .\.venv\Scripts\python.exe -m playwright install chromium
Write-Host 'PANGU bootstrap complete with isolated Chromium media/browser runtime.'
