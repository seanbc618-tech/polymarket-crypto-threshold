"""Independent OOS event-group coverage gate tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from crypto_threshold.domain.rules import DAILY_THRESHOLD_FAMILY
from crypto_threshold.services.oos_coverage_service import OOSCoverageService


class _Repository:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows

    def settled_strategy_signal_rows(
        self, *, contract_family: str, workflow_version: str
    ) -> list[dict[str, object]]:
        assert contract_family == DAILY_THRESHOLD_FAMILY
        assert workflow_version == "market-workflow-v2"
        return self.rows


def _row(asset: str, target: datetime, *, signal_id: str) -> dict[str, object]:
    return {
        "signal_id": signal_id,
        "asset": asset,
        "deadline": target.isoformat(),
        "label_interval": "1m",
    }


def test_coverage_passes_only_with_independent_dates_and_asset_groups() -> None:
    assets = ("BTC", "ETH", "SOL", "XRP")
    offsets_by_asset = {
        "BTC": (0, 1, 2, 3, 4),
        "ETH": (0, 1, 2, 3, 4),
        "SOL": (1, 2, 3, 4, 5),
        "XRP": (2, 3, 4, 5, 6),
    }
    rows: list[dict[str, object]] = []
    start = datetime(2026, 7, 1, 16, 0, tzinfo=UTC)
    for asset in assets:
        for offset in offsets_by_asset[asset]:
            target = start + timedelta(days=offset)
            rows.append(_row(asset, target, signal_id=f"{asset}-{offset}"))
            rows.append(_row(asset, target, signal_id=f"{asset}-{offset}-repeat"))

    report = OOSCoverageService(_Repository(rows)).report(
        contract_family=DAILY_THRESHOLD_FAMILY,
    )

    assert report.coverage_passed is True
    assert report.independent_event_group_count == 20
    assert report.settlement_date_count == 7
    assert report.groups_per_asset == (
        ("BTC", 5),
        ("ETH", 5),
        ("SOL", 5),
        ("XRP", 5),
    )


def test_coverage_fails_closed_and_reports_missing_assets() -> None:
    rows = [
        _row(
            "BTC",
            datetime(2026, 7, 1, 16, 0, tzinfo=UTC),
            signal_id="btc-1",
        ),
        {"signal_id": "malformed", "asset": "ETH", "deadline": None},
    ]

    report = OOSCoverageService(_Repository(rows)).report(
        contract_family=DAILY_THRESHOLD_FAMILY,
        workflow_version="market-workflow-v2",
    )

    assert report.coverage_passed is False
    assert report.invalid_labeled_row_count == 1
    assert "insufficient_oos_groups:1/20" in report.reasons
    assert "insufficient_oos_dates:1/7" in report.reasons
    assert "insufficient_asset_groups:XRP:0/4" in report.reasons
