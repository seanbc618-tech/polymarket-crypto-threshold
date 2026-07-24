"""Coinbase public spot-price sanity-check adapter."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal

import httpx

from crypto_threshold.domain.prices import PriceSnapshot

COINBASE_API = "https://api.coinbase.com/v2"
COINBASE_SOURCE_VERSION = "coinbase-prices-v2"
ASSET_SYMBOLS = {"BTC": "BTC-USD", "ETH": "ETH-USD"}


class CoinbaseProvider:
    """Fetch public BTC/ETH USD spot prices for sanity checking only."""

    def __init__(
        self,
        base_url: str = COINBASE_API,
        *,
        client: httpx.Client | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = client or httpx.Client(timeout=10)
        self._owns_client = client is None
        self._clock = clock or (lambda: datetime.now(UTC))

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def get_spot_price(self, asset: str) -> PriceSnapshot:
        symbol = ASSET_SYMBOLS.get(asset.upper())
        if symbol is None:
            raise ValueError(f"unsupported asset for Coinbase: {asset}")
        response = self._client.get(f"{self.base_url}/prices/{symbol}/spot")
        response.raise_for_status()
        payload = response.json()
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict) or str(data.get("base") or "").upper() != asset.upper():
            raise ValueError("Coinbase spot asset mismatch")
        received_at = _utc(self._clock())
        return PriceSnapshot(
            asset=asset.upper(),
            quote=str(data.get("currency") or "").upper(),
            provider="coinbase",
            symbol=symbol,
            price=Decimal(str(data["amount"])),
            price_kind="spot",
            observed_at=received_at,
            received_at=received_at,
            source_version=COINBASE_SOURCE_VERSION,
            raw_payload=payload,
        )


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
