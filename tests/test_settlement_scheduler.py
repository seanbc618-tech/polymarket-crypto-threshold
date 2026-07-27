from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from crypto_threshold.domain.rules import SHORT_UPDOWN_FAMILY
from crypto_threshold.services.settlement_service import (
    SettlementBatchError,
    SettlementService,
)
from crypto_threshold.storage.db import Database
from crypto_threshold.storage.repositories import Repository

NOW = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)


class _ResolutionClient:
    def __init__(
        self,
        events: dict[str, dict[str, Any]],
        *,
        failures: set[str] | None = None,
    ) -> None:
        self.events = events
        self.failures = failures or set()
        self.calls: list[str] = []
        self.price_calls: list[tuple[str, str]] = []

    def get_event(self, event_id: str) -> dict[str, Any]:
        self.calls.append(event_id)
        if event_id in self.failures:
            raise TimeoutError("test timeout")
        return self.events[event_id]

    def get_crypto_window_price(
        self,
        asset: str,
        *,
        interval: str,
        start: datetime,
        end: datetime,
    ) -> dict[str, Any]:
        self.price_calls.append((asset, interval))
        return {
            "openPrice": "100",
            "closePrice": "101",
            "completed": True,
            "incomplete": False,
            "cached": True,
            "timestamp": int(end.timestamp() * 1000),
        }


def _seed_markets(
    repository: Repository,
    *,
    count: int,
    target: datetime,
) -> tuple[list[str], dict[str, dict[str, Any]]]:
    market_ids = [f"market-{index:02d}" for index in range(count)]
    events: dict[str, dict[str, Any]] = {}
    with repository.database.transaction() as connection:
        for market_id in market_ids:
            index = int(market_id.rsplit("-", 1)[1])
            event_id = f"event-{index:02d}"
            condition_id = f"condition-{index:02d}"
            connection.execute(
                """
                INSERT INTO markets (
                    market_id, event_id, condition_id, question, raw_payload,
                    raw_received_at
                ) VALUES (?, ?, ?, ?, '{}', ?)
                """,
                (
                    market_id,
                    event_id,
                    condition_id,
                    f"BTC Up or Down {index}",
                    NOW.isoformat(),
                ),
            )
            connection.execute(
                """
                INSERT INTO resolution_rules (
                    rule_id, market_id, event_id, condition_id,
                    yes_token_id, no_token_id, asset, settlement_source, pair,
                    exact_operator, strike, candle_interval, price_field,
                    timezone, observation_time, target_time_utc,
                    window_start_time_utc, tradable,
                    preview_only, raw_description, contract_family,
                    boundary_type, affirmative_outcome, negative_outcome
                ) VALUES (?, ?, ?, ?, ?, ?, 'BTC', 'chainlink', 'BTC/USD',
                          '>=', '100', '5m', 'data_stream_value', 'UTC',
                          'window_start', ?, ?, 1, 0, 'test', ?,
                          'window_start_price', 'Up', 'Down')
                """,
                (
                    f"rule-{index:02d}",
                    market_id,
                    event_id,
                    condition_id,
                    f"{market_id}-up",
                    f"{market_id}-down",
                    target.isoformat(),
                    (target - timedelta(minutes=5)).isoformat(),
                    SHORT_UPDOWN_FAMILY,
                ),
            )
            events[event_id] = {
                "id": event_id,
                "eventMetadata": {"priceToBeat": "100"},
                "markets": [
                    {
                        "id": market_id,
                        "conditionId": condition_id,
                        "closed": True,
                        "outcomes": ["Up", "Down"],
                        "outcomePrices": ["1", "0"],
                    }
                ],
            }
    return market_ids, events


def _resolved_events(
    events: dict[str, dict[str, Any]],
    market_ids: list[str],
) -> None:
    for market_id in market_ids:
        index = int(market_id.rsplit("-", 1)[1])
        event = events[f"event-{index:02d}"]
        event["eventMetadata"]["finalPrice"] = "101"


def _service(
    tmp_path: Path,
    *,
    market_count: int,
    resolved_ids: list[str] | None = None,
    failures: set[str] | None = None,
    clock: list[datetime] | None = None,
) -> tuple[SettlementService, Repository, _ResolutionClient, list[str]]:
    database = Database(tmp_path / "settlement-scheduler.db")
    database.initialize()
    repository = Repository(database)
    market_ids, events = _seed_markets(
        repository,
        count=market_count,
        target=NOW - timedelta(minutes=30),
    )
    _resolved_events(events, resolved_ids or [])
    client = _ResolutionClient(events, failures=failures)
    current = clock or [NOW]
    service = SettlementService(
        repository=repository,
        binance=object(),  # type: ignore[arg-type]
        client=client,  # type: ignore[arg-type]
        clock=lambda: current[0],
    )
    return service, repository, client, market_ids


