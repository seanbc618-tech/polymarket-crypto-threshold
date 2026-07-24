"""Immutable, offline-verifiable replay dataset construction."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import uuid4

from crypto_threshold.domain.research import ReplayBuildResult, ReplayVerificationResult
from crypto_threshold.storage.repositories import Repository

REPLAY_SOURCE_VERSION = "replay-manifest-v1"
REQUIRED_INPUT_ROLES = {
    "market",
    "yes_book",
    "no_book",
    "market_info_fee_schedule",
    "settlement_klines_1m",
    "volatility_klines_1d",
    "sanity_spot",
}


class ReplayService:
    """Seal exact historical decision inputs and verify them without network I/O."""

    def __init__(
        self,
        repository: Repository,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.repository = repository
        self.clock = clock or (lambda: datetime.now(UTC))

    def build(self, name: str) -> ReplayBuildResult:
        if not name.strip():
            raise ValueError("replay dataset name is required")
        items: list[dict[str, Any]] = []
        rejected: list[str] = []
        for signal in self.repository.replay_candidate_rows():
            item, reason = self._candidate_item(signal, ordinal=len(items))
            if item is None:
                rejected.append(f"{signal['signal_id']}:{reason}")
                continue
            items.append(item)

        config = {
            "required_input_roles": sorted(REQUIRED_INPUT_ROLES),
            "source_version": REPLAY_SOURCE_VERSION,
        }
        manifest_hash = _hash(
            {
                "name": name,
                "config": config,
                "items": [
                    {
                        "signal_id": item["signal_id"],
                        "label_id": item["label_id"],
                        "feature_hash": item["feature_hash"],
                        "input_manifest_hash": item["input_manifest_hash"],
                    }
                    for item in items
                ],
            }
        )
        dataset_id = f"replay:{uuid4()}"
        self.repository.seal_replay_dataset(
            dataset_id=dataset_id,
            name=name,
            manifest_hash=manifest_hash,
            config=config,
            source_version=REPLAY_SOURCE_VERSION,
            created_at=_utc(self.clock()),
            items=tuple(items),
        )
        return ReplayBuildResult(
            dataset_id=dataset_id,
            name=name,
            status="sealed",
            item_count=len(items),
            manifest_hash=manifest_hash,
            rejection_reasons=tuple(rejected),
        )

    def verify(self, dataset: str) -> ReplayVerificationResult:
        row = self.repository.get_replay_dataset(dataset)
        if row is None:
            raise ValueError(f"unknown replay dataset: {dataset}")
        items = self.repository.replay_item_rows(str(row["dataset_id"]))
        reasons: list[str] = []
        if not items:
            reasons.append("empty_dataset")
        manifest_items: list[dict[str, str]] = []
        verified = 0
        for item in items:
            feature = json.loads(str(item["feature_payload"]))
            if _hash(feature) != str(item["feature_hash"]):
                reasons.append(f"{item['signal_id']}:feature_hash_mismatch")
                continue
            input_rows = self.repository.signal_input_rows(str(item["signal_id"]))
            if _input_manifest_hash(input_rows) != str(item["input_manifest_hash"]):
                reasons.append(f"{item['signal_id']}:input_manifest_hash_mismatch")
                continue
            if not _ev_matches(feature):
                reasons.append(f"{item['signal_id']}:net_ev_mismatch")
                continue
            verified += 1
            manifest_items.append(
                {
                    "signal_id": str(item["signal_id"]),
                    "label_id": str(item["label_id"]),
                    "feature_hash": str(item["feature_hash"]),
                    "input_manifest_hash": str(item["input_manifest_hash"]),
                }
            )
        config = json.loads(str(row["config_json"]))
        expected_manifest = _hash(
            {"name": str(row["name"]), "config": config, "items": manifest_items}
        )
        if verified == len(items) and expected_manifest != str(row["manifest_hash"]):
            reasons.append("dataset_manifest_hash_mismatch")
        return ReplayVerificationResult(
            dataset_id=str(row["dataset_id"]),
            item_count=len(items),
            verified_count=verified,
            ok=bool(items) and verified == len(items) and not reasons,
            reasons=tuple(reasons),
        )

    def _candidate_item(self, signal: Any, *, ordinal: int) -> tuple[dict[str, Any] | None, str]:
        decision_at = _time(signal["observed_at"])
        deadline = _time(signal["deadline"])
        label_available_at = _time(signal["label_received_at"])
        label_target = _time(signal["label_target_time_utc"])
        if deadline != label_target:
            return None, "settlement_target_mismatch"
        if decision_at >= deadline:
            return None, "non_predeadline_signal"
        if label_available_at <= decision_at:
            return None, "label_not_strictly_after_decision"
        run_id = str(signal["analysis_run_id"] or "")
        if not run_id:
            return None, "missing_analysis_run_id"
        inputs = self.repository.signal_input_rows(str(signal["signal_id"]))
        roles = {str(row["input_role"]) for row in inputs}
        if not REQUIRED_INPUT_ROLES.issubset(roles):
            return None, "incomplete_exact_inputs"
        for row in inputs:
            if str(row["analysis_run_id"] or "") != run_id:
                return None, "cross_run_input"
            if _time(row["received_at"]) > decision_at:
                return None, "input_received_after_decision"

        feature = _feature_payload(signal)
        return (
            {
                "ordinal": ordinal,
                "signal_id": str(signal["signal_id"]),
                "label_id": str(signal["label_id"]),
                "decision_at": decision_at.isoformat(),
                "label_available_at": label_available_at.isoformat(),
                "feature_payload": _json(feature),
                "feature_hash": _hash(feature),
                "input_manifest_hash": _input_manifest_hash(inputs),
            },
            "",
        )


def _feature_payload(row: Any) -> dict[str, Any]:
    fields = (
        "signal_id",
        "market_id",
        "asset",
        "threshold",
        "deadline",
        "estimated_probability",
        "probability_low",
        "probability_high",
        "yes_midpoint",
        "no_midpoint",
        "yes_ask_vwap",
        "no_ask_vwap",
        "target_size_usdc",
        "fee_rate",
        "yes_fee_per_share",
        "no_fee_per_share",
        "yes_net_ev",
        "no_net_ev",
        "selected_outcome",
        "net_ev",
        "model_name",
        "model_version",
        "observed_at",
    )
    payload = {field: row[field] for field in fields}
    payload["outcome_yes"] = bool(row["outcome_yes"])
    return payload


def _input_manifest_hash(rows: list[Any]) -> str:
    manifest = []
    for row in rows:
        raw = str(row["raw_payload"])
        try:
            raw_payload: Any = json.loads(raw)
        except json.JSONDecodeError:
            raw_payload = raw
        manifest.append(
            {
                "id": int(row["id"]),
                "analysis_run_id": row["analysis_run_id"],
                "input_role": row["input_role"],
                "source": row["source"],
                "payload_kind": row["payload_kind"],
                "observed_at": row["observed_at"],
                "received_at": row["received_at"],
                "source_version": row["source_version"],
                "raw_payload": raw_payload,
            }
        )
    return _hash(manifest)


def _ev_matches(feature: dict[str, Any]) -> bool:
    probability = Decimal(str(feature["estimated_probability"]))
    yes = probability - Decimal(str(feature["yes_ask_vwap"])) - Decimal(
        str(feature["yes_fee_per_share"])
    )
    no = Decimal("1") - probability - Decimal(str(feature["no_ask_vwap"])) - Decimal(
        str(feature["no_fee_per_share"])
    )
    tolerance = Decimal("0.000000000001")
    return (
        abs(yes - Decimal(str(feature["yes_net_ev"]))) <= tolerance
        and abs(no - Decimal(str(feature["no_net_ev"]))) <= tolerance
    )


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _hash(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _time(value: Any) -> datetime:
    if not value:
        raise ValueError("required replay timestamp is missing")
    return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
