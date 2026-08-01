#!/usr/bin/env python3
"""Prune redundant runtime snapshots without touching sealed research evidence."""

from __future__ import annotations

import argparse
import shutil
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Deletion:
    path: Path
    size: int
    mtime_ns: int
    inode: int
    reason: str


RUNTIME_DIRECTORIES = {
    "root": Path("."),
    "updown": Path("updown"),
    "forward": Path("forward"),
    "microstructure": Path("microstructure"),
}


def build_deletion_plan(
    backup_root: Path,
    *,
    retentions: dict[str, int],
    artifact_grace_seconds: float,
    deploy_retention: int,
    deploy_grace_seconds: float,
    now: float | None = None,
) -> list[Deletion]:
    """Build an allow-listed plan; sealed final/training/OOS trees are excluded."""
    if artifact_grace_seconds < 0:
        raise ValueError("artifact grace must be non-negative")
    if deploy_grace_seconds < 0:
        raise ValueError("deploy grace must be non-negative")
    root = _validated_root(backup_root)
    current_time = time.time() if now is None else now
    planned: dict[Path, Deletion] = {}

    for name, relative in RUNTIME_DIRECTORIES.items():
        retention = retentions[name]
        if retention < 0:
            raise ValueError(f"{name} retention must be non-negative")
        directory = root if relative == Path(".") else root / relative
        if not directory.exists():
            continue
        _require_real_directory(directory, root)
        snapshots = _matching_files(directory, "crypto-threshold-*.db")
        for stale in snapshots[retention:]:
            planned[stale] = _deletion(stale, f"{name}_snapshot_beyond_retention")

        for pattern in (
            "crypto-threshold-*.db.partial*",
            "crypto-threshold-*.db-wal",
            "crypto-threshold-*.db-shm",
        ):
            for artifact in _matching_files(directory, pattern):
                age_seconds = current_time - artifact.stat().st_mtime
                if age_seconds >= artifact_grace_seconds:
                    planned[artifact] = _deletion(artifact, f"{name}_sqlite_artifact")

    if deploy_retention < 0:
        raise ValueError("deploy retention must be non-negative")
    deploy_directory = root / "deploy"
    if deploy_directory.exists():
        _require_real_directory(deploy_directory, root)
        archives = _matching_files(deploy_directory, "*.tar.gz")
        for stale in archives[deploy_retention:]:
            age_seconds = current_time - stale.stat().st_mtime
            if age_seconds >= deploy_grace_seconds:
                planned[stale] = _deletion(stale, "deploy_archive_beyond_retention")

    return sorted(planned.values(), key=lambda item: str(item.path))


def apply_deletion_plan(plan: list[Deletion]) -> int:
    """Delete only unchanged regular files from a freshly constructed plan."""
    deleted_bytes = 0
    for deletion in plan:
        try:
            stat = deletion.path.lstat()
        except FileNotFoundError:
            continue
        if deletion.path.is_symlink() or not deletion.path.is_file():
            raise RuntimeError(f"refusing non-regular deletion target: {deletion.path}")
        if (
            stat.st_ino != deletion.inode
            or stat.st_size != deletion.size
            or stat.st_mtime_ns != deletion.mtime_ns
        ):
            raise RuntimeError(f"deletion target changed after planning: {deletion.path}")
        deletion.path.unlink()
        deleted_bytes += deletion.size
    return deleted_bytes


def _validated_root(path: Path) -> Path:
    expanded = path.expanduser()
    if expanded.is_symlink():
        raise RuntimeError(f"refusing symlink storage root: {expanded}")
    root = expanded.resolve(strict=True)
    _require_real_directory(root, root)
    return root


def _require_real_directory(path: Path, root: Path) -> None:
    if path.is_symlink() or not path.is_dir():
        raise RuntimeError(f"refusing non-directory storage path: {path}")
    if path != root and path.parent != root:
        raise RuntimeError(f"storage path escapes the allow-listed root: {path}")


def _matching_files(directory: Path, pattern: str) -> list[Path]:
    matches: list[Path] = []
    for path in directory.glob(pattern):
        if path.is_symlink():
            raise RuntimeError(f"refusing symlink in managed storage: {path}")
        if path.is_file():
            matches.append(path)
    return sorted(matches, key=lambda path: path.name, reverse=True)


def _deletion(path: Path, reason: str) -> Deletion:
    stat = path.stat()
    return Deletion(
        path=path,
        size=stat.st_size,
        mtime_ns=stat.st_mtime_ns,
        inode=stat.st_ino,
        reason=reason,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backup-root", required=True, type=Path)
    parser.add_argument("--root-retention", type=int, default=0)
    parser.add_argument("--updown-retention", type=int, default=1)
    parser.add_argument("--forward-retention", type=int, default=1)
    parser.add_argument("--microstructure-retention", type=int, default=1)
    parser.add_argument("--deploy-retention", type=int, default=1)
    parser.add_argument("--artifact-grace-hours", type=float, default=6.0)
    parser.add_argument("--deploy-grace-days", type=float, default=7.0)
    parser.add_argument("--minimum-free-gib", type=float, default=20.0)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    minimum_free_bytes = int(args.minimum_free_gib * 1024**3)
    if minimum_free_bytes < 0:
        raise ValueError("minimum free space must be non-negative")

    retentions = {
        "root": args.root_retention,
        "updown": args.updown_retention,
        "forward": args.forward_retention,
        "microstructure": args.microstructure_retention,
    }
    plan = build_deletion_plan(
        args.backup_root,
        retentions=retentions,
        artifact_grace_seconds=args.artifact_grace_hours * 3600,
        deploy_retention=args.deploy_retention,
        deploy_grace_seconds=args.deploy_grace_days * 86400,
    )
    action = "DELETE" if args.apply else "WOULD_DELETE"
    for deletion in plan:
        print(f"{action}\t{deletion.size}\t{deletion.reason}\t{deletion.path}")

    deleted_bytes = apply_deletion_plan(plan) if args.apply else 0
    disk = shutil.disk_usage(args.backup_root)
    predicted_free = disk.free if args.apply else disk.free + sum(item.size for item in plan)
    print(
        "SUMMARY"
        f"\tfiles={len(plan)}"
        f"\tplanned_bytes={sum(item.size for item in plan)}"
        f"\tdeleted_bytes={deleted_bytes}"
        f"\tfree_bytes={disk.free}"
        f"\tpredicted_free_bytes={predicted_free}"
    )
    if predicted_free < minimum_free_bytes:
        print(f"ALERT\tfree_space_below_minimum={predicted_free}<{minimum_free_bytes}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