def test_pending_head_does_not_starve_later_candidates(tmp_path: Path) -> None:
    later = [f"market-{index:02d}" for index in (14, 15)]
    service, repository, client, _ = _service(
        tmp_path,
        market_count=16,
        resolved_ids=later,
    )

    assert service.settle_due(limit=14) == ()
    assert client.calls == [f"event-{index:02d}" for index in range(14)]
    assert repository.table_count("settlement_attempts") == 14

    labels = service.settle_due(limit=14)

    assert {label.market_id for label in labels} == set(later)
    assert client.calls[-2:] == ["event-14", "event-15"]
    assert repository.table_count("settlement_labels") == 2


def test_retry_and_new_candidates_keep_rotating(tmp_path: Path) -> None:
    clock = [NOW]
    service, repository, client, _ = _service(
        tmp_path,
        market_count=30,
        resolved_ids=[f"market-{index:02d}" for index in range(14, 30)],
        clock=clock,
    )

    assert service.settle_due(limit=14) == ()
    clock[0] = NOW + timedelta(minutes=6)

    labels = service.settle_due(limit=14)

    assert {label.market_id for label in labels} == {
        f"market-{index:02d}" for index in range(14, 21)
    }
    assert set(client.calls[14:]) == {
        *(f"event-{index:02d}" for index in range(7)),
        *(f"event-{index:02d}" for index in range(14, 21)),
    }
    with repository.database.connect() as connection:
        attempts = connection.execute(
            """
            SELECT market_id, attempt_count
            FROM settlement_attempts
            ORDER BY market_id
            """
        ).fetchall()
    assert [
        (row["market_id"], row["attempt_count"])
        for row in attempts
        if row["market_id"] < "market-14"
    ] == [
        (f"market-{index:02d}", 2) for index in range(7)
    ] + [
        (f"market-{index:02d}", 1) for index in range(7, 14)
    ]


def test_pending_backoff_and_unchanged_payload_deduplication(
    tmp_path: Path,
) -> None:
    clock = [NOW]
    service, repository, client, _ = _service(
        tmp_path,
        market_count=1,
        clock=clock,
    )

    assert service.settle_due(limit=1) == ()
    assert repository.table_count("external_payloads") == 1

    clock[0] = NOW + timedelta(minutes=1)
    assert service.settle_due(limit=1) == ()
    assert len(client.calls) == 1
    assert repository.table_count("external_payloads") == 1

    clock[0] = NOW + timedelta(minutes=6)
    assert service.settle_due(limit=1) == ()
    assert len(client.calls) == 2
    assert repository.table_count("external_payloads") == 1
    with repository.database.connect() as connection:
        attempt = connection.execute(
            """
            SELECT attempt_count, last_status, next_attempt_at
            FROM settlement_attempts
            WHERE market_id = 'market-00'
            """
        ).fetchone()
    assert attempt["attempt_count"] == 2
    assert attempt["last_status"] == "pending"
    assert attempt["next_attempt_at"] == (
        NOW + timedelta(minutes=21)
    ).isoformat()

    event = client.events["event-00"]
    event["updatedAt"] = "irrelevant-market-metadata-change"
    event["eventMetadata"]["finalPrice"] = "101"
    clock[0] = NOW + timedelta(minutes=22)
    labels = service.settle_due(limit=1)

    assert len(labels) == 1
    assert repository.table_count("external_payloads") == 3


def test_one_settlement_error_does_not_block_other_candidates(
    tmp_path: Path,
) -> None:
    resolved = ["market-01"]
    service, repository, client, _ = _service(
        tmp_path,
        market_count=2,
        resolved_ids=resolved,
        failures={"event-00"},
    )

    with pytest.raises(SettlementBatchError) as error:
        service.settle_due(limit=2)

    assert "market-00:TimeoutError" in error.value.reasons
    label = repository.get_settlement_label("market-01")
    assert label is not None
    assert label["market_id"] == "market-01"
    assert client.calls == ["event-00", "event-01"]
    with repository.database.connect() as connection:
        statuses = connection.execute(
            """
            SELECT market_id, last_status
            FROM settlement_attempts
            ORDER BY market_id
            """
        ).fetchall()
    assert [(row["market_id"], row["last_status"]) for row in statuses] == [
        ("market-00", "error"),
        ("market-01", "succeeded"),
    ]


def test_schema_v5_migration_adds_durable_settlement_attempt_state(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "migration.db")
    database.initialize()
    with database.transaction() as connection:
        connection.execute("DROP TABLE settlement_attempts")
        connection.execute(
            "UPDATE schema_meta SET version = 4 WHERE id = 1"
        )

    database.initialize()

    with database.connect() as connection:
        tables = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        version = connection.execute(
            "SELECT version FROM schema_meta WHERE id = 1"
        ).fetchone()["version"]
    assert version == 5
    assert "settlement_attempts" in tables
