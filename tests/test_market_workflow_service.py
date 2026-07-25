"""Read-only Gamma -> workflow -> repository acceptance tests."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest

from crypto_threshold.config import Settings
from crypto_threshold.services.market_workflow_service import MarketWorkflowService
from crypto_threshold.storage.db import Database
from crypto_threshold.storage.repositories import Repository
from tests.conftest import (
    NOW,
    FakeBinanceProvider,
    FakeCoinbaseProvider,
    FakePolymarketClient,
    make_market_payload,
)


def _workflow(
    tmp_path: Path,
    market_payload: dict[str, object],
    *,
    client: FakePolymarketClient | None = None,
    coinbase: FakeCoinbaseProvider | None = None,
) -> tuple[MarketWorkflowService, Repository, FakePolymarketClient, Database]:
    database = Database(tmp_path / "workflow.db")
    database.initialize()
    repository = Repository(database)
    read_client = client or FakePolymarketClient(market_payload)
    settings = Settings(DATABASE_PATH=str(database.path), _env_file=None)
    service = MarketWorkflowService(
        client=read_client,
        repository=repository,
        binance=FakeBinanceProvider(),
        coinbase=coinbase or FakeCoinbaseProvider(),
        settings=settings,
        clock=lambda: NOW,
    )
    return service, repository, read_client, database


def test_complete_real_input_shape_persists_net_ev_and_raw_audit(
    tmp_path: Path, market_payload: dict[str, object]
) -> None:
    service, repository, client, database = _workflow(tmp_path, market_payload)
    signal = service.analyze("market-1")
    assert signal.status == "analyzed"
    assert signal.yes_ask_vwap is not None
    assert signal.yes_midpoint is not None
    assert signal.yes_ask_vwap != signal.yes_midpoint
    assert signal.yes_net_ev is not None
    assert signal.no_net_ev is not None
    assert client.reads == [
        "market",
        "book:yes-token",
        "book:no-token",
        "market_info",
    ]

    stored = repository.latest_signal("market-1")
    assert stored is not None
    assert stored["input_payload_max_id"] is not None
    assert repository.table_count("external_payloads") == 7
    with database.connect() as connection:
        payload_max = connection.execute(
            "SELECT MAX(id) FROM external_payloads WHERE market_id='market-1'"
        ).fetchone()[0]
        assert stored["input_payload_max_id"] == payload_max
        assert json.loads(stored["reasons"]) is not None
        linked = list(
            connection.execute(
                """
                SELECT p.analysis_run_id, i.input_role
                FROM analysis_signal_inputs AS i
                JOIN external_payloads AS p ON p.id = i.payload_id
                WHERE i.signal_id = ? ORDER BY p.id
                """,
                (signal.signal_id,),
            )
        )
        assert len(linked) == 7
        assert {row["analysis_run_id"] for row in linked} == {
            stored["analysis_run_id"]
        }
        assert {row["input_role"] for row in linked} >= {
            "market",
            "yes_book",
            "no_book",
            "market_info_fee_schedule",
            "settlement_klines_1m",
            "volatility_klines_1d",
            "sanity_spot",
        }


@pytest.mark.parametrize(
    ("asset", "name", "strike", "pair"),
    [
        ("SOL", "Solana", "50", "SOL/USDT"),
        ("XRP", "XRP", "0.60", "XRP/USDT"),
    ],
)
def test_additional_assets_complete_the_same_read_only_workflow(
    tmp_path: Path,
    asset: str,
    name: str,
    strike: str,
    pair: str,
) -> None:
    payload = make_market_payload(
        question=(
            f"Will the price of {name} be above ${strike} on July 23, 2026?"
        ),
        description=(
            f"This market resolves Yes using the Binance {pair} 1-minute "
            "candle Close price at 12:00 PM ET."
        ),
    )
    service, repository, _, _ = _workflow(
        tmp_path,
        payload,
        coinbase=FakeCoinbaseProvider(asset=asset),
    )

    signal = service.analyze("market-1")

    assert signal.status == "analyzed"
    assert signal.asset == asset
    assert repository.latest_signal("market-1")["asset"] == asset


def test_stale_book_hard_rejects(
    tmp_path: Path, market_payload: dict[str, object]
) -> None:
    client = FakePolymarketClient(
        market_payload, book_time=NOW - timedelta(seconds=200)
    )
    service, _, _, _ = _workflow(tmp_path, market_payload, client=client)
    signal = service.analyze("market-1")
    assert signal.status == "rejected"
    assert any(reason.startswith("stale_yes_book") for reason in signal.reasons)


def test_missing_fee_schedule_hard_rejects(
    tmp_path: Path, market_payload: dict[str, object]
) -> None:
    client = FakePolymarketClient(market_payload, fee_payload={"fd": {}})
    service, _, _, _ = _workflow(tmp_path, market_payload, client=client)
    signal = service.analyze("market-1")
    assert signal.status == "rejected"
    assert any(reason.startswith("missing_fee_schedule") for reason in signal.reasons)


def test_malformed_tokens_reject_before_book_reads_and_after_raw_persistence(
    tmp_path: Path, market_payload: dict[str, object]
) -> None:
    malformed = {**market_payload, "clobTokenIds": "not-json"}
    client = FakePolymarketClient(malformed)
    service, repository, _, _ = _workflow(tmp_path, malformed, client=client)
    signal = service.analyze("market-1")
    assert signal.status == "rejected"
    assert client.reads == ["market"]
    assert repository.table_count("external_payloads") == 1
    assert repository.latest_signal("market-1")["input_payload_max_id"] == 1


def test_no_trade_mutation_surface_exists(
    tmp_path: Path, market_payload: dict[str, object]
) -> None:
    service, _, client, database = _workflow(tmp_path, market_payload)
    service.analyze("market-1")
    assert not hasattr(client, "place_order")
    assert not hasattr(service, "buy")
    assert not hasattr(service, "sell")
    with database.connect() as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    assert "orders" not in tables
    assert "order_intents" not in tables
