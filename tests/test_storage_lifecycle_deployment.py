from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).parents[1]
SYSTEMD = ROOT / "deploy" / "systemd"


def test_storage_pruner_is_allow_listed_and_preserves_sealed_directories() -> None:
    content = (SYSTEMD / "crypto-threshold-storage-prune.service").read_text()

    assert "scripts/prune_research_storage.py" in content
    assert "--root-retention 0" in content
    assert "--updown-retention 1" in content
    assert "--forward-retention 1" in content
    assert "--microstructure-retention 1" in content
    assert "--minimum-free-gib 20" in content
    assert "User=root" in content
    assert "ReadWritePaths=/opt/polymarket-crypto-threshold/backups" in content
    assert "ReadWritePaths=/opt/polymarket-crypto-threshold/data" not in content
    assert "phase2-training" not in content
    assert "phase2-oos" not in content
    assert "/final" not in content


def test_storage_pruner_runs_after_daily_runtime_backups() -> None:
    content = (SYSTEMD / "crypto-threshold-storage-prune.timer").read_text()

    assert "OnCalendar=*-*-* 04:50:00" in content
    assert "Persistent=true" in content


def test_runtime_backup_dropins_keep_one_copy() -> None:
    for family in ("updown", "forward", "microstructure"):
        path = SYSTEMD / f"crypto-threshold-{family}-backup.service.d" / "10-storage-policy.conf"
        content = path.read_text()
        assert "ExecStart=" in content
        assert "--retention 1" in content

    microstructure = (
        SYSTEMD / "crypto-threshold-microstructure-backup.service.d" / "10-storage-policy.conf"
    ).read_text()
    assert "--skip-unchanged" in microstructure
