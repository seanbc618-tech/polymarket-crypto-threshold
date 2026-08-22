"""Fail closed when account or order credentials enter the capture process."""

from __future__ import annotations

from collections.abc import Mapping

FORBIDDEN_ENVIRONMENT_KEYS = frozenset(
    {
        "POLYMARKET_PRIVATE_KEY",
        "POLYMARKET_FUNDER",
        "POLYMARKET_API_KEY",
        "POLYMARKET_API_SECRET",
        "POLYMARKET_API_PASSPHRASE",
        "POLYMARKET_STREAM_USER_CHANNEL_ENABLED",
        "TRADING_ENABLED",
        "LIVE_TRADING_ENABLED",
    }
)


def assert_capture_only_environment(environment: Mapping[str, str]) -> None:
    present = sorted(key for key in FORBIDDEN_ENVIRONMENT_KEYS if environment.get(key))
    if present:
        raise RuntimeError(f"capture process contains forbidden credentials: {','.join(present)}")
