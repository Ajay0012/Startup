from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class CodeSymbol:
    path: str
    name: str
    kind: str
    line: int
    references: tuple[str, ...]


@dataclass(frozen=True)
class ImpactAnalysis:
    changed_paths: tuple[str, ...]
    dependent_paths: tuple[str, ...]
    suggested_tests: tuple[str, ...]
    risk_score: float
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class FailureDiagnosis:
    category: str
    likely_paths: tuple[str, ...]
    summary: str
    confidence: float


class RepositorySemanticIndex:
    """Local AST/text index for targeted self-upgrade planning."""

    def __init__(self, root: Path, *, max_file_bytes: int = 300_000) -> None:
        self.root = root.resolve()
        self.max_file_bytes = max_file_bytes
        self.symbols: list[CodeSymbol] = []
        self.importers: dict[str, set[str]] = {}

    def build(self) -> None:
        self.symbols.clear()
        self.importers.clear()
        src = self.root / "src" / "pangu"
        if not src.is_dir():
            raise RuntimeError("PANGU source directory is unavailable")
        for path in sorted(src.rglob("*.py")):
            if path.stat().st_size > self.max_file_bytes:
                continue
            relative = path.relative_to(self.root).as_posix()
            text = path.read_text(encoding="utf-8", errors="replace")
            try:
                tree = ast.parse(text, filename=relative)
            except SyntaxError:
                continue
            module = ".".join(path.relative_to(src).with_suffix("").parts)
            for node in ast.walk(tree):
                if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                    refs = sorted(
                        {
                            child.id
                            for child in ast.walk(node)
                            if isinstance(child, ast.Name) and child.id != node.name
                        }
                    )[:64]
                    self.symbols.append(
                        CodeSymbol(
                            relative,
                            node.name,
                            "class" if isinstance(node, ast.ClassDef) else "function",
                            node.lineno,
                            tuple(refs),
                        )
                    )
                elif isinstance(node, ast.ImportFrom):
                    imported = node.module or ""
                    if node.level and imported:
                        imported = imported.split(".")[-1]
                    if imported:
                        self.importers.setdefault(imported, set()).add(relative)
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        self.importers.setdefault(alias.name, set()).add(relative)
            self.importers.setdefault(module, set())

    @staticmethod
    def _tokens(query: str) -> set[str]:
        return {token for token in re.findall(r"[a-z0-9_]+", query.casefold()) if len(token) > 2}

    def search(self, query: str, *, limit: int = 20) -> tuple[CodeSymbol, ...]:
        wanted = self._tokens(query)
        ranked: list[tuple[float, CodeSymbol]] = []
        for symbol in self.symbols:
            haystack = f"{symbol.path} {symbol.name} {' '.join(symbol.references)}".casefold()
            score = sum(1.0 for token in wanted if token in haystack)
            if symbol.name.casefold() in query.casefold():
                score += 2.0
            if score:
                ranked.append((score, symbol))
        ranked.sort(key=lambda item: (-item[0], item[1].path, item[1].line))
        return tuple(symbol for _, symbol in ranked[:limit])

    def impact(self, changed_paths: tuple[str, ...]) -> ImpactAnalysis:
        changed = tuple(dict.fromkeys(changed_paths))
        dependent: set[str] = set()
        tests: set[str] = set()
        reasons: list[str] = []
        for path in changed:
            stem = Path(path).stem
            for module, importers in self.importers.items():
                if module.endswith(stem):
                    dependent.update(importers)
            direct_test = self.root / "tests" / f"test_{stem}.py"
            if direct_test.is_file():
                tests.add(direct_test.relative_to(self.root).as_posix())
            for test in (self.root / "tests").glob("test_*.py"):
                try:
                    text = test.read_text(encoding="utf-8", errors="ignore")
                except OSError:
                    continue
                if stem in text:
                    tests.add(test.relative_to(self.root).as_posix())
        dependent.difference_update(changed)
        risk = min(1.0, len(changed) * 0.08 + len(dependent) * 0.025)
        if any(
            path.endswith(("runtime.py", "runtime_builder.py", "database.py")) for path in changed
        ):
            risk = min(1.0, risk + 0.3)
            reasons.append("core composition/runtime path changed")
        if any(
            path.endswith(("voice.py", "realtime_voice.py", "computer_use.py")) for path in changed
        ):
            risk = min(1.0, risk + 0.2)
            reasons.append("hardware/action critical path changed")
        if len(changed) > 6:
            reasons.append("wide change set")
        return ImpactAnalysis(
            changed, tuple(sorted(dependent)), tuple(sorted(tests)), risk, tuple(reasons)
        )


class FailureLogDiagnoser:
    _patterns = (
        (re.compile(r"mypy|incompatible type|has no attribute", re.I), "typing"),
        (re.compile(r"ruff|format|line too long|unused import", re.I), "lint"),
        (re.compile(r"assertionerror|failed\s+tests?|pytest", re.I), "test"),
        (re.compile(r"dotnet|cs\d{4}|build failed", re.I), "dotnet"),
        (re.compile(r"timeout|timed out", re.I), "timeout"),
        (re.compile(r"modulenotfounderror|importerror", re.I), "dependency"),
    )
    _path = re.compile(r"(?:[A-Za-z]:\\|/)?(?:[\w.-]+[/\\])+[\w.-]+\.(?:py|cs|ps1|toml)")

    def diagnose(self, log: str) -> FailureDiagnosis:
        excerpt = log[-40_000:]
        category = "unknown"
        confidence = 0.35
        for pattern, label in self._patterns:
            if pattern.search(excerpt):
                category = label
                confidence = 0.82
                break
        paths = tuple(
            dict.fromkeys(
                match.group(0).replace("\\", "/") for match in self._path.finditer(excerpt)
            )
        )[:12]
        summary = f"Detected {category} failure"
        if paths:
            summary += f" involving {', '.join(paths[:4])}"
        return FailureDiagnosis(category, paths, summary, confidence)
