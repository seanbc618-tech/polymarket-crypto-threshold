"""Tests for the public Binance microstructure adapter."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from crypto_threshold.adapters.prices.binance_microstructure import (
    BinanceMicrostructureRestClient,
    BinanceMicrostructureStream,
    normalize_binance_agg_trade,
    normalize_binance_depth,
)
from crypto_threshold.domain.microstructure import TradeAggressor
from crypto_threshold.domain.microstructure_capture import RawMicrostructureKind


class _Response:
    def __init__(self, payload: object) -> None:
        self.payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> object:
        return self.payload


class _HttpClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def get(self, url: str, *, params: dict[str, object]) -> _Response:
        self.calls.append((url, params))
        if url.endswith("/depth"):
            return _Response(
                {
                    "lastUpdateId": 100,
                    "bids": [["100.0", "2.0"], ["99.0", "1.0"]],
                    "asks": [["101.0", "3.0"], ["102.0", "1.0"]],
                }
            )
        return _Response(
            {
                "symbol": "BTCUSDT",
                "markPrice": "100.5",
                "indexPrice": "100.0",
                "lastFundingRate": "0.0001",
                "time": 1_700_000_000_000,
            }
        )


def test_normalizers_preserve_exchange_and_receive_times_and_hashes() -> None:
    received = datetime(2026, 7, 30, 5, 0, 0, 1000, tzinfo=UTC)
    depth = normalize_binance_depth(
        {
            "e": "depthUpdate",
            "E": 1_700_000_000_000,
            "s": "btcusdt",
            "U": 101,
            "u": 102,
            "b": [["100.0", "1.5"]],
            "a": [["101.0", "0"]],
        },
        received_at=received,
    )
    assert depth.kind is RawMicrostructureKind.DEPTH
    assert depth.symbol == "BTCUSDT"
    assert depth.venue_sequence_start == 101
    assert depth.venue_sequence_end == 102
    assert depth.received_at == received
    assert depth.bids[0].quantity == Decimal("1.5")
    assert len(depth.payload_hash) == 64

    trade = normalize_binance_agg_trade(
        {
            "e": "aggTrade",
            "E": 1_700_000_000_010,
            "T": 1_700_000_000_011,
            "s": "BTCUSDT",
            "a": 4,
            "p": "100.25",
            "q": "0.2",
            "m": True,
        },
        received_at=received,
    )
    assert trade.kind is RawMicrostructureKind.TRADE
    assert trade.aggressor is TradeAggressor.SELL
    assert trade.price == Decimal("100.25")
    assert trade.quantity == Decimal("0.2")


def test_rest_client_reads_depth_and_perpetual_mark_without_authentication() -> None:
    fake = _HttpClient()
    client = BinanceMicrostructureRestClient(
        spot_base_url="https://spot.example/api/v3",
        futures_base_url="https://futures.example/fapi/v1",
        client=fake,  # type: ignore[arg-type]
        clock=lambda: datetime(2026, 7, 30, 5, 0, tzinfo=UTC),
    )
    snapshot = client.depth_snapshot("btcusdt", limit=5)
    mark = client.perpetual_mark("btcusdt")

    assert snapshot.source == "binance_spot_rest"
    assert snapshot.timestamp_trusted is False
    assert snapshot.venue_sequence_end == 100
    assert mark.mark_price == Decimal("100.5")
    assert mark.index_price == Decimal("100.0")
    assert fake.calls == [
        (
            "https://spot.example/api/v3/depth",
            {"symbol": "BTCUSDT", "limit": 5},
        ),
        (
            "https://futures.example/fapi/v1/premiumIndex",
            {"symbol": "BTCUSDT"},
        ),
    ]


def test_rest_client_rejects_crossed_snapshot() -> None:
    class Crossed(_HttpClient):
        def get(self, url: str, *, params: dict[str, object]) -> _Response:
            return _Response(
                {
                    "lastUpdateId": 100,
                    "bids": [["101", "1"]],
                    "asks": [["100", "1"]],
                }
            )

    client = BinanceMicrostructureRestClient(
        client=Crossed(),  # type: ignore[arg-type]
    )
    try:
        client.depth_snapshot("BTCUSDT")
    except ValueError as exc:
        assert "crossed" in str(exc)
    else:
        raise AssertionError("crossed snapshot was accepted")


def test_stream_health_reports_each_symbol_observed_before_snapshot() -> None:
    received = datetime(2026, 7, 30, 5, 0, 0, 1000, tzinfo=UTC)
    stream = BinanceMicrostructureStream(
        symbols=("BTCUSDT", "ETHUSDT"),
        max_events=1_000,
        clock=lambda: received,
    )
    stream._on_depth(  # noqa: SLF001 - callback contract is the behavior under test
        {
            "e": "depthUpdate",
            "E": 1_700_000_000_000,
            "s": "BTCUSDT",
            "U": 101,
            "u": 101,
            "b": [["100", "1"]],
            "a": [["101", "1"]],
        }
    )
    detail = stream.health()["detail"]
    assert isinstance(detail, dict)
    assert detail["observed_symbols"] == ["BTCUSDT"]
