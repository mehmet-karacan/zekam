from __future__ import annotations

import ast
from pathlib import Path


def test_application_layer_imports_no_concrete_database_adapter() -> None:
    application = Path(__file__).resolve().parents[2] / "src" / "zekam" / "application"
    forbidden = ("zekam.infrastructure.postgres", "zekam.infrastructure.sqlite")
    violations: list[str] = []
    for path in sorted(application.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            modules: tuple[str, ...] = ()
            if isinstance(node, ast.ImportFrom) and node.module is not None:
                modules = (node.module,)
            elif isinstance(node, ast.Import):
                modules = tuple(alias.name for alias in node.names)
            for module in modules:
                if module.startswith(forbidden):
                    violations.append(f"{path.relative_to(application)}:{node.lineno}:{module}")
    assert violations == []
