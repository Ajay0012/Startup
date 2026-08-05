"""Optional FastAPI host; install project dependencies first."""

from pathlib import Path

from pangu.runtime import build_runtime

try:
    from fastapi import FastAPI, Header, HTTPException
except ImportError as error:
    raise SystemExit("Install dependencies with scripts/bootstrap.ps1") from error
runtime = build_runtime(Path(__file__).resolve().parents[2])
app = FastAPI(title="PANGU local API")


@app.on_event("startup")
def startup() -> None:
    runtime.start()


@app.on_event("shutdown")
def shutdown() -> None:
    runtime.stop()


@app.get("/health")
def health() -> dict[str, object]:
    database = runtime.db.health_details()
    return {
        "status": "ready" if database["database_ready"] else "degraded",
        "provider": "available" if runtime.settings.gemini_key_present else "degraded",
        "bind": "loopback-only",
        "database": database,
    }


@app.get("/ready")
def ready() -> dict[str, object]:
    database = runtime.db.health_details()
    if not database["database_ready"]:
        raise HTTPException(503, "database is not ready")
    return {"status": "ready", "database": database}


@app.post("/v1/commands")
def command(
    payload: dict[str, str], authorization: str | None = Header(default=None)
) -> dict[str, object]:
    if authorization != "Bearer local-development":
        raise HTTPException(401, "unauthorized")
    result = runtime.command(payload.get("text", ""), "local_api")
    return {"status": result.status, "message": result.message, "evidence": result.evidence}
