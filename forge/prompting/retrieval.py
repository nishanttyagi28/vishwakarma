"""Deterministic workspace retrieval for context candidates.

Keyword-occurrence scoring over text files. No embeddings, no index service:
at repository scale (thousands of files at most) a linear scan is fast enough
and fully reproducible, which matters for auditability.
"""

from collections.abc import Iterator
from pathlib import Path

SKIP_DIRS = {
    ".venv", ".git", "node_modules", "__pycache__", ".pytest_cache",
    ".ruff_cache", ".mypy_cache", "runs", "dist", "build", ".ollama",
    "state", ".agenteval",
}
SCAN_SUFFIXES = {".py", ".md", ".yaml", ".yml", ".toml", ".ts", ".tsx", ".json", ".txt"}


def iter_scannable_files(root: Path) -> Iterator[Path]:
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.relative_to(root).parts):
            continue
        if path.suffix.lower() in SCAN_SUFFIXES:
            yield path


def search_workspace(
    root: Path, keywords: list[str], max_results: int = 12
) -> list[tuple[str, int]]:
    """Return (relative_path, score) for the top files matching any keyword."""
    lowered = [k.lower() for k in keywords if k]
    scored: list[tuple[str, int]] = []
    for path in iter_scannable_files(root):
        try:
            text = path.read_text(encoding="utf-8", errors="replace").lower()
        except OSError:
            continue
        score = sum(text.count(k) for k in lowered)
        if score > 0:
            scored.append((path.relative_to(root).as_posix(), score))
    scored.sort(key=lambda item: (-item[1], item[0]))
    return scored[:max_results]
