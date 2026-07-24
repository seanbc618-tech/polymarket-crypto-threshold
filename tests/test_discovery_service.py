"""Canonical discovery persistence tests."""

from __future__ import annotations

from pathlib import Path

from crypto_threshold.services.discovery_service import DiscoveryService
from crypto_threshold.storage.db import Database
from crypto_threshold.storage.repositories import Repository
from tests.conftest import NOW, FakePolymarketClient


def test_rerun_discovery_upserts_one_market(
    tmp_path: Path, market_payload: dict[str, object]
) -> None:
    database = Database(tmp_path / "discovery.db")
    database.initialize()
    repository = Repository(database)
    service = DiscoveryService(
        FakePolymarketClient(market_payload), repository, clock=lambda: NOW
    )
    service.discover(asset="BTC")
    service.discover(asset="BTC")
    assert repository.table_count("markets") == 1
    assert repository.table_count("resolution_rules") == 1
    assert repository.table_count("external_payloads") == 2
