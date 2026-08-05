"""FastAPI host. Importing this module has no runtime side effects."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, HTTPException

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
            "gemini": container.gemini_provider.health(),
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
            intent.canonical_english, intent.intent_name != "informational"
        )
        return value.__dict__

    @app.post("/api/v1/cognition/decide")
    def decide(payload: dict[str, str]) -> dict[str, object]:
        return runtime.decide(payload.get("text", "")).__dict__

    return app
