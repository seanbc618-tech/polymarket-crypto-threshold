from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest


def _load_prune_module():
    script = Path(__file__).parents[1] / "scripts" / "prune_research_storage.py"
    spec = importlib.util.spec_from_file_location("prune_research_storage", script)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _write(path: Path, content: bytes = b"snapshot") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def test_prune_keeps_one_runtime_copy_and_protects_sealed_evidence(
    tmp_path: Path,
) -> None:
    module = _load_prune_module()
    backup_root = tmp_path / "backups"
    backup_root.mkdir()
    old_time = 1_000_000.0

    legacy = _write(backup_root / "crypto-threshold-20000101T000000.000000Z.db")
    retained: list[Path] = []
    stale: list[Path] = [legacy]
    for directory in ("updown", "forward", "microstructure"):
        old = _write(backup_root / directory / "crypto-threshold-20000101T000000.000000Z.db")
        new = _write(backup_root / directory / "crypto-threshold-20000102T000000.000000Z.db")
        stale.append(old)
        retained.append(new)

    artifact = _write(
        backup_root / "updown" / "crypto-threshold-20000103T000000.000000Z.db.partial-wal"
    )
    stale.append(artifact)
    old_archive = _write(backup_root / "deploy" / "deploy-20000101.tar.gz")
    new_archive = _write(backup_root / "deploy" / "deploy-20000102.tar.gz")
    stale.append(old_archive)
    retained.append(new_archive)
    sealed = _write(
        backup_root / "phase2-oos-20260729" / "crypto-threshold-20000101T000000.000000Z.db"
    )
    retained.append(sealed)

    for path in stale + retained:
        os.utime(path, (old_time, old_time))

    plan = module.build_deletion_plan(
        backup_root,
        retentions={
            "root": 0,
            "updown": 1,
            "forward": 1,
            "microstructure": 1,
        },
        artifact_grace_seconds=3600,
        deploy_retention=1,
        deploy_grace_seconds=3600,
        now=old_time + 7200,
    )

    assert {item.path for item in plan} == set(stale)
    deleted_bytes = module.apply_deletion_plan(plan)
    assert deleted_bytes == sum(len(b"snapshot") for _ in stale)
    assert all(not path.exists() for path in stale)
    assert all(path.exists() for path in retained)


def test_prune_refuses_symlinks_in_managed_directories(tmp_path: Path) -> None:
    module = _load_prune_module()
    backup_root = tmp_path / "backups"
    updown = backup_root / "updown"
    updown.mkdir(parents=True)
    target = _write(tmp_path / "outside.db")
    (updown / "crypto-threshold-20000101T000000.000000Z.db").symlink_to(target)

    with pytest.raises(RuntimeError, match="refusing symlink"):
        module.build_deletion_plan(
            backup_root,
            retentions={
                "root": 0,
                "updown": 1,
                "forward": 1,
                "microstructure": 1,
            },
            artifact_grace_seconds=3600,
            deploy_retention=1,
            deploy_grace_seconds=3600,
        )
