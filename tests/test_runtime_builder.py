from pathlib import Path

from pangu.runtime_builder import RuntimeBuilder


def test_runtime_builder_has_one_explicit_service_owner(tmp_path: Path) -> None:
    container = RuntimeBuilder(tmp_path).build()
    assert container.database.path == tmp_path / "runtime-data" / "database" / "pangu.db"
    assert container.model_router.deterministic is container.deterministic_provider
    assert container.model_router.gemini is container.gemini_provider
