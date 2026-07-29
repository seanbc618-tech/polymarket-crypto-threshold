"""Immutable, offline-verifiable replay dataset construction."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import uuid4

from crypto_threshold.domain.research import (
    ReplayBuildResult,
    ReplayPlanResult,
    ReplayVerificationResult,
)
from crypto_threshold.domain.rules import DAILY_THRESHOLD_FAMILY, SHORT_UPDOWN_FAMILY
from crypto_threshold.storage.repositories import Repository

REPLAY_SOURCE_VERSION = "replay-manifest-v3"
SELECTION_REPLAY_SOURCE_VERSIONS = {
    "replay-manifest-v2",
    REPLAY_SOURCE_VERSION,
}
TIMESTAMP_BOUND_REPLAY_SOURCE_VERSIONS = {REPLAY_SOURCE_VERSION}
DAILY_REQUIRED_INPUT_ROLES = {
    "market",
    "yes_book",
    "no_book",
    "market_info_fee_schedule",
    "settlement_klines_1m",
    "volatility_klines_1d",
    "sanity_spot",
}
SHORT_REQUIRED_INPUT_ROLES = {
    "market",
    "up_book",
    "down_book",
    "market_info_fee_schedule",
    "cex_direction_klines_1m",
    "cex_direction_model",
}
# Compatibility export for the original daily-threshold replay tests.
REQUIRED_INPUT_ROLES = DAILY_REQUIRED_INPUT_ROLES


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

    def build(
        self,
        name: str,
        *,
        contract_family: str = DAILY_THRESHOLD_FAMILY,
        training_label_count: int | None = None,
        training_dataset: str | None = None,
    ) -> ReplayBuildResult:
        if not name.strip():
            raise ValueError("replay dataset name is required")
        if training_label_count is not None and training_dataset is not None:
            raise ValueError(
                "training label count and training dataset are mutually exclusive"
            )
        _validate_training_label_count(training_label_count)
        required_roles = _required_roles(contract_family)
        candidate_items, rejected = self._eligible_candidates(
            contract_family=contract_family,
            required_roles=required_roles,
        )
        eligible_labels = _label_entries(candidate_items)
        training_reference = (
            self._training_reference(training_dataset, candidate_items)
            if training_dataset is not None
            else None
        )
        if (
            training_label_count is not None
            and len(eligible_labels) < training_label_count
        ):
            raise ValueError(
                "training replay requires "
                f"{training_label_count} eligible unique labels; "
                f"found {len(eligible_labels)}"
            )
        selected_labels = (
            eligible_labels[:training_label_count]
            if training_label_count is not None
            else eligible_labels
        )
        selected_label_ids = {
            str(label["label_id"]) for label in selected_labels
        }
        items: list[dict[str, Any]] = []
        for candidate in candidate_items:
            if str(candidate["label_id"]) not in selected_label_ids:
                continue
            items.append({**candidate, "ordinal": len(items)})
        if training_label_count is not None:
            rejected.extend(
                f"{label['label_id']}:label_after_training_cutoff"
                for label in eligible_labels[training_label_count:]
            )

        selection_cutoff = (
            dict(selected_labels[-1])
            if training_label_count is not None and selected_labels
            else None
        )
        reported_training_cutoff = (
            selection_cutoff
            if selection_cutoff is not None
            else (
                dict(training_reference["training_cutoff"])
                if training_reference is not None
                else None
            )
        )

        config = {
            "contract_family": contract_family,
            "required_input_roles": sorted(required_roles),
            "selection": {
                "mode": (
                    "first_n_eligible_labels"
                    if training_label_count is not None
                    else "all_eligible_labels"
                ),
                "requested_unique_label_count": training_label_count,
                "selected_unique_label_count": len(selected_labels),
                "selected_labels": selected_labels,
                "training_cutoff": selection_cutoff,
            },
            "source_version": REPLAY_SOURCE_VERSION,
        }
        if training_reference is not None:
            config["training_reference"] = training_reference
        manifest_hash = _hash(
            {
                "name": name,
                "config": config,
                "items": [
                    _manifest_item(item, source_version=REPLAY_SOURCE_VERSION)
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
            unique_label_count=len(selected_labels),
            training_cutoff_at=(
                _time(reported_training_cutoff["label_available_at"])
                if reported_training_cutoff is not None
                else None
            ),
            training_cutoff_label_id=(
                str(reported_training_cutoff["label_id"])
                if reported_training_cutoff is not None
                else None
            ),
            rejection_reasons=tuple(rejected),
        )

    def plan(
        self,
        *,
        training_label_count: int,
        contract_family: str = DAILY_THRESHOLD_FAMILY,
    ) -> ReplayPlanResult:
        """Evaluate an exact training boundary without persisting a dataset."""
        _validate_training_label_count(training_label_count)
        required_roles = _required_roles(contract_family)
        candidate_items, rejected = self._eligible_candidates(
            contract_family=contract_family,
            required_roles=required_roles,
        )
        eligible_labels = _label_entries(candidate_items)
        ready = len(eligible_labels) >= training_label_count
        selected_count = min(len(eligible_labels), training_label_count)
        cutoff = eligible_labels[training_label_count - 1] if ready else None
        return ReplayPlanResult(
            contract_family=contract_family,
            requested_unique_label_count=training_label_count,
            ready=ready,
            eligible_item_count=len(candidate_items),
            eligible_unique_label_count=len(eligible_labels),
            selected_unique_label_count=selected_count,
            training_cutoff_at=(
                _time(cutoff["label_available_at"]) if cutoff is not None else None
            ),
            training_cutoff_label_id=(
                str(cutoff["label_id"]) if cutoff is not None else None
            ),
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
        source_version = str(row["source_version"])
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
                _manifest_item(item, source_version=source_version)
            )
        config = json.loads(str(row["config_json"]))
        reasons.extend(_selection_issues(config, items))
        reasons.extend(
            self._training_reference_issues(
                config,
                items,
                dataset_id=str(row["dataset_id"]),
            )
        )
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

    def _eligible_candidates(
        self,
        *,
        contract_family: str,
        required_roles: set[str],
    ) -> tuple[list[dict[str, Any]], list[str]]:
        candidates: list[dict[str, Any]] = []
        rejected: list[str] = []
        for signal in self.repository.replay_candidate_rows(
            contract_family=contract_family
        ):
            item, reason = self._candidate_item(
                signal,
                ordinal=0,
                required_roles=required_roles,
            )
            if item is None:
                rejected.append(f"{signal['signal_id']}:{reason}")
                continue
            candidates.append(item)
        return candidates, rejected

    def _training_reference(
        self,
        dataset: str,
        candidate_items: list[dict[str, Any]],
    ) -> dict[str, Any]:
        row = self.repository.get_replay_dataset(dataset)
        if row is None:
            raise ValueError(f"unknown training replay dataset: {dataset}")
        dataset_id = str(row["dataset_id"])
        verification = self.verify(dataset_id)
        if not verification.ok:
            raise ValueError(
                f"training replay dataset failed verification: {dataset_id}"
            )
        config = json.loads(str(row["config_json"]))
        selection = config.get("selection") if isinstance(config, dict) else None
        if (
            not isinstance(selection, dict)
            or selection.get("mode") != "first_n_eligible_labels"
        ):
            raise ValueError(
                "training replay dataset must use first_n_eligible_labels selection"
            )
        if config.get("training_reference") is not None:
            raise ValueError("nested training replay references are not supported")
        training_items = self.repository.replay_item_rows(dataset_id)
        candidate_identities = {_item_identity(item) for item in candidate_items}
        missing = [
            str(item["signal_id"])
            for item in training_items
            if _item_identity(item) not in candidate_identities
        ]
        if missing:
            raise ValueError(
                "combined replay is missing exact frozen training items: "
                + ",".join(missing[:5])
            )
        return {
            "dataset_id": dataset_id,
            "name": str(row["name"]),
            "manifest_hash": str(row["manifest_hash"]),
            "source_version": str(row["source_version"]),
            "selected_unique_label_count": selection.get(
                "selected_unique_label_count"
            ),
            "selected_labels": selection.get("selected_labels"),
            "training_cutoff": selection.get("training_cutoff"),
        }

    def _training_reference_issues(
        self,
        config: Any,
        items: list[Any],
        *,
        dataset_id: str,
    ) -> list[str]:
        if not isinstance(config, dict):
            return []
        reference = config.get("training_reference")
        if reference is None:
            return []
        if not isinstance(reference, dict):
            return ["invalid_training_reference"]
        training_id = str(reference.get("dataset_id") or "")
        if not training_id:
            return ["missing_training_dataset_id"]
        if training_id == dataset_id:
            return ["self_referential_training_dataset"]
        row = self.repository.get_replay_dataset(training_id)
        if row is None:
            return ["missing_training_dataset"]
        training_config = json.loads(str(row["config_json"]))
        selection = (
            training_config.get("selection")
            if isinstance(training_config, dict)
            else None
        )
        if not isinstance(selection, dict):
            return ["invalid_training_dataset_selection"]
        issues: list[str] = []
        expected_reference = {
            "dataset_id": str(row["dataset_id"]),
            "name": str(row["name"]),
            "manifest_hash": str(row["manifest_hash"]),
            "source_version": str(row["source_version"]),
            "selected_unique_label_count": selection.get(
                "selected_unique_label_count"
            ),
            "selected_labels": selection.get("selected_labels"),
            "training_cutoff": selection.get("training_cutoff"),
        }
        if reference != expected_reference:
            issues.append("training_reference_manifest_mismatch")
        if selection.get("mode") != "first_n_eligible_labels":
            issues.append("training_dataset_not_frozen")
        if training_config.get("training_reference") is not None:
            issues.append("nested_training_reference")
        else:
            verification = self.verify(training_id)
            if not verification.ok:
                issues.append("training_dataset_verification_failed")
        current_identities = {_item_identity(item) for item in items}
        training_items = self.repository.replay_item_rows(training_id)
        if any(
            _item_identity(item) not in current_identities
            for item in training_items
        ):
            issues.append("combined_replay_missing_training_items")
        return issues

    def _candidate_item(
        self,
        signal: Any,
        *,
        ordinal: int,
        required_roles: set[str],
    ) -> tuple[dict[str, Any] | None, str]:
        decision_at = _time(signal["observed_at"])
        deadline = _time(signal["deadline"])
        label_available_at = _time(signal["label_received_at"])
        label_target = _time(signal["label_target_time_utc"])
        if deadline != label_target:
            return None, "settlement_target_mismatch"
        if (
            str(signal["contract_family"]) == DAILY_THRESHOLD_FAMILY
            and not _same_decimal(signal["threshold"], signal["label_strike"])
        ):
            return None, "settlement_threshold_mismatch"
        if (
            str(signal["contract_family"]) == SHORT_UPDOWN_FAMILY
            and signal["threshold"] is not None
        ):
            return None, "unexpected_short_signal_threshold"
        if decision_at >= deadline:
            return None, "non_predeadline_signal"
        if label_available_at <= decision_at:
            return None, "label_not_strictly_after_decision"
        run_id = str(signal["analysis_run_id"] or "")
        if not run_id:
            return None, "missing_analysis_run_id"
        inputs = self.repository.signal_input_rows(str(signal["signal_id"]))
        roles = {str(row["input_role"]) for row in inputs}
        if not required_roles.issubset(roles):
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
        "contract_family",
        "affirmative_outcome",
        "negative_outcome",
    )
    payload = {field: row[field] for field in fields}
    payload["outcome_yes"] = bool(row["outcome_yes"])
    return payload


def _required_roles(contract_family: str) -> set[str]:
    if contract_family == DAILY_THRESHOLD_FAMILY:
        return DAILY_REQUIRED_INPUT_ROLES
    if contract_family == SHORT_UPDOWN_FAMILY:
        return SHORT_REQUIRED_INPUT_ROLES
    raise ValueError(f"unsupported replay contract family: {contract_family}")


def _validate_training_label_count(value: int | None) -> None:
    if value is not None and value < 1:
        raise ValueError("training label count must be positive")


def _label_entries(items: list[Any]) -> list[dict[str, str]]:
    labels: dict[str, str] = {}
    for item in items:
        label_id = str(item["label_id"] or "")
        if not label_id:
            raise ValueError("eligible replay item is missing label_id")
        available_at = _time(item["label_available_at"]).isoformat()
        existing = labels.get(label_id)
        if existing is not None and existing != available_at:
            raise ValueError(
                f"replay label has inconsistent availability timestamp: {label_id}"
            )
        labels[label_id] = available_at
    return [
        {"label_available_at": available_at, "label_id": label_id}
        for label_id, available_at in sorted(
            labels.items(),
            key=lambda item: (_time(item[1]), item[0]),
        )
    ]


def _selection_issues(config: Any, items: list[Any]) -> list[str]:
    if not isinstance(config, dict):
        return ["invalid_replay_config"]
    if config.get("source_version") not in SELECTION_REPLAY_SOURCE_VERSIONS:
        return []
    selection = config.get("selection")
    if not isinstance(selection, dict):
        return ["missing_selection_manifest"]
    issues: list[str] = []
    actual_labels = _label_entries(items)
    selected_labels = selection.get("selected_labels")
    if selected_labels != actual_labels:
        issues.append("selected_label_manifest_mismatch")
    if selection.get("selected_unique_label_count") != len(actual_labels):
        issues.append("selected_unique_label_count_mismatch")
    mode = selection.get("mode")
    requested = selection.get("requested_unique_label_count")
    cutoff = selection.get("training_cutoff")
    if mode == "first_n_eligible_labels":
        if not isinstance(requested, int) or requested < 1:
            issues.append("invalid_requested_unique_label_count")
        elif requested != len(actual_labels):
            issues.append("requested_unique_label_count_mismatch")
        expected_cutoff = actual_labels[-1] if actual_labels else None
        if cutoff != expected_cutoff:
            issues.append("training_cutoff_mismatch")
    elif mode == "all_eligible_labels":
        if requested is not None:
            issues.append("unexpected_requested_unique_label_count")
        if cutoff is not None:
            issues.append("unexpected_training_cutoff")
    else:
        issues.append("invalid_selection_mode")
    return issues


def _manifest_item(item: Any, *, source_version: str) -> dict[str, str]:
    manifest = {
        "signal_id": str(item["signal_id"]),
        "label_id": str(item["label_id"]),
        "feature_hash": str(item["feature_hash"]),
        "input_manifest_hash": str(item["input_manifest_hash"]),
    }
    if source_version in TIMESTAMP_BOUND_REPLAY_SOURCE_VERSIONS:
        manifest["decision_at"] = _time(item["decision_at"]).isoformat()
        manifest["label_available_at"] = _time(
            item["label_available_at"]
        ).isoformat()
    return manifest


def _item_identity(item: Any) -> tuple[str, str, str, str, str, str]:
    return (
        str(item["signal_id"]),
        str(item["label_id"]),
        str(item["feature_hash"]),
        str(item["input_manifest_hash"]),
        _time(item["decision_at"]).isoformat(),
        _time(item["label_available_at"]).isoformat(),
    )


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
    if feature.get("contract_family") == SHORT_UPDOWN_FAMILY:
        yes_probability = Decimal(str(feature["probability_low"]))
        no_probability = Decimal("1") - Decimal(str(feature["probability_high"]))
    else:
        yes_probability = probability
        no_probability = Decimal("1") - probability
    yes = yes_probability - Decimal(str(feature["yes_ask_vwap"])) - Decimal(
        str(feature["yes_fee_per_share"])
    )
    no = no_probability - Decimal(str(feature["no_ask_vwap"])) - Decimal(
        str(feature["no_fee_per_share"])
    )
    tolerance = Decimal("0.000000000001")
    return (
        abs(yes - Decimal(str(feature["yes_net_ev"]))) <= tolerance
        and abs(no - Decimal(str(feature["no_net_ev"]))) <= tolerance
    )


def _same_decimal(left: Any, right: Any) -> bool:
    try:
        return Decimal(str(left)) == Decimal(str(right))
    except Exception:
        return False


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
