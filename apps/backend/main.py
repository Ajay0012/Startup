"""FastAPI host. Importing this module has no runtime side effects."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from pangu.runtime_builder import ServiceContainer


def create_app(container: ServiceContainer) -> FastAPI:
    runtime = container.runtime

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        await runtime.start_async()
        try:
            yield
        finally:
            await runtime.stop_async()

    app = FastAPI(title="PANGU local API", lifespan=lifespan)

    class ApplicationRequest(BaseModel):
        name: str
        approval_token: str | None = None

    @app.get("/health")
    def health() -> dict[str, object]:
        database = runtime.db.health_details()
        return {
            "status": "ready" if database["database_ready"] else "degraded",
            "database": database,
            "models": {"gemini": container.gemini_provider.health()},
        }

    @app.get("/ready")
    def ready() -> dict[str, object]:
        database = runtime.db.health_details()
        if not database["database_ready"]:
            raise HTTPException(503, "database is not ready")
        return {"status": "ready", "database": database}

    @app.get("/api/v1/models/health")
    def model_health() -> dict[str, object]:
        return {
            "deterministic": container.deterministic_provider.health(),
            "gemini": container.gemini_provider.health_details(),
        }

    @app.get("/api/v1/models")
    def models() -> dict[str, object]:
        return {
            "capabilities": [
                capability.__dict__ for capability in container.model_capabilities.all()
            ]
        }

    @app.post("/api/v1/language/normalize")
    def normalize(payload: dict[str, str]) -> dict[str, object]:
        return runtime.language.normalize(payload.get("text", "")).__dict__

    @app.post("/api/v1/context/sanitize")
    def sanitize(payload: dict[str, str]) -> dict[str, object]:
        decision = container.sanitizer.sanitize(payload.get("text", ""))
        return {
            "outcome": decision.outcome,
            "sanitized_content": decision.sanitized_content,
            "redactions": decision.redactions,
            "original_hash": decision.original_hash,
            "sanitized_hash": decision.sanitized_hash,
        }

    @app.post("/api/v1/models/route")
    def route(payload: dict[str, str]) -> dict[str, object]:
        intent = runtime.language.normalize(payload.get("text", ""))
        value = container.model_router.route(
            intent.canonical_english,
            intent.intent_name != "informational",
            payload.get("kind", "text"),
        )
        return value.__dict__

    @app.post("/api/v1/cognition/decide")
    def decide(payload: dict[str, str]) -> dict[str, object]:
        return runtime.decide(payload.get("text", "")).__dict__

    @app.get("/api/v1/applications")
    def applications() -> dict[str, object]:
        return {"applications": [item.public() for item in runtime.list_applications()]}

    @app.post("/api/v1/applications/discover")
    def discover_applications() -> dict[str, object]:
        return {"applications": [item.public() for item in runtime.discover_applications()]}

    @app.post("/api/v1/applications/refresh")
    def refresh_applications() -> dict[str, object]:
        return {"applications": [item.public() for item in runtime.refresh_applications()]}

    @app.post("/api/v1/applications/resolve")
    def resolve_application(payload: ApplicationRequest) -> dict[str, object]:
        return runtime.resolve_application(payload.name).__dict__

    @app.post("/api/v1/applications/{operation}")
    def application_operation(operation: str, payload: ApplicationRequest) -> dict[str, object]:
        methods = {
            "open": runtime.open_application,
            "focus": runtime.focus_application,
            "minimize": runtime.minimize_application,
            "maximize": runtime.maximize_application,
            "restore": runtime.restore_application,
            "close": runtime.close_application,
            "restart": runtime.restart_application,
        }
        if operation not in methods:
            raise HTTPException(404, "Unknown application operation")
        method = methods[operation]
        result = (
            method(payload.name, payload.approval_token)
            if operation in {"close", "restart"}
            else method(payload.name)
        )
        return result.__dict__

    @app.get("/api/v1/applications/{application_id}/status")
    def application_status(application_id: str) -> dict[str, object]:
        app_record = container.application_catalog.get(application_id)
        if app_record is None:
            raise HTTPException(404, "Application not found")
        return runtime.application_status(app_record.display_name).__dict__

    @app.get("/api/v1/applications/{application_id}/windows")
    def application_windows(application_id: str) -> dict[str, object]:
        app_record = container.application_catalog.get(application_id)
        if app_record is None:
            raise HTTPException(404, "Application not found")
        return {
            "windows": [
                item.__dict__ for item in runtime.list_application_windows(app_record.display_name)
            ]
        }

    return app
