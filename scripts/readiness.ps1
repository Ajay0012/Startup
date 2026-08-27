$ErrorActionPreference = 'Stop'

if (-not (Test-Path '.\.venv\Scripts\python.exe')) {
    Write-Error 'PANGU virtual environment is missing. Run .\scripts\bootstrap.ps1 first.'
}

& .\.venv\Scripts\python.exe -c "import json; from pangu.readiness import PanguReadinessInspector; from pangu.settings import resolve_application_root; root=resolve_application_root(); report=PanguReadinessInspector(root).inspect(); print(json.dumps(report.public(), indent=2, default=str)); raise SystemExit(0 if report.ready else 2)"
