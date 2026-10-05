"""Bounded, project-local cleanup for transient order-processing files."""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


DEBUG_DIRECTORIES = (
    "artifacts",
    "tmp/supplier2_debug",
    "tmp/supplier3_debug",
    "tmp/supplier4_debug",
    "pages",
)
PRESERVED_FILES = {"artifacts/.gitkeep", "artifacts/cart_sample_ok.html", "artifacts/cart_sample_bad.html"}
ROOT_RUNTIME_PATTERNS = (
    "step*.png",
    "supplier*_failed.png",
    "supplier*_login_failed.png",
    "supplier*_add_items_failed.png",
    "dobavki_success_*.png",
)


@dataclass(frozen=True)
class CleanupResult:
    deleted_files: int = 0
    deleted_bytes: int = 0

    def __add__(self, other: "CleanupResult") -> "CleanupResult":
        return CleanupResult(self.deleted_files + other.deleted_files, self.deleted_bytes + other.deleted_bytes)


def _enabled() -> bool:
    return (os.getenv("RUNTIME_CLEANUP_ON_START") or "1").strip().lower() not in {"0", "false", "no", "off"}


def labels_keep_last() -> int:
    try:
        return max(0, int((os.getenv("RUNTIME_LABELS_KEEP_LAST") or "3").strip()))
    except ValueError:
        return 3


def _inside_root(root: Path, path: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _remove_file(root: Path, path: Path) -> CleanupResult:
    if path.is_symlink() or not path.is_file() or not _inside_root(root, path):
        return CleanupResult()
    try:
        size = path.stat().st_size
        path.unlink()
        return CleanupResult(1, size)
    except OSError:
        return CleanupResult()


def clear_directory(root: Path, relative: str) -> CleanupResult:
    """Delete files below one known project-local transient directory."""
    directory = root / relative
    if directory.is_symlink() or not directory.is_dir() or not _inside_root(root, directory):
        return CleanupResult()
    result = CleanupResult()
    for path in sorted(directory.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        try:
            if path.relative_to(root).as_posix() in PRESERVED_FILES:
                continue
        except ValueError:
            continue
        if path.is_symlink():
            continue
        if path.is_file():
            result += _remove_file(root, path)
        elif path.is_dir():
            try:
                path.rmdir()
            except OSError:
                pass
    return result


def retain_newest(root: Path, directory: Path, files: Iterable[Path], keep: int | None = None) -> CleanupResult:
    """Keep newest regular files, never following symlinks or leaving *root*."""
    keep = labels_keep_last() if keep is None else max(0, keep)
    candidates = [p for p in files if p.is_file() and not p.is_symlink() and _inside_root(root, p)]
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    result = CleanupResult()
    for path in candidates[keep:]:
        result += _remove_file(root, path)
    return result


def retain_sportatlet_sets(root: Path, directory: Path, keep: int | None = None) -> CleanupResult:
    """Retain newest TTN-based PDF/XLSX pairs as a single logical artifact."""
    keep = labels_keep_last() if keep is None else max(0, keep)
    groups: dict[str, list[Path]] = {}
    for path in directory.glob("*") if directory.is_dir() else ():
        if not path.is_file() or path.is_symlink() or not _inside_root(root, path):
            continue
        key = path.stem.removeprefix("marking-")
        if path.suffix.lower() in {".pdf", ".xlsx"}:
            groups.setdefault(key, []).append(path)
    newest = sorted(groups.items(), key=lambda item: max(p.stat().st_mtime for p in item[1]), reverse=True)
    result = CleanupResult()
    for _, paths in newest[keep:]:
        for path in paths:
            result += _remove_file(root, path)
    return result


def cleanup_startup(root: Path, *, include_exports: bool = False) -> CleanupResult:
    if not _enabled():
        return CleanupResult()
    result = CleanupResult()
    for relative in DEBUG_DIRECTORIES:
        result += clear_directory(root, relative)
    for pattern in ROOT_RUNTIME_PATTERNS:
        for path in root.glob(pattern):
            result += _remove_file(root, path)
    label_dirs = ("supplier2_labels", "supplier3_labels", "supplier4_labels", "supplier7_labels", "zoohub_labels", "vitaworld_labels")
    for relative in label_dirs:
        directory = root / relative
        result += retain_newest(root, directory, directory.glob("*.pdf"))
    result += retain_sportatlet_sets(root, root / "sportatlet_files")
    if include_exports:
        export_dir = root / "exports" / "dobavki"
        result += retain_newest(root, export_dir, (p for p in export_dir.glob("*") if p.name != "dobavki_products.json"), keep=0)
    if result.deleted_files:
        print(f"[CLEANUP] removed {result.deleted_files} transient files ({result.deleted_bytes / 1024 / 1024:.1f} MiB)")
    return result
