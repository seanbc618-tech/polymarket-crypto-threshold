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
UPDOWN_ENV_TEMPLATE = ROOT / "deploy" / "env" / "hk-updown-shadow.example.env"
UPDOWN_SHADOW_UNIT = (
    ROOT / "deploy" / "systemd" / "crypto-threshold-updown-shadow.service"
)
UPDOWN_BACKUP_UNIT = (
    ROOT / "deploy" / "systemd" / "crypto-threshold-updown-backup.service"
)
FORWARD_ENV_TEMPLATE = ROOT / "deploy" / "env" / "hk-forward-shadow.example.env"
FORWARD_SHADOW_UNIT = (
    ROOT / "deploy" / "systemd" / "crypto-threshold-forward-shadow.service"
)
FORWARD_BACKUP_UNIT = (
    ROOT / "deploy" / "systemd" / "crypto-threshold-forward-backup.service"
)
MICROSTRUCTURE_ENV_TEMPLATE = (
    ROOT / "deploy" / "env" / "hk-microstructure-shadow.example.env"
)
MICROSTRUCTURE_SHADOW_UNIT = (
    ROOT / "deploy" / "systemd" / "crypto-threshold-microstructure-shadow.service"
)
MICROSTRUCTURE_BACKUP_UNIT = (
    ROOT / "deploy" / "systemd" / "crypto-threshold-microstructure-backup.service"
)
MICROSTRUCTURE_BACKUP_TIMER = (
    ROOT / "deploy" / "systemd" / "crypto-threshold-microstructure-backup.timer"
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


def test_updown_environment_is_separate_public_and_read_only() -> None:
    content = UPDOWN_ENV_TEMPLATE.read_text()

    assert "DATABASE_PATH=/opt/polymarket-crypto-threshold/data/updown-shadow.db" in content
    assert "SHADOW_CONTRACT_FAMILY=short_updown" in content
    assert "CHAINLINK_REFERENCE_STREAM_ENABLED=false" in content
    assert "BINANCE_REFERENCE_STREAM_ENABLED=false" in content
    assert "SHORT_CEX_MODEL_PATH=/opt/polymarket-crypto-threshold/data/models/" in content
    assert "SHORT_CHALLENGER_ENABLED=true" in content
    assert "SHORT_CHALLENGER_CHECKPOINTS_SECONDS=180,120,60,30" in content
    assert "SHORT_CHALLENGER_LATENCIES_MS=0,100,250,500,1000" in content
    assert "SHORT_CHALLENGER_MIN_REMAINING_SECONDS=5" in content
    assert "SHADOW_INTERVAL_SECONDS=10" in content
    assert "SHADOW_ANALYSIS_LIMIT=14" in content
    assert "SHADOW_SETTLEMENT_LIMIT=50" in content
    assert "TRADING_DISABLED=true" in content
    assert "POLYMARKET_STREAM_USER_CHANNEL_ENABLED=false" in content
    assert "POLYMARKET_PRIVATE_KEY" not in content
    assert "POLYMARKET_FUNDER" not in content
    assert "API_KEY=" not in content


def test_updown_unit_is_independent_hardened_and_secret_free() -> None:
    content = UPDOWN_SHADOW_UNIT.read_text()

    assert "EnvironmentFile=/etc/polymarket-crypto-updown.env" in content
    assert "ExecStart=/opt/polymarket-crypto-threshold/.venv/bin/crypto-threshold shadow" in content
    assert "crypto-threshold-shadow.service" not in content
    assert "POLYMARKET_PRIVATE_KEY" in content
    assert "POLYMARKET_FUNDER" in content
    assert "UnsetEnvironment=" in content
    assert "HTTP_PROXY" in content
    assert "ProtectSystem=strict" in content
    assert "NoNewPrivileges=true" in content
    assert "BUY" not in content
    assert "SELL" not in content


def test_updown_backup_uses_its_own_database_and_directory() -> None:
    content = UPDOWN_BACKUP_UNIT.read_text()

    assert "--database /opt/polymarket-crypto-threshold/data/updown-shadow.db" in content
    assert "--output-dir /opt/polymarket-crypto-threshold/backups/updown" in content
    assert "RestrictAddressFamilies=AF_UNIX" in content


def test_forward_environment_is_daily_read_only_and_lower_cadence() -> None:
    content = FORWARD_ENV_TEMPLATE.read_text()

    assert (
        "DATABASE_PATH=/opt/polymarket-crypto-threshold/data/phase2-forward.db"
        in content
    )
    assert "SHADOW_CONTRACT_FAMILY=daily_threshold" in content
    assert "SHADOW_INTERVAL_SECONDS=900" in content
    assert "SHADOW_DISCOVERY_LIMIT=20" in content
    assert "SHADOW_ANALYSIS_LIMIT=20" in content
    assert "SHADOW_SETTLEMENT_LIMIT=20" in content
    assert "TRADING_DISABLED=true" in content
    assert "POLYMARKET_STREAM_USER_CHANNEL_ENABLED=false" in content
    assert "BINANCE_STREAM_PROXY_URL=\n" in content
    assert "POLYMARKET_PRIVATE_KEY" not in content
    assert "POLYMARKET_FUNDER" not in content
    assert "API_KEY=" not in content


def test_forward_unit_is_bounded_independent_and_secret_free() -> None:
    content = FORWARD_SHADOW_UNIT.read_text()

    assert "EnvironmentFile=/etc/polymarket-crypto-forward.env" in content
    assert "shadow --duration-hours 336" in content
    assert "crypto-threshold-shadow.service" not in content
    assert "POLYMARKET_PRIVATE_KEY" in content
    assert "POLYMARKET_FUNDER" in content
    assert "UnsetEnvironment=" in content
    assert "ProtectSystem=strict" in content
    assert "NoNewPrivileges=true" in content
    assert "BUY" not in content
    assert "SELL" not in content


def test_forward_backup_is_separate_and_retains_three_copies() -> None:
    content = FORWARD_BACKUP_UNIT.read_text()

    assert "--database /opt/polymarket-crypto-threshold/data/phase2-forward.db" in content
    assert "--output-dir /opt/polymarket-crypto-threshold/backups/forward" in content
    assert "--retention 3" in content
    assert "RestrictAddressFamilies=AF_UNIX" in content


def test_microstructure_environment_is_public_only_and_independent() -> None:
    content = MICROSTRUCTURE_ENV_TEMPLATE.read_text()

    assert "MICROSTRUCTURE_ENABLED=true" in content
    assert (
        "MICROSTRUCTURE_DATABASE_PATH=/opt/polymarket-crypto-threshold/data/"
        "microstructure-shadow.db" in content
    )
    assert "MICROSTRUCTURE_SYMBOLS=BTCUSDT,ETHUSDT,SOLUSDT,XRPUSDT" in content
    assert "TRADING_DISABLED=true" in content
    assert "BINANCE_FUTURES_API_BASE=https://fapi.binance.com/fapi/v1" in content
    assert "MICROSTRUCTURE_STREAM_READY_TIMEOUT_SECONDS=30" in content
    assert "MICROSTRUCTURE_PURGE_SECONDS=600" in content
    assert "MICROSTRUCTURE_EMBARGO_SECONDS=300" in content
    assert "BINANCE_STREAM_PROXY_URL=\n" in content
    assert "POLYMARKET_PRIVATE_KEY" not in content
    assert "POLYMARKET_FUNDER" not in content


def test_microstructure_unit_is_bounded_and_does_not_touch_existing_units() -> None:
    content = MICROSTRUCTURE_SHADOW_UNIT.read_text()

    assert "EnvironmentFile=/etc/polymarket-crypto-microstructure.env" in content
    assert "microstructure-shadow --duration-hours 2" in content
    assert "crypto-threshold-forward-shadow.service" not in content
    assert "crypto-threshold-updown-shadow.service" not in content
    assert "UnsetEnvironment=" in content
    assert "POLYMARKET_PRIVATE_KEY" in content
    assert "ProtectSystem=strict" in content
    assert "NoNewPrivileges=true" in content


def test_microstructure_backup_is_separate_and_timed() -> None:
    content = MICROSTRUCTURE_BACKUP_UNIT.read_text()
    timer = MICROSTRUCTURE_BACKUP_TIMER.read_text()

    assert "--database /opt/polymarket-crypto-threshold/data/microstructure-shadow.db" in content
    assert "--output-dir /opt/polymarket-crypto-threshold/backups/microstructure" in content
    assert "--retention 3" in content
    assert "RestrictAddressFamilies=AF_UNIX" in content
    assert "OnUnitActiveSec=30min" in timer
