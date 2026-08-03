$ErrorActionPreference = 'Stop'
& .\.venv\Scripts\python.exe -m compileall -q src apps
& .\.venv\Scripts\python.exe -m ruff check src tests apps
& .\.venv\Scripts\python.exe -m ruff format --check src tests apps
& .\.venv\Scripts\python.exe -m mypy src
& .\.venv\Scripts\python.exe -m pytest -q
dotnet test Pangu.sln
