from __future__ import annotations

import hashlib
import shutil
from pathlib import Path


class FilesystemAdapter:
    def __init__(self, allowed_root: Path) -> None:
        self.root = allowed_root.resolve()

    def resolve(self, relative_path: str) -> Path:
        candidate = (self.root / relative_path).resolve()
        try:
            candidate.relative_to(self.root)
        except ValueError as error:
            raise PermissionError("path escapes allowed root") from error
        if candidate.is_symlink():
            raise PermissionError("symlink targets are not allowed")
        return candidate

    def create_folder(self, relative_path: str) -> tuple[Path, bool]:
        target = self.resolve(relative_path)
        target.mkdir(parents=True, exist_ok=True)
        return target, target.is_dir()

    def write_text(
        self, relative_path: str, content: str, overwrite: bool = False
    ) -> tuple[Path, str]:
        target = self.resolve(relative_path)
        if target.exists() and not overwrite:
            raise FileExistsError("refusing to overwrite without explicit policy")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        verified = target.read_text(encoding="utf-8") == content
        if not verified:
            raise OSError("write postcondition failed")
        return target, hashlib.sha256(content.encode()).hexdigest()

    def list_directory(self, relative_path: str = ".") -> list[str]:
        target = self.resolve(relative_path)
        return sorted(item.name for item in target.iterdir())

    def recycle(self, relative_path: str) -> Path:
        target = self.resolve(relative_path)
        if not target.exists():
            raise FileNotFoundError(target)
        recycle = self.root / ".pangu-recycle"
        recycle.mkdir(exist_ok=True)
        destination = recycle / target.name
        if destination.exists():
            raise FileExistsError("recycle destination exists")
        shutil.move(str(target), str(destination))
        if not destination.exists() or target.exists():
            raise OSError("recycle postcondition failed")
        return destination
