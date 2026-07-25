"""Mechanical external-payload schema checks for shadow evidence."""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from crypto_threshold.storage.repositories import Repository

SCHEMA_DRIFT_SOURCE_VERSION = "external-payload-schema-monitor-v1"


@dataclass(frozen=True)
class PayloadSchemaIssue:
    payload_id: int
    contract: str
    code: str


@dataclass(frozen=True)
class SchemaDriftReport:
    status: str
    boundary_payload_id: int
    last_payload_id: int
    scanned_payload_count: int
    contract_counts: dict[str, int]
    source_versions: dict[str, tuple[str, ...]]
    issues: tuple[PayloadSchemaIssue, ...]
    source_version: str = SCHEMA_DRIFT_SOURCE_VERSION

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "boundary_payload_id": self.boundary_payload_id,
            "last_payload_id": self.last_payload_id,
            "scanned_payload_count": self.scanned_payload_count,
            "contract_counts": self.contract_counts,
            "source_versions": {
                contract: list(versions)
                for contract, versions in self.source_versions.items()
            },
            "issues": [
                {
                    "payload_id": issue.payload_id,
                    "contract": issue.contract,
                    "code": issue.code,
                }
                for issue in self.issues
            ],
            "source_version": self.source_version,
        }


class ExternalPayloadSchemaMonitor:
    """Inspect only raw public payloads persisted during one shadow cycle."""

    def __init__(self, repository: Repository) -> None:
        self.repository = repository

    def capture_boundary(self) -> int:
        return self.repository.max_external_payload_id()

    def inspect_after(self, boundary_payload_id: int) -> SchemaDriftReport:
        rows = self.repository.external_payload_rows_after(boundary_payload_id)
        counts: Counter[str] = Counter()
        versions: defaultdict[str, set[str]] = defaultdict(set)
        issues: list[PayloadSchemaIssue] = []
        last_payload_id = boundary_payload_id
        for row in rows:
            payload_id = int(row["id"])
            source = str(row["source"])
            payload_kind = str(row["payload_kind"])
            contract = f"{source}/{payload_kind}"
            last_payload_id = max(last_payload_id, payload_id)
            counts[contract] += 1
            versions[contract].add(str(row["source_version"]))
            try:
                payload = json.loads(str(row["raw_payload"]))
            except json.JSONDecodeError:
                issues.append(PayloadSchemaIssue(payload_id, contract, "invalid_json"))
                continue
            validator = _VALIDATORS.get((source, payload_kind))
            if validator is None:
                issues.append(
                    PayloadSchemaIssue(payload_id, contract, "unknown_payload_contract")
                )
                continue
            issues.extend(
                PayloadSchemaIssue(payload_id, contract, code)
                for code in validator(payload)
            )
        status = "drift_detected" if issues else "ok" if rows else "no_payloads"
        return SchemaDriftReport(
            status=status,
            boundary_payload_id=boundary_payload_id,
            last_payload_id=last_payload_id,
            scanned_payload_count=len(rows),
            contract_counts=dict(sorted(counts.items())),
            source_versions={
                contract: tuple(sorted(contract_versions))
                for contract, contract_versions in sorted(versions.items())
            },
            issues=tuple(issues[:100]),
        )


def _gamma_market(payload: Any) -> list[str]:
    if not isinstance(payload, dict):
        return ["root_not_object"]
    issues: list[str] = []
    if not _nonempty(payload.get("id") or payload.get("conditionId")):
        issues.append("missing_market_identifier")
    if not _nonempty(payload.get("question") or payload.get("title")):
        issues.append("missing_question")
    issues.extend(_optional_listish(payload, "outcomes"))
    if "clobTokenIds" in payload:
        issues.extend(_optional_listish(payload, "clobTokenIds"))
    elif "tokenIds" in payload:
        issues.extend(_optional_listish(payload, "tokenIds"))
    elif "tokens" in payload and not isinstance(payload["tokens"], list):
        issues.append("tokens_not_list")
    for key in ("events",):
        if key in payload and not isinstance(payload[key], (list, str)):
            issues.append(f"{key}_not_listish")
    return issues


def _gamma_event_context(payload: Any) -> list[str]:
    if not isinstance(payload, dict):
        return ["root_not_object"]
    events = payload.get("events")
    if not isinstance(events, list):
        return ["events_not_list"]
    if any(not isinstance(event, dict) for event in events):
        return ["event_not_object"]
    return []


