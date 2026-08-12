from fnmatch import fnmatch
from pathlib import Path


class Workspace:
    def __init__(self, root: str | Path) -> None:
        root = Path(root).expanduser()
        if not root.is_absolute():
            root = Path.cwd() / root
        self.root = root.resolve(strict=True)
        if not self.root.is_dir():
            raise ValueError("workspace root must be a directory")

    def resolve(self, relative: str, *, must_exist: bool = True) -> Path:
        candidate = Path(relative)
        if candidate.is_absolute():
            raise PermissionError("absolute paths are not allowed")
        resolved = (self.root / candidate).resolve(strict=must_exist)
        if not resolved.is_relative_to(self.root):
            raise PermissionError("path escapes the authorized workspace")
        return resolved

    def discover(self, include: list[str] | None = None, recursive: bool = True) -> list[Path]:
        patterns = include or ["*.csv", "*.parquet"]
        iterator = self.root.rglob("*") if recursive else self.root.glob("*")
        return sorted(p for p in iterator if p.is_file() and any(fnmatch(p.name, x) for x in patterns))

