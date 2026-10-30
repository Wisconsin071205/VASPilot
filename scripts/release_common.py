"""Read release identity from the source package without importing an install."""
from __future__ import annotations

import ast
from pathlib import Path


def release_version(root: Path) -> str:
    source = root / "src" / "rsci" / "__init__.py"
    for node in ast.parse(source.read_text(encoding="utf-8-sig")).body:
        if isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == "__version__"
                for target in node.targets):
            value = ast.literal_eval(node.value)
            if isinstance(value, str) and value:
                return value
    raise ValueError(f"No release version in {source}")


def archive_name(version: str) -> str:
    return f"rsci_{version}_智能体可部署程序.zip"
