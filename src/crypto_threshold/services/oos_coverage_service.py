"""Mechanical independent OOS event-group coverage checks."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from crypto_threshold.domain.assets import DAILY_THRESHOLD_ASSETS, SHORT_UPDOWN_ASSETS
from crypto_threshold.domain.probability import (
    DAILY_WORKFLOW_SOURCE_VERSION,
    SHORT_UPDOWN_WORKFLOW_SOURCE_VERSION,
)
from crypto_threshold.domain.rules import DAILY_THRESHOLD_FAMILY, SHORT_UPDOWN_FAMILY
from crypto_threshold.storage.repositories import Repository

OOS_COVERAGE_SOURCE_VERSION = "independent-oos-coverage-v1"
DEFAULT_MIN_OOS_GROUPS = 20
DEFAULT_MIN_OOS_DATES = 7
DEFAULT_MIN_GROUPS_PER_ASSET = 4
DEFAULT_REQUIRED_DAILY_ASSETS = tuple(sorted(DAILY_THRESHOLD_ASSETS))
# HYPE has no validated CEX source and is intentionally not a default short
# Up/Down coverage requirement.
DEFAULT_REQUIRED_SHORT_ASSETS = tuple(
    sorted(asset for asset in SHORT_UPDOWN_ASSETS if asset != "HYPE")
)


@dataclass(frozen=True)
class OOSCoverageReport:
    """Event-group coverage, separate from any promotion or live-readiness gate."""

    contract_family: str
    workflow_version: str
    labeled_signal_row_count: int
    invalid_labeled_row_count: int
    independent_event_group_count: int
    settlement_date_count: int
    assets: tuple[str, ...]
    groups_per_asset: tuple[tuple[str, int], ...]
    required_assets: tuple[str, ...]
    minimum_event_groups: int
    minimum_settlement_dates: int
    minimum_groups_per_asset: int
    coverage_passed: bool
    reasons: tuple[str, ...]
    promotion_allowed: bool = False
    source_version: str = OOS_COVERAGE_SOURCE_VERSION


class OOSCoverageService:
    """Count settled, independently grouped labels without mutating evidence."""

    def __init__(self, repository: Repository) -> None:
        self.repository = repository

    def report(
        self,
        *,
        contract_family: str,
        workflow_version: str | None = None,
        minimum_event_groups: int = DEFAULT_MIN_OOS_GROUPS,
        minimum_settlement_dates: int = DEFAULT_MIN_OOS_DATES,
        minimum_groups_per_asset: int = DEFAULT_MIN_GROUPS_PER_ASSET,
        required_assets: tuple[str, ...] | None = None,
    ) -> OOSCoverageReport:
        version = workflow_version or _canonical_workflow_version(contract_family)
        _validate_minimum("minimum_event_groups", minimum_event_groups)
        _validate_minimum("minimum_settlement_dates", minimum_settlement_dates)
        _validate_minimum("minimum_groups_per_asset", minimum_groups_per_asset)
        normalized_assets = _normalize_assets(
            required_assets
            if required_assets is not None
            else _default_required_assets(contract_family)
        )
        rows = self.repository.settled_strategy_signal_rows(
            contract_family=contract_family,
            workflow_version=version,
        )

        groups: set[tuple[str, str, str, str]] = set()
        groups_by_asset: dict[str, set[tuple[str, str, str, str]]] = defaultdict(set)
        invalid_rows = 0
        for row in rows:
            parsed = _event_group(row, contract_family)
            if parsed is None:
                invalid_rows += 1
                continue
            group, asset = parsed
            groups.add(group)
            groups_by_asset[asset].add(group)

        assets = tuple(sorted(groups_by_asset))
        groups_per_asset = tuple(
            sorted((asset, len(asset_groups)) for asset, asset_groups in groups_by_asset.items())
        )
        reasons: list[str] = []
        if len(groups) < minimum_event_groups:
            reasons.append(
                f"insufficient_oos_groups:{len(groups)}/{minimum_event_groups}"
            )
        dates = {group[3][:10] for group in groups}
        if len(dates) < minimum_settlement_dates:
            reasons.append(
                f"insufficient_oos_dates:{len(dates)}/{minimum_settlement_dates}"
            )
        counts = dict(groups_per_asset)
        for asset in normalized_assets:
            count = counts.get(asset, 0)
            if count < minimum_groups_per_asset:
                reasons.append(
                    f"insufficient_asset_groups:{asset}:{count}/"
                    f"{minimum_groups_per_asset}"
                )

        return OOSCoverageReport(
            contract_family=contract_family,
            workflow_version=version,
            labeled_signal_row_count=len(rows),
            invalid_labeled_row_count=invalid_rows,
            independent_event_group_count=len(groups),
            settlement_date_count=len(dates),
            assets=assets,
            groups_per_asset=groups_per_asset,
            required_assets=normalized_assets,
            minimum_event_groups=minimum_event_groups,
            minimum_settlement_dates=minimum_settlement_dates,
            minimum_groups_per_asset=minimum_groups_per_asset,
            coverage_passed=not reasons,
            reasons=tuple(reasons),
        )


def _event_group(row: object, contract_family: str) -> tuple[tuple[str, str, str, str], str] | None:
    value: Any = row
    try:
        asset = str(value["asset"] or "").strip().upper()
        interval = str(value["label_interval"] or "").strip()
        deadline = _parse_datetime(value["deadline"])
    except (KeyError, TypeError, ValueError):
        return None
    if not asset or not interval or deadline is None:
        return None
    group = (contract_family, asset, interval, deadline.isoformat())
    return group, asset


def _parse_datetime(value: object) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(UTC)


def _validate_minimum(name: str, value: int) -> None:
    if value < 1:
        raise ValueError(f"{name} must be positive")


def _normalize_assets(values: tuple[str, ...]) -> tuple[str, ...]:
    normalized = tuple(sorted({value.strip().upper() for value in values if value.strip()}))
    if not normalized:
        raise ValueError("required_assets must be non-empty")
    return normalized


def _default_required_assets(contract_family: str) -> tuple[str, ...]:
    if contract_family == DAILY_THRESHOLD_FAMILY:
        return DEFAULT_REQUIRED_DAILY_ASSETS
    if contract_family == SHORT_UPDOWN_FAMILY:
        return DEFAULT_REQUIRED_SHORT_ASSETS
    raise ValueError(f"unsupported contract family: {contract_family}")


def _canonical_workflow_version(contract_family: str) -> str:
    if contract_family == DAILY_THRESHOLD_FAMILY:
        return DAILY_WORKFLOW_SOURCE_VERSION
    if contract_family == SHORT_UPDOWN_FAMILY:
        return SHORT_UPDOWN_WORKFLOW_SOURCE_VERSION
    raise ValueError(f"unsupported contract family: {contract_family}")
