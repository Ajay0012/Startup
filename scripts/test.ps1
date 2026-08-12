$ErrorActionPreference = 'Stop'

function Invoke-Checked {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Name,
        [Parameter(Mandatory = $true)]
        [scriptblock]$Command
    )

    & $Command
    if ($LASTEXITCODE -ne 0) {
        throw "$Name failed with exit code $LASTEXITCODE"
    }
}

Invoke-Checked "compileall" { .\.venv\Scripts\python.exe -m compileall -q src apps }
Invoke-Checked "ruff check" { .\.venv\Scripts\python.exe -m ruff check src tests apps }
Invoke-Checked "ruff format" { .\.venv\Scripts\python.exe -m ruff format --check src tests apps }
Invoke-Checked "mypy" { .\.venv\Scripts\python.exe -m mypy src }
Invoke-Checked "pytest" { .\.venv\Scripts\python.exe -m pytest -q }
Invoke-Checked "dotnet test" { dotnet test Pangu.sln }