def _clob_book(payload: Any) -> list[str]:
    if not isinstance(payload, dict):
        return ["root_not_object"]
    issues: list[str] = []
    for side in ("bids", "asks"):
        levels = payload.get(side)
        if not isinstance(levels, list):
            issues.append(f"{side}_not_list")
            continue
        for level in levels:
            if isinstance(level, dict):
                if "price" not in level or not ({"size", "quantity"} & level.keys()):
                    issues.append(f"{side}_level_missing_price_or_size")
                    break
            elif not isinstance(level, (list, tuple)) or len(level) < 2:
                issues.append(f"{side}_level_invalid")
                break
    timestamp = payload.get("timestamp")
    if timestamp is None or timestamp == "":
        issues.append("missing_timestamp")
    return issues


def _fee_schedule(payload: Any) -> list[str]:
    if not isinstance(payload, dict):
        return ["root_not_object"]
    fee = payload.get("fd") or payload.get("feeSchedule") or payload.get("fee_schedule")
    if not isinstance(fee, dict):
        return ["fee_schedule_not_object"]
    issues: list[str] = []
    if not ({"r", "rate"} & fee.keys()):
        issues.append("missing_fee_rate")
    if not ({"e", "exponent"} & fee.keys()):
        issues.append("missing_fee_exponent")
    if not ({"to", "takerOnly"} & fee.keys()):
        issues.append("missing_fee_taker_only")
    return issues


def _binance_klines(payload: Any) -> list[str]:
    if not isinstance(payload, list):
        return ["root_not_list"]
    if not payload:
        return ["empty_klines"]
    if any(not isinstance(item, list) or len(item) < 7 for item in payload):
        return ["malformed_kline"]
    return []


def _coinbase_spot(payload: Any) -> list[str]:
    if not isinstance(payload, dict):
        return ["root_not_object"]
    data = payload.get("data")
    if not isinstance(data, dict):
        return ["data_not_object"]
    missing = [
        key
        for key in ("base", "currency", "amount")
        if not _nonempty(data.get(key))
    ]
    return [f"missing_data_{key}" for key in missing]


def _chainlink_tick(payload: Any) -> list[str]:
    if not isinstance(payload, dict):
        return ["root_not_object"]
    required = {
        "provider",
        "pair",
        "candle_interval",
        "price_field",
        "price",
        "provider_timestamp",
        "received_at",
        "source_version",
    }
    return [
        f"missing_{key}"
        for key in sorted(required)
        if not _nonempty(payload.get(key))
    ]


def _chainlink_tick_window(payload: Any) -> list[str]:
    if not isinstance(payload, dict):
        return ["root_not_object"]
    if not isinstance(payload.get("window_seconds"), int):
        return ["window_seconds_not_integer"]
    if not isinstance(payload.get("sample_seconds"), int):
        return ["sample_seconds_not_integer"]
    ticks = payload.get("ticks")
    if not isinstance(ticks, list):
        return ["ticks_not_list"]
    if not ticks:
        return ["empty_tick_window"]
    issues: list[str] = []
    for tick in ticks:
        issues.extend(_chainlink_tick(tick))
        if issues:
            break
    return issues


def _gamma_resolution_event(payload: Any) -> list[str]:
    if not isinstance(payload, dict):
        return ["root_not_object"]
    issues: list[str] = []
    if not _nonempty(payload.get("id")):
        issues.append("missing_event_identifier")
    if not isinstance(payload.get("markets"), list):
        issues.append("markets_not_list")
    return issues


def _optional_listish(payload: dict[str, Any], key: str) -> list[str]:
    if key not in payload:
        return []
    value = payload[key]
    if isinstance(value, list):
        return []
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return [f"{key}_invalid_json"]
        return [] if isinstance(parsed, list) else [f"{key}_not_list"]
    return [f"{key}_not_listish"]


def _nonempty(value: Any) -> bool:
    return value is not None and bool(str(value).strip())


_VALIDATORS: dict[tuple[str, str], Callable[[Any], list[str]]] = {
    ("gamma", "market"): _gamma_market,
    ("gamma", "event_context"): _gamma_event_context,
    ("polymarket_clob", "yes_book"): _clob_book,
    ("polymarket_clob", "no_book"): _clob_book,
    ("polymarket_clob", "up_book"): _clob_book,
    ("polymarket_clob", "down_book"): _clob_book,
    ("polymarket_clob", "market_info_fee_schedule"): _fee_schedule,
    ("binance", "settlement_klines_1m"): _binance_klines,
    ("binance", "volatility_klines_1d"): _binance_klines,
    ("binance", "settlement_candle_1m_close"): _binance_klines,
    ("coinbase", "sanity_spot"): _coinbase_spot,
    ("chainlink", "chainlink_start_price"): _chainlink_tick,
    ("chainlink", "chainlink_current_price"): _chainlink_tick,
    ("chainlink", "chainlink_volatility_window"): _chainlink_tick_window,
    ("gamma", "chainlink_resolution_event"): _gamma_resolution_event,
}
