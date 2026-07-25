"""Binance public market-data adapter."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal

import httpx

from crypto_threshold.domain.assets import ASSET_CONTRACTS, DAILY_THRESHOLD_ASSETS
from crypto_threshold.domain.prices import Kline, KlineSeries, PriceSnapshot

BINANCE_API = "https://api.binance.com/api/v3"
BINANCE_SOURCE_VERSION = "binance-spot-rest-v3"
ASSET_SYMBOLS = {
    asset: ASSET_CONTRACTS[asset].binance_symbol
    for asset in DAILY_THRESHOLD_ASSETS
    if ASSET_CONTRACTS[asset].binance_symbol is not None
}


class BinanceProvider:
    """Fetch supported public USDT ticker and kline data."""

    def __init__(
        self,
        base_url: str = BINANCE_API,
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

    def get_ticker_price(self, asset: str) -> PriceSnapshot:
        symbol = _symbol(asset)
        response = self._client.get(
            f"{self.base_url}/ticker/price", params={"symbol": symbol}
        )
        response.raise_for_status()
        payload = response.json()
        if str(payload.get("symbol") or "").upper() != symbol:
            raise ValueError("Binance ticker symbol mismatch")
        received_at = _utc(self._clock())
        return PriceSnapshot(
            asset=asset.upper(),
            quote="USDT",
            provider="binance",
            symbol=symbol,
            price=Decimal(str(payload["price"])),
            price_kind="last",
            observed_at=received_at,
            received_at=received_at,
            source_version=BINANCE_SOURCE_VERSION,
            raw_payload=payload,
        )

    def get_klines(
        self,
        asset: str,
        interval: str = "1m",
        limit: int = 100,
        *,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
    ) -> KlineSeries:
        symbol = _symbol(asset)
        params: dict[str, str | int] = {
            "symbol": symbol,
            "interval": interval,
            "limit": limit,
        }
        if start_time is not None:
            params["startTime"] = int(_utc(start_time).timestamp() * 1000)
        if end_time is not None:
            params["endTime"] = int(_utc(end_time).timestamp() * 1000)
        response = self._client.get(
            f"{self.base_url}/klines",
            params=params,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, list):
            raise ValueError("unexpected Binance kline response")
        klines: list[Kline] = []
        for item in payload:
            if not isinstance(item, list) or len(item) < 7:
                raise ValueError("malformed Binance kline")
            klines.append(
                Kline(
                    open_time=datetime.fromtimestamp(int(item[0]) / 1000, tz=UTC),
                    close_time=datetime.fromtimestamp(int(item[6]) / 1000, tz=UTC),
                    open=Decimal(str(item[1])),
                    high=Decimal(str(item[2])),
                    low=Decimal(str(item[3])),
                    close=Decimal(str(item[4])),
                    volume=Decimal(str(item[5])),
                )
            )
        return KlineSeries(
            asset=asset.upper(),
            quote="USDT",
            provider="binance",
            symbol=symbol,
            interval=interval,
            klines=tuple(klines),
            received_at=_utc(self._clock()),
            source_version=BINANCE_SOURCE_VERSION,
            raw_payload=payload,
        )

    def latest_close_snapshot(
        self, series: KlineSeries, *, now: datetime | None = None
    ) -> PriceSnapshot:
        cutoff = _utc(now or self._clock())
        closed = [kline for kline in series.klines if kline.close_time <= cutoff]
        if not closed:
            raise ValueError("Binance kline series has no closed candle")
        latest = max(closed, key=lambda kline: kline.close_time)
        return PriceSnapshot(
            asset=series.asset,
            quote=series.quote,
            provider=series.provider,
            symbol=series.symbol,
            price=latest.close,
            price_kind=f"{series.interval}_close",
            observed_at=latest.close_time,
            received_at=series.received_at,
            source_version=series.source_version,
            raw_payload=series.raw_payload,
        )


def _symbol(asset: str) -> str:
    symbol = ASSET_SYMBOLS.get(asset.upper())
    if symbol is None:
        raise ValueError(f"unsupported asset for Binance: {asset}")
    return symbol


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
