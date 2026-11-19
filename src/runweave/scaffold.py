"""`runweave init`: copy a starter project (Python or TypeScript) into a folder."""

from importlib.resources import files
from importlib.resources.abc import Traversable
from pathlib import Path
from typing import Literal

from runweave.project import ProjectError

Language = Literal["python", "typescript"]


def _walk(node: Traversable, prefix: Path) -> list[tuple[Path, Traversable]]:
    found: list[tuple[Path, Traversable]] = []
    for child in node.iterdir():
        relative = prefix / child.name
        if child.is_dir():
            found.extend(_walk(child, relative))
        elif child.name != "__pycache__":
            found.append((relative, child))
    return found


def init_project(directory: Path, language: Language, *, force: bool = False) -> list[Path]:
    template = files("runweave").joinpath("templates", language)
    entries = _walk(template, Path())
    clashes = [rel for rel, _ in entries if (directory / rel).exists()]
    if clashes and not force:
        names = ", ".join(str(p) for p in clashes[:5])
        raise ProjectError(f"{directory} already has {names}; use --force to overwrite")
    written = []
    for relative, source in entries:
        target = directory / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes())
        written.append(target)
    return written
