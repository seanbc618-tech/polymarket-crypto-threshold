"""Phase 2 readiness checks that never construct an authenticated client."""

from __future__ import annotations

from dataclasses import dataclass

from crypto_threshold.adapters.keychain import KeychainStore
from crypto_threshold.config import Settings
from crypto_threshold.dashboard.setup_flow import read_wallet_status
from crypto_threshold.storage.db import SCHEMA_VERSION, Database


@dataclass(frozen=True)
class ReadinessCheck:
    name: str
    ok: bool
    status: str
    detail: str
    required: bool = True


class ResearchReadinessService:
    """Report research/UI readiness while keeping live capabilities disconnected."""

    def __init__(
        self,
        settings: Settings,
        *,
        keychain: KeychainStore | None = None,
    ) -> None:
        self.settings = settings
        self.keychain = keychain

    def check(self) -> list[ReadinessCheck]:
        checks = [
            self._database_check(),
            ReadinessCheck(
                "trading_mode",
                self.settings.TRADING_DISABLED,
                "locked" if self.settings.TRADING_DISABLED else "unsafe",
                (
                    "TRADING_DISABLED=true; exchange mutation is unavailable"
                    if self.settings.TRADING_DISABLED
                    else "TRADING_DISABLED=false; dashboard must not start"
                ),
            ),
            ReadinessCheck(
                "user_channel",
                not self.settings.POLYMARKET_STREAM_USER_CHANNEL_ENABLED,
                (
                    "disabled"
                    if not self.settings.POLYMARKET_STREAM_USER_CHANNEL_ENABLED
                    else "unsafe"
                ),
                "authenticated Polymarket User Channel is disabled in Phase 2",
            ),
            ReadinessCheck(
                "public_market_data",
                bool(
                    self.settings.POLYMARKET_GAMMA_API_BASE
                    and self.settings.POLYMARKET_CLOB_API_BASE
                ),
                "configured",
                "Gamma/CLOB public read endpoints are configured",
            ),
            self._wallet_check(),
            ReadinessCheck(
                "authenticated_account",
                True,
                "not-enabled",
                "balance, open orders, fills, positions, and signing are not connected",
                required=False,
            ),
            ReadinessCheck(
                "trading_service",
                True,
                "absent",
                "no TradingService or BUY/SELL route exists",
            ),
        ]
        return checks

    def _database_check(self) -> ReadinessCheck:
        try:
            health = Database(self.settings.DATABASE_PATH).health()
        except Exception as exc:  # noqa: BLE001
            return ReadinessCheck(
                "database",
                False,
                "failed",
                f"{type(exc).__name__}: {exc}",
            )
        ok = bool(
            health.get("ok")
            and health.get("foreign_keys")
            and health.get("journal_mode") == "wal"
            and health.get("schema_version") == SCHEMA_VERSION
        )
        return ReadinessCheck(
            "database",
            ok,
            "ok" if ok else "invalid",
            f"schema={health.get('schema_version')} journal={health.get('journal_mode')}",
        )

    def _wallet_check(self) -> ReadinessCheck:
        status = read_wallet_status(self.settings, self.keychain)
        configured = bool(status.private_key_configured and status.funder)
        return ReadinessCheck(
            "wallet_configuration",
            configured,
            "configured" if configured else "not-configured",
            (
                "Keychain private key and funder address are configured; "
                "they are not connected to an authenticated client"
                if configured
                else "wallet is optional for Phase 2 research"
            ),
            required=False,
        )


def required_readiness_ok(checks: list[ReadinessCheck]) -> bool:
    return all(check.ok for check in checks if check.required)
