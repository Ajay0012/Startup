import os
from pathlib import Path

from pangu.contracts import Status
from pangu.runtime import build_runtime


def test_create_folder_is_verified(tmp_path: Path) -> None:
    os.environ["PANGU_RUNTIME_ROOT"] = str(tmp_path / "runtime")
    runtime = build_runtime(tmp_path)
    runtime.start()
    try:
        result = runtime.command("create folder reports")
        assert result.status == Status.VERIFIED
        assert (tmp_path / "reports").is_dir()
    finally:
        runtime.stop()


def test_tanglish_normalizes_and_is_safe(tmp_path: Path) -> None:
    os.environ["PANGU_RUNTIME_ROOT"] = str(tmp_path / "runtime")
    runtime = build_runtime(tmp_path)
    runtime.start()
    try:
        assert (
            runtime.language.normalize("Chrome ah open pannu").canonical_english
            == "Open Google Chrome"
        )
    finally:
        runtime.stop()
