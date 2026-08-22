from __future__ import annotations

from pathlib import Path


def test_repository_contains_no_private_artifact_paths() -> None:
    root = Path(__file__).resolve().parents[1]
    forbidden_path_parts = {"research", "private_plugins", "strategy_plugins", "secrets"}
    forbidden_suffixes = {".db", ".sqlite", ".pem", ".key"}
    ignored_path_parts = {
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        "__pycache__",
    }
    violations = [
        str(path.relative_to(root))
        for path in root.rglob("*")
        if path.is_file()
        and ".git" not in path.parts
        and not ignored_path_parts.intersection(path.relative_to(root).parts)
        and (
            forbidden_path_parts.intersection(path.relative_to(root).parts)
            or path.suffix.lower() in forbidden_suffixes
        )
    ]
    assert violations == []
