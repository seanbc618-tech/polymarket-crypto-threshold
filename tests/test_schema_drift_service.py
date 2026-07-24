"""External public-payload schema drift monitoring tests."""

from __future__ import annotations

from pathlib import Path

from crypto_threshold.services.schema_drift_service import ExternalPayloadSchemaMonitor
from crypto_threshold.storage.db import Database
from crypto_threshold.storage.repositories import Repository
from tests.conftest import NOW, make_book, make_market_payload


def test_valid_provider_contracts_are_recorded_without_raw_payload_echo(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    monitor = ExternalPayloadSchemaMonitor(repository)
    boundary = monitor.capture_boundary()
    _record(repository, "gamma", "market", make_market_payload())
    _record(repository, "gamma", "event_context", {"events": []})
    _record(repository, "polymarket_clob", "yes_book", make_book(outcome="YES"))
    _record(
        repository,
        "polymarket_clob",
        "market_info_fee_schedule",
        {"fd": {"r": 0.07, "e": 1, "to": True}},
    )
    _record(
        repository,
        "binance",
        "settlement_klines_1m",
        [[1, "1", "2", "0.5", "1.5", "10", 2]],
    )
    _record(
        repository,
        "coinbase",
        "sanity_spot",
        {"data": {"base": "BTC", "currency": "USD", "amount": "100000"}},
    )

    report = monitor.inspect_after(boundary)
    assert report.status == "ok"
    assert report.scanned_payload_count == 6
    assert not report.issues
    rendered = report.as_dict()
    assert rendered["contract_counts"]["gamma/market"] == 1
    assert "raw_payload" not in str(rendered)
    assert "100000" not in str(rendered)


def test_missing_required_shape_and_unknown_contract_fail_closed(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    monitor = ExternalPayloadSchemaMonitor(repository)
    boundary = monitor.capture_boundary()
    _record(
        repository,
        "polymarket_clob",
        "yes_book",
        {"bids": {}, "asks": [], "timestamp": None},
    )
    _record(repository, "new_provider", "mystery", {"value": 1})

    report = monitor.inspect_after(boundary)
    assert report.status == "drift_detected"
    codes = {issue.code for issue in report.issues}
    assert "bids_not_list" in codes
    assert "missing_timestamp" in codes
    assert "unknown_payload_contract" in codes


def test_boundary_excludes_older_payloads_and_empty_window_is_explicit(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    monitor = ExternalPayloadSchemaMonitor(repository)
    _record(repository, "gamma", "market", make_market_payload())
    boundary = monitor.capture_boundary()

    empty = monitor.inspect_after(boundary)
    assert empty.status == "no_payloads"
    assert empty.scanned_payload_count == 0

    _record(repository, "gamma", "event_context", {"events": []})
    report = monitor.inspect_after(boundary)
    assert report.status == "ok"
    assert report.scanned_payload_count == 1
    assert report.contract_counts == {"gamma/event_context": 1}


def _repository(tmp_path: Path) -> Repository:
    database = Database(tmp_path / "schema-drift.db")
    database.initialize()
    return Repository(database)


def _record(
    repository: Repository,
    source: str,
    payload_kind: str,
    payload: object,
) -> None:
    repository.record_external_payload(
        market_id=None,
        source=source,
        payload_kind=payload_kind,
        payload=payload,
        observed_at=NOW,
        received_at=NOW,
        source_version=f"{source}-test-v1",
    )
