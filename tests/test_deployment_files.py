from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).parents[1]
ENV_TEMPLATE = ROOT / "deploy" / "env" / "hk-readonly.example.env"
SHADOW_UNIT = (
    ROOT / "deploy" / "systemd" / "crypto-threshold-shadow.service"
)
BACKUP_UNIT = (
    ROOT / "deploy" / "systemd" / "crypto-threshold-backup.service"
)
TIMESYNCD_CONFIG = (
    ROOT / "deploy" / "timesyncd" / "50-polymarket-vps.conf"
)


def test_vps_environment_is_read_only_direct_connect() -> None:
    content = ENV_TEMPLATE.read_text()

    assert "TRADING_DISABLED=true" in content
    assert "POLYMARKET_STREAM_USER_CHANNEL_ENABLED=false" in content
    assert "POLYMARKET_STREAM_SHADOW_MODE=true" in content
    assert "BINANCE_STREAM_PROXY_URL=\n" in content
    assert "POLYMARKET_PRIVATE_KEY" not in content
    assert "POLYMARKET_FUNDER" not in content
    assert "API_KEY=" not in content


def test_shadow_unit_is_bounded_hardened_and_clears_proxies() -> None:
    content = SHADOW_UNIT.read_text()

    assert "User=crypto-threshold" in content
    assert "shadow --duration-hours 73" in content
    assert "UnsetEnvironment=" in content
    assert "HTTP_PROXY" in content
    assert "BINANCE_STREAM_PROXY_URL" in content
    assert "systemd-time-wait-sync.service" in content
    assert "ProtectSystem=strict" in content
    assert "NoNewPrivileges=true" in content
    assert "ReadWritePaths=/opt/polymarket-crypto-threshold/data" in content
    assert "BUY" not in content
    assert "SELL" not in content


def test_backup_unit_uses_consistent_backup_script() -> None:
    content = BACKUP_UNIT.read_text()

    assert "scripts/backup_sqlite.py" in content
    assert "--retention 7" in content
    assert "RestrictAddressFamilies=AF_UNIX" in content
    assert (
        "ReadWritePaths=/opt/polymarket-crypto-threshold/data "
        "/opt/polymarket-crypto-threshold/backups" in content
    )


def test_vps_timesync_uses_explicit_ipv4_sources() -> None:
    content = TIMESYNCD_CONFIG.read_text()

    assert "NTP=162.159.200.1 216.239.35.0 203.107.6.88" in content
    assert "RootDistanceMaxSec=5" in content
