"""Mechanical Phase 2 acceptance checks over an existing evidence database.

This service never synthesizes production evidence, never mutates trading state,
and never claims acceptance without concrete SQLite rows that satisfy every gate.
"""

from __future__ import annotations

import json
import math
from contextlib import closing
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from crypto_threshold.services.calibration_service import (
    CALIBRATION_METHOD,
    CALIBRATION_MODEL_VERSION,
    CALIBRATION_SOURCE_VERSION,
)
from crypto_threshold.services.replay_service import ReplayService
from crypto_threshold.services.schema_drift_service import SCHEMA_DRIFT_SOURCE_VERSION
from crypto_threshold.storage.db import SCHEMA_VERSION, Database
from crypto_threshold.storage.repositories import Repository

MIN_CHRONOLOGICAL_TRAIN_LABELS = 30
MIN_SHADOW_HOURS = 72
MAX_SHADOW_GAP_SECONDS = 300
MAX_SHADOW_CYCLE_SECONDS = 900
BINANCE_STREAM_SOURCE_VERSION = "binance-spot-sdk-stream-v1"
SUPPORTED_EVIDENCE_SCHEMA_VERSIONS = frozenset({3, 4, 5})
BASE_REQUIRED_TABLES = frozenset(
    {
        "schema_meta",
        "markets",
        "resolution_rules",
        "external_payloads",
        "market_snapshots",
        "market_fee_schedules",
        "price_snapshots",
        "price_cross_checks",
        "analysis_signals",
        "analysis_signal_inputs",
        "settlement_labels",
        "replay_datasets",
        "replay_items",
        "calibration_runs",
        "paper_ledger",
        "shadow_cycles",
    }
)
VERSION_REQUIRED_TABLES = {
    5: frozenset({"settlement_attempts"}),
}
REQUIRED_TABLES = BASE_REQUIRED_TABLES.union(
    *(tables for tables in VERSION_REQUIRED_TABLES.values())
)
FORBIDDEN_TABLES = frozenset(
    {
        "orders",
        "fills",
        "positions",
        "open_orders",
        "trades",
        "order_fills",
        "signed_orders",
        "order_events",
        "order_intents",
        "exchange_orders",
        "authenticated_reconciliation",
        "reconciliation_mutations",
        "reconciliations",
        "private_keys",
        "wallet_secrets",
        "keychain_secrets",
        "signatures",
        "signers",
    }
)
FORBIDDEN_TABLE_FRAGMENTS = (
    "order",
    "fill",
    "position",
    "signer",
    "signature",
    "authenticated",
    "reconcil",
    "private_key",
)
METRIC_FAMILIES = ("raw", "calibrated", "market_midpoint_baseline")
METRIC_NAMES = ("brier", "log_loss", "ece")
VERDICT_ACCEPTED = "ACCEPTED"
VERDICT_PENDING = "PENDING/NOT ACCEPTED"


@dataclass(frozen=True)
class AcceptanceCheck:
    name: str
    ok: bool
    detail: str
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Phase2AcceptanceReport:
    verdict: str
    checks: tuple[AcceptanceCheck, ...]
    database_path: str
    generated_at: str

    @property
    def accepted(self) -> bool:
        return self.verdict == VERDICT_ACCEPTED


@dataclass(frozen=True)
class _CalibrationEvidence:
    run_id: str
    dataset_id: str
    min_train_size: int
    sample_count: int
    evaluated_count: int
    computed_evaluated_count: int
    metrics_json: str


class Phase2AcceptanceService:
    """Fail closed: any missing empirical gate yields PENDING/NOT ACCEPTED."""

    def __init__(self, repository: Repository) -> None:
        self.repository = repository

    @classmethod
    def from_db_path(cls, db_path: str | Path) -> Phase2AcceptanceService:
        path = Path(db_path).expanduser().resolve()
        if not path.is_file():
            raise FileNotFoundError(f"evidence database does not exist: {path}")
        database = Database(path, read_only=True)
        with closing(database.connect()) as connection:
            connection.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()
        return cls(Repository(database))

    def evaluate(self) -> Phase2AcceptanceReport:
        checks = [
            self._check_schema(),
            self._check_no_trading_mutation_surface(),
            self._check_replay(),
            self._check_training_and_oos(),
            self._check_metrics(),
            self._check_shadow_coverage(),
            self._check_cycle_fallback_rejection_paper(),
            self._check_schema_drift_monitoring(),
            self._check_binance_stream(),
        ]
        accepted = all(check.ok for check in checks)
        return Phase2AcceptanceReport(
            verdict=VERDICT_ACCEPTED if accepted else VERDICT_PENDING,
            checks=tuple(checks),
            database_path=str(self.repository.database.path),
            generated_at=datetime.now(UTC).isoformat(),
        )

    def render_markdown(self, report: Phase2AcceptanceReport) -> str:
        lines = [
            "# Phase 2 Acceptance Report",
            "",
            f"**Verdict:** `{report.verdict}`",
            "",
            f"- Generated at (UTC): `{report.generated_at}`",
            f"- Evidence database: `{report.database_path}`",
            (
                "- Supported evidence schemas: "
                f"`{sorted(SUPPORTED_EVIDENCE_SCHEMA_VERSIONS)}` "
                f"(current runtime: `v{SCHEMA_VERSION}`)"
            ),
            "",
            "This report is produced by mechanical SQLite inspection only. "
            "Missing evidence is treated as failure; nothing is inferred.",
            "",
            "## Checks",
            "",
        ]
        for check in report.checks:
            status = "PASS" if check.ok else "FAIL"
            lines.append(f"### `{check.name}` — **{status}**")
            lines.append("")
            lines.append(check.detail)
            lines.append("")
            if check.evidence:
                lines.append("```json")
                lines.append(json.dumps(check.evidence, indent=2, sort_keys=True, default=str))
                lines.append("```")
                lines.append("")
        failed = [check.name for check in report.checks if not check.ok]
        lines.append("## Summary")
        lines.append("")
        if report.accepted:
            lines.append(
                "All mechanical gates observed concrete evidence in the supplied database."
            )
        else:
            lines.append(
                "Phase 2 remains **PENDING/NOT ACCEPTED**. "
                "Failed gates: "
                + (", ".join(f"`{name}`" for name in failed) if failed else "unknown")
                + "."
            )
            lines.append("")
            lines.append(
                "Do not treat software completeness, unit tests, or synthetic fixtures "
                "as empirical Phase 2 acceptance."
            )
        lines.append("")
        return "\n".join(lines)

    def write_report(self, report: Phase2AcceptanceReport, output: str | Path) -> Path:
        path = Path(output).expanduser().resolve()
        database_path = self.repository.database.path
        same_existing_file = path.exists() and path.samefile(database_path)
        if path == database_path or same_existing_file:
            raise ValueError("report output must not overwrite the evidence database")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.render_markdown(report), encoding="utf-8")
        return path

    def _check_schema(self) -> AcceptanceCheck:
        try:
            health = self.repository.database.health()
            tables = set(self.repository.list_sqlite_tables())
            foreign_key_violations = self.repository.foreign_key_violations()
        except Exception as exc:  # noqa: BLE001
            return AcceptanceCheck(
                "schema_integrity",
                False,
                f"schema inspection failed: {type(exc).__name__}: {exc}",
            )
        schema_version = health.get("schema_version")
        version_supported = schema_version in SUPPORTED_EVIDENCE_SCHEMA_VERSIONS
        required_tables = _required_tables_for_schema(schema_version)
        missing = sorted(required_tables - tables)
        foreign_keys = bool(health.get("foreign_keys"))
        journal_mode = str(health.get("journal_mode") or "")
        ok = (
            version_supported
            and foreign_keys
            and journal_mode == "wal"
            and not foreign_key_violations
            and not missing
        )
        detail = (
            f"schema_version={schema_version}; "
            f"supported={sorted(SUPPORTED_EVIDENCE_SCHEMA_VERSIONS)}; "
            f"current_runtime={SCHEMA_VERSION}; "
            f"foreign_keys={foreign_keys}; journal_mode={journal_mode}; "
            f"foreign_key_violations={len(foreign_key_violations)}; "
            f"missing_tables={missing or 'none'}"
        )
        return AcceptanceCheck(
            "schema_integrity",
            ok,
            detail if ok else f"schema gate failed: {detail}",
            {
                "schema_version": schema_version,
                "supported_schema_versions": sorted(
                    SUPPORTED_EVIDENCE_SCHEMA_VERSIONS
                ),
                "current_runtime_schema_version": SCHEMA_VERSION,
                "foreign_keys": foreign_keys,
                "journal_mode": journal_mode,
                "foreign_key_violations": [
                    [value for value in row] for row in foreign_key_violations[:20]
                ],
                "missing_tables": missing,
                "table_count": len(tables),
            },
        )

    def _check_no_trading_mutation_surface(self) -> AcceptanceCheck:
        try:
            tables = self.repository.list_sqlite_tables()
        except Exception as exc:  # noqa: BLE001
            return AcceptanceCheck(
                "no_trading_mutation_surface",
                False,
                f"table inventory failed: {type(exc).__name__}: {exc}",
            )
        forbidden_hits = sorted(
            {
                name
                for name in tables
                if name in FORBIDDEN_TABLES
                or any(fragment in name.lower() for fragment in FORBIDDEN_TABLE_FRAGMENTS)
            }
        )
        # Expected research tables may contain substrings; allow the allow-list.
        forbidden_hits = [name for name in forbidden_hits if name not in REQUIRED_TABLES]
        ok = not forbidden_hits
        return AcceptanceCheck(
            "no_trading_mutation_surface",
            ok,
            (
                "no order/fill/position/signer/authenticated-mutation tables present"
                if ok
                else f"forbidden trading mutation tables present: {forbidden_hits}"
            ),
            {"forbidden_tables": forbidden_hits, "tables": tables},
        )

    def _check_replay(self) -> AcceptanceCheck:
        try:
            sealed = self.repository.sealed_replay_datasets()
        except Exception as exc:  # noqa: BLE001
            return AcceptanceCheck(
                "replay_dataset",
                False,
                f"replay dataset query failed: {type(exc).__name__}: {exc}",
            )
        if not sealed:
            return AcceptanceCheck(
                "replay_dataset",
                False,
                "no sealed replay dataset found",
                {"sealed_count": 0},
            )
        verifier = ReplayService(self.repository)
        verified: list[dict[str, Any]] = []
        failures: list[dict[str, Any]] = []
        for row in sealed:
            dataset_id = str(row["dataset_id"])
            item_count = int(row["item_count"] or 0)
            if item_count <= 0:
                failures.append(
                    {
                        "dataset_id": dataset_id,
                        "item_count": item_count,
                        "reason": "empty_dataset",
                    }
                )
                continue
            try:
                result = verifier.verify(dataset_id)
            except Exception as exc:  # noqa: BLE001
                failures.append(
                    {
                        "dataset_id": dataset_id,
                        "item_count": item_count,
                        "reason": f"{type(exc).__name__}: {exc}",
                    }
                )
                continue
            payload = {
                "dataset_id": dataset_id,
                "name": str(row["name"]),
                "item_count": result.item_count,
                "verified_count": result.verified_count,
                "ok": result.ok,
                "reasons": list(result.reasons),
            }
            if result.ok and result.item_count > 0:
                verified.append(payload)
            else:
                failures.append(payload)
        ok = bool(verified)
        return AcceptanceCheck(
            "replay_dataset",
            ok,
            (
                f"sealed non-empty verified replay datasets: {len(verified)}"
                if ok
                else "no sealed non-empty replay dataset verified at 100%"
            ),
            {"verified": verified, "failures": failures, "sealed_count": len(sealed)},
        )

    def _check_training_and_oos(self) -> AcceptanceCheck:
        try:
            runs = self.repository.complete_calibration_runs()
            qualifying, rejected = self._qualifying_calibration_runs(runs)
        except Exception as exc:  # noqa: BLE001
            return AcceptanceCheck(
                "chronological_train_and_oos",
                False,
                f"calibration query failed: {type(exc).__name__}: {exc}",
            )
        if not runs:
            return AcceptanceCheck(
                "chronological_train_and_oos",
                False,
                "no complete calibration run found",
                {"complete_runs": 0},
            )
        ok = bool(qualifying)
        return AcceptanceCheck(
            "chronological_train_and_oos",
            ok,
            (
                "complete calibration run has >=30 unique frozen training labels "
                "and at least one independent OOS evaluation label"
                if ok
                else (
                    "no complete calibration run evidences >=30 unique frozen "
                    "training labels plus a separate OOS evaluation window"
                )
            ),
            {
                "required_min_train": MIN_CHRONOLOGICAL_TRAIN_LABELS,
                "qualifying_runs": [
                    {
                        "run_id": item.run_id,
                        "dataset_id": item.dataset_id,
                        "min_train_size": item.min_train_size,
                        "sample_count": item.sample_count,
                        "evaluated_count": item.evaluated_count,
                        "computed_evaluated_count": item.computed_evaluated_count,
                    }
                    for item in qualifying
                ],
                "rejected_runs": rejected,
                "complete_runs": len(runs),
            },
        )

    def _check_metrics(self) -> AcceptanceCheck:
        try:
            runs = self.repository.complete_calibration_runs()
            qualifying, rejected_runs = self._qualifying_calibration_runs(runs)
        except Exception as exc:  # noqa: BLE001
            return AcceptanceCheck(
                "calibration_metrics",
                False,
                f"calibration metrics query failed: {type(exc).__name__}: {exc}",
            )
        complete_metrics: list[dict[str, Any]] = []
        incomplete: list[dict[str, Any]] = []
        for run in qualifying:
            metrics, issues = _parse_metrics(run.metrics_json)
            payload = {
                "run_id": run.run_id,
                "dataset_id": run.dataset_id,
                "evaluated_count": run.evaluated_count,
                "issues": issues,
                "metrics": metrics,
            }
            if not issues:
                complete_metrics.append(payload)
            else:
                incomplete.append(payload)
        ok = bool(complete_metrics)
        return AcceptanceCheck(
            "calibration_metrics",
            ok,
            (
                "raw/calibrated/market Brier, log loss, and ECE are complete and finite"
                if ok
                else "no complete calibration run has finite Brier/log_loss/ECE for all families"
            ),
            {
                "complete_metric_runs": complete_metrics,
                "incomplete": incomplete,
                "non_qualifying_runs": rejected_runs,
            },
        )

    def _qualifying_calibration_runs(
        self, runs: list[Any]
    ) -> tuple[list[_CalibrationEvidence], list[dict[str, Any]]]:
        qualifying: list[_CalibrationEvidence] = []
        rejected: list[dict[str, Any]] = []
        verifier = ReplayService(self.repository)
        for row in runs:
            run_id = str(row["run_id"])
            dataset_id = str(row["dataset_id"])
            reasons: list[str] = []
            dataset = self.repository.get_replay_dataset(dataset_id)
            replay_rows = self.repository.replay_item_rows(dataset_id)
            min_train = int(row["min_train_size"] or 0)
            stored_samples = int(row["sample_count"] or 0)
            stored_evaluated = int(row["evaluated_count"] or 0)
            if dataset is None or str(dataset["status"]) != "sealed":
                reasons.append("dataset_not_sealed")
            else:
                verification = verifier.verify(dataset_id)
                if not verification.ok:
                    reasons.append("dataset_verification_failed")
            if min_train < MIN_CHRONOLOGICAL_TRAIN_LABELS:
                reasons.append("min_train_below_30")
            if str(row["method"]) != CALIBRATION_METHOD:
                reasons.append("unexpected_calibration_method")
            if str(row["model_version"]) != CALIBRATION_MODEL_VERSION:
                reasons.append("unexpected_model_version")
            if str(row["source_version"]) != CALIBRATION_SOURCE_VERSION:
                reasons.append("unexpected_source_version")
            if int(row["bins"] or 0) < 2:
                reasons.append("invalid_bin_count")
            training_rows, training_issues = self._frozen_training_rows(dataset)
            reasons.extend(training_issues)
            chronology = _walk_forward_evidence(
                replay_rows,
                min_train=min_train,
                training_rows=training_rows,
            )
            reasons.extend(chronology["issues"])
            computed_samples = int(chronology["sample_count"])
            if stored_samples != computed_samples:
                reasons.append("sample_count_mismatch")
            computed_evaluated = int(chronology["evaluated_count"])
            if stored_evaluated != computed_evaluated:
                reasons.append("evaluated_count_mismatch")
            if computed_evaluated < 1:
                reasons.append("no_independent_oos_sample")
            if reasons:
                rejected.append(
                    {
                        "run_id": run_id,
                        "dataset_id": dataset_id,
                        "reasons": sorted(set(reasons)),
                        "stored_sample_count": stored_samples,
                        "replay_item_count": len(replay_rows),
                        "unique_label_count": computed_samples,
                        "stored_evaluated_count": stored_evaluated,
                        "computed_evaluated_count": computed_evaluated,
                    }
                )
                continue
            qualifying.append(
                _CalibrationEvidence(
                    run_id=run_id,
                    dataset_id=dataset_id,
                    min_train_size=min_train,
                    sample_count=stored_samples,
                    evaluated_count=stored_evaluated,
                    computed_evaluated_count=computed_evaluated,
                    metrics_json=str(row["metrics_json"] or ""),
                )
            )
        return qualifying, rejected

    def _frozen_training_rows(
        self,
        combined_dataset: Any,
    ) -> tuple[list[Any], list[str]]:
        if combined_dataset is None:
            return [], ["missing_combined_replay_dataset"]
        try:
            config = json.loads(str(combined_dataset["config_json"]))
        except (TypeError, ValueError, json.JSONDecodeError):
            return [], ["invalid_combined_replay_config"]
        reference = config.get("training_reference") if isinstance(config, dict) else None
        if not isinstance(reference, dict):
            return [], ["missing_frozen_training_reference"]
        training_id = str(reference.get("dataset_id") or "")
        if not training_id:
            return [], ["missing_frozen_training_dataset_id"]
        training_dataset = self.repository.get_replay_dataset(training_id)
        if training_dataset is None:
            return [], ["missing_frozen_training_dataset"]
        issues: list[str] = []
        if str(training_dataset["manifest_hash"]) != str(
            reference.get("manifest_hash") or ""
        ):
            issues.append("frozen_training_manifest_hash_mismatch")
        try:
            training_config = json.loads(str(training_dataset["config_json"]))
        except (TypeError, ValueError, json.JSONDecodeError):
            return [], [*issues, "invalid_frozen_training_config"]
        selection = (
            training_config.get("selection")
            if isinstance(training_config, dict)
            else None
        )
        if (
            not isinstance(selection, dict)
            or selection.get("mode") != "first_n_eligible_labels"
        ):
            issues.append("training_dataset_not_frozen")
        verification = ReplayService(self.repository).verify(training_id)
        if not verification.ok:
            issues.append("frozen_training_dataset_verification_failed")
        return self.repository.replay_item_rows(training_id), issues

    def _check_shadow_coverage(self) -> AcceptanceCheck:
        try:
            cycles = self.repository.all_shadow_cycles()
        except Exception as exc:  # noqa: BLE001
            return AcceptanceCheck(
                "shadow_72h_coverage",
                False,
                f"shadow cycle query failed: {type(exc).__name__}: {exc}",
            )
        if not cycles:
            return AcceptanceCheck(
                "shadow_72h_coverage",
                False,
                "no shadow_cycles rows found",
                {"cycle_count": 0},
            )
        starts: list[datetime] = []
        ends: list[datetime] = []
        intervals: list[tuple[datetime, datetime, str]] = []
        overlong_cycles: list[dict[str, Any]] = []
        for row in cycles:
            started = _parse_time(row["started_at"])
            completed = _parse_time(row["completed_at"])
            if started is None or completed is None:
                return AcceptanceCheck(
                    "shadow_72h_coverage",
                    False,
                    "one or more shadow cycles lack parseable started_at/completed_at timestamps",
                    {"cycle_count": len(cycles)},
                )
            if completed < started:
                return AcceptanceCheck(
                    "shadow_72h_coverage",
                    False,
                    "one or more shadow cycles complete before they start",
                    {"cycle_id": str(row["cycle_id"])},
                )
            duration_seconds = (completed - started).total_seconds()
            if duration_seconds > MAX_SHADOW_CYCLE_SECONDS:
                overlong_cycles.append(
                    {
                        "cycle_id": str(row["cycle_id"]),
                        "duration_seconds": round(duration_seconds, 3),
                    }
                )
            starts.append(started)
            ends.append(completed)
            intervals.append((started, completed, str(row["cycle_id"])))
        intervals.sort(key=lambda item: (item[0], item[1], item[2]))
        first = min(starts)
        last = max(ends)
        gaps: list[dict[str, Any]] = []
        segments: list[dict[str, Any]] = []
        segment_start = intervals[0][0]
        coverage_end = intervals[0][1]
        segment_cycle_count = 1
        for started, completed, cycle_id in intervals[1:]:
            gap_seconds = max(0.0, (started - coverage_end).total_seconds())
            if gap_seconds > MAX_SHADOW_GAP_SECONDS:
                gaps.append(
                    {
                        "next_cycle_id": cycle_id,
                        "gap_seconds": round(gap_seconds, 3),
                        "previous_coverage_end": coverage_end.isoformat(),
                        "next_started_at": started.isoformat(),
                    }
                )
                segments.append(
                    _shadow_segment(
                        segment_start,
                        coverage_end,
                        segment_cycle_count,
                    )
                )
                segment_start = started
                coverage_end = completed
                segment_cycle_count = 1
                continue
            coverage_end = max(coverage_end, completed)
            segment_cycle_count += 1
        segments.append(
            _shadow_segment(segment_start, coverage_end, segment_cycle_count)
        )
        qualifying_segments = [
            segment
            for segment in segments
            if segment["cycle_count"] >= 2
            and segment["coverage_hours"] >= MIN_SHADOW_HOURS
        ]
        longest_hours = max(
            (float(segment["coverage_hours"]) for segment in segments),
            default=0.0,
        )
        ok = bool(qualifying_segments) and not overlong_cycles
        return AcceptanceCheck(
            "shadow_72h_coverage",
            ok,
            (
                f"at least one continuous shadow segment spans "
                f"{longest_hours:.2f} hours across {len(cycles)} total cycles"
                if ok
                else (
                    f"longest continuous shadow segment is {longest_hours:.2f} hours "
                    f"over {len(cycles)} total cycles; "
                    f"need at least {MIN_SHADOW_HOURS} hours with no gap above "
                    f"{MAX_SHADOW_GAP_SECONDS} seconds and no cycle above "
                    f"{MAX_SHADOW_CYCLE_SECONDS} seconds; "
                    f"oversized_gaps={len(gaps)}; "
                    f"overlong_cycles={len(overlong_cycles)}"
                )
            ),
            {
                "cycle_count": len(cycles),
                "first_started_at": first.isoformat(),
                "last_completed_at": last.isoformat(),
                "coverage_hours": round(longest_hours, 4),
                "total_span_hours": round(
                    (last - first).total_seconds() / 3600.0,
                    4,
                ),
                "required_hours": MIN_SHADOW_HOURS,
                "max_allowed_gap_seconds": MAX_SHADOW_GAP_SECONDS,
                "max_allowed_cycle_seconds": MAX_SHADOW_CYCLE_SECONDS,
                "oversized_gaps": gaps[:20],
                "overlong_cycles": overlong_cycles[:20],
                "segments": segments[:20],
                "qualifying_segments": qualifying_segments[:20],
            },
        )

    def _check_cycle_fallback_rejection_paper(self) -> AcceptanceCheck:
        try:
            cycles = self.repository.all_shadow_cycles()
            rejected = self.repository.rejected_signal_count()
            paper = self.repository.table_count("paper_ledger")
        except Exception as exc:  # noqa: BLE001
            return AcceptanceCheck(
                "cycle_rest_rejection_paper_evidence",
                False,
                f"evidence query failed: {type(exc).__name__}: {exc}",
            )
        cycle_count = len(cycles)
        rest_fallback_cycles = [
            str(row["cycle_id"])
            for row in cycles
            if _has_rest_fallback(row)
        ]
        ok = (
            cycle_count >= 1
            and bool(rest_fallback_cycles)
            and rejected >= 1
            and paper >= 1
        )
        missing: list[str] = []
        if cycle_count < 1:
            missing.append("shadow_cycles")
        if not rest_fallback_cycles:
            missing.append("rest_fallback")
        if rejected < 1:
            missing.append("rejection_reasons")
        if paper < 1:
            missing.append("paper_ledger")
        return AcceptanceCheck(
            "cycle_rest_rejection_paper_evidence",
            ok,
            (
                "cycle, REST fallback, rejection-reason, and paper ledger evidence present"
                if ok
                else f"missing evidence: {', '.join(missing)}"
            ),
            {
                "cycle_count": cycle_count,
                "rest_fallback_cycle_ids": rest_fallback_cycles[:20],
                "rejected_signal_count": rejected,
                "paper_ledger_count": paper,
                "missing": missing,
            },
        )

    def _check_binance_stream(self) -> AcceptanceCheck:
        try:
            cycles = self.repository.all_shadow_cycles()
        except Exception as exc:  # noqa: BLE001
            return AcceptanceCheck(
                "binance_websocket_evidence",
                False,
                f"binance stream evidence query failed: {type(exc).__name__}: {exc}",
            )
        closed_ticks: list[dict[str, Any]] = []
        reconnects: list[dict[str, Any]] = []
        for row in cycles:
            health = _json_object(row["stream_health_json"])
            reference = health.get("binance_reference")
            if not isinstance(reference, dict):
                continue
            generation = _as_int(_nested(reference, "detail", "generation"))
            if generation is None:
                generation = _as_int(reference.get("generation"))
            evidence = reference.get("drained_tick_evidence")
            if isinstance(evidence, list):
                for tick in evidence:
                    issues = _binance_tick_issues(tick)
                    if not issues:
                        closed_ticks.append(
                            {
                                "cycle_id": str(row["cycle_id"]),
                                "pair": tick["pair"],
                                "provider_timestamp": tick["provider_timestamp"],
                                "received_at": tick["received_at"],
                                "sequence": tick.get("sequence"),
                                "payload_hash": tick["payload_hash"],
                            }
                        )
            if (
                generation is not None
                and generation >= 2
                and reference.get("status") == "connected"
            ):
                reconnects.append(
                    {
                        "cycle_id": str(row["cycle_id"]),
                        "generation": generation,
                        "status": reference.get("status"),
                    }
                )
        ok = bool(closed_ticks) and bool(reconnects)
        missing: list[str] = []
        if not closed_ticks:
            missing.append("closed_1m_tick")
        if not reconnects:
            missing.append("reconnect")
        return AcceptanceCheck(
            "binance_websocket_evidence",
            ok,
            (
                "shadow health records include fresh closed Binance 1m ticks "
                "and a connected reconnect generation>=2"
                if ok
                else f"missing Binance WebSocket evidence: {', '.join(missing)}"
            ),
            {
                "closed_1m_tick_cycles": closed_ticks[:20],
                "reconnect_cycles": reconnects[:20],
                "missing": missing,
            },
        )

    def _check_schema_drift_monitoring(self) -> AcceptanceCheck:
        try:
            cycles = self.repository.all_shadow_cycles()
        except Exception as exc:  # noqa: BLE001
            return AcceptanceCheck(
                "external_schema_drift_monitoring",
                False,
                f"schema-drift evidence query failed: {type(exc).__name__}: {exc}",
            )
        missing_cycles: list[str] = []
        failed_cycles: list[dict[str, Any]] = []
        monitored_payloads = 0
        ok_cycles = 0
        for row in cycles:
            health = _json_object(row["stream_health_json"])
            evidence = health.get("schema_drift")
            cycle_id = str(row["cycle_id"])
            if not isinstance(evidence, dict):
                missing_cycles.append(cycle_id)
                continue
            status = str(evidence.get("status") or "")
            source_version = str(evidence.get("source_version") or "")
            scanned = _as_int(evidence.get("scanned_payload_count")) or 0
            monitored_payloads += max(0, scanned)
            if (
                status not in {"ok", "no_payloads"}
                or source_version != SCHEMA_DRIFT_SOURCE_VERSION
            ):
                failed_cycles.append(
                    {
                        "cycle_id": cycle_id,
                        "status": status,
                        "source_version": source_version,
                        "issues": evidence.get("issues", []),
                    }
                )
            elif status == "ok" and scanned > 0:
                ok_cycles += 1
        ok = (
            bool(cycles)
            and not missing_cycles
            and not failed_cycles
            and monitored_payloads > 0
            and ok_cycles > 0
        )
        missing: list[str] = []
        if not cycles:
            missing.append("shadow_cycles")
        if missing_cycles:
            missing.append("cycles_without_monitor_evidence")
        if failed_cycles:
            missing.append("drift_or_monitor_failure")
        if monitored_payloads < 1:
            missing.append("checked_payloads")
        return AcceptanceCheck(
            "external_schema_drift_monitoring",
            ok,
            (
                f"schema monitor checked {monitored_payloads} payloads without drift"
                if ok
                else f"schema-drift evidence incomplete or failed: {', '.join(missing)}"
            ),
            {
                "cycle_count": len(cycles),
                "ok_cycles_with_payloads": ok_cycles,
                "monitored_payload_count": monitored_payloads,
                "cycles_without_evidence": missing_cycles[:20],
                "failed_cycles": failed_cycles[:20],
                "missing": missing,
                "source_version": SCHEMA_DRIFT_SOURCE_VERSION,
            },
        )


def _shadow_segment(
    started_at: datetime,
    completed_at: datetime,
    cycle_count: int,
) -> dict[str, Any]:
    return {
        "started_at": started_at.isoformat(),
        "completed_at": completed_at.isoformat(),
        "cycle_count": cycle_count,
        "coverage_hours": round(
            (completed_at - started_at).total_seconds() / 3600.0,
            4,
        ),
    }


def _required_tables_for_schema(schema_version: object) -> frozenset[str]:
    required = BASE_REQUIRED_TABLES
    if isinstance(schema_version, int):
        for introduced_in, tables in VERSION_REQUIRED_TABLES.items():
            if schema_version >= introduced_in:
                required = required.union(tables)
    return required


def _parse_metrics(raw: str) -> tuple[dict[str, Any], list[str]]:
    issues: list[str] = []
    try:
        payload = json.loads(raw) if raw else {}
    except json.JSONDecodeError as exc:
        return {}, [f"metrics_json_invalid:{exc}"]
    if not isinstance(payload, dict):
        return {}, ["metrics_json_not_object"]
    for family in METRIC_FAMILIES:
        family_payload = payload.get(family)
        if not isinstance(family_payload, dict):
            issues.append(f"missing_family:{family}")
            continue
        for name in METRIC_NAMES:
            value = family_payload.get(name)
            if value is None:
                issues.append(f"missing:{family}.{name}")
                continue
            try:
                number = float(value)
            except (TypeError, ValueError):
                issues.append(f"non_numeric:{family}.{name}")
                continue
            if not math.isfinite(number):
                issues.append(f"non_finite:{family}.{name}")
    return payload, issues


def _walk_forward_evidence(
    rows: list[Any],
    *,
    min_train: int,
    training_rows: list[Any] | None = None,
) -> dict[str, Any]:
    samples, issues = _replay_samples(rows)
    if training_rows is None:
        training_samples: list[tuple[str, int, datetime, datetime]] | None = None
    else:
        training_samples, training_issues = _replay_samples(training_rows)
        issues.extend(f"training_{issue}" for issue in training_issues)
    evaluated = 0
    if not issues:
        if training_samples is None:
            for index, (_label_id, _ordinal, decision_at, _available_at) in enumerate(
                samples
            ):
                available_training = sum(
                    1
                    for (
                        _prior_label,
                        _prior_ordinal,
                        _prior_decision,
                        prior_available,
                    ) in samples[:index]
                    if prior_available < decision_at
                )
                if available_training >= min_train:
                    evaluated += 1
        else:
            combined_label_ids = {item[0] for item in samples}
            training_label_ids = {item[0] for item in training_samples}
            if not training_label_ids.issubset(combined_label_ids):
                issues.append("frozen_training_labels_missing_from_combined")
            else:
                for label_id, _ordinal, decision_at, _available_at in samples:
                    if label_id in training_label_ids:
                        continue
                    available_training = sum(
                        1
                        for (
                            _training_label,
                            _training_ordinal,
                            _training_decision,
                            training_available,
                        ) in training_samples
                        if training_available < decision_at
                    )
                    if (
                        len(training_samples) >= min_train
                        and available_training == len(training_samples)
                    ):
                        evaluated += 1
    return {
        "sample_count": len(samples),
        "training_label_count": (
            len(training_samples) if training_samples is not None else None
        ),
        "evaluated_count": evaluated,
        "issues": sorted(set(issues)),
    }


def _replay_samples(
    rows: list[Any],
) -> tuple[list[tuple[str, int, datetime, datetime]], list[str]]:
    parsed: list[tuple[str, int, datetime, datetime]] = []
    issues: list[str] = []
    for expected_ordinal, row in enumerate(rows):
        ordinal = _as_int(row["ordinal"])
        if ordinal is None:
            issues.append("invalid_ordinal")
            continue
        if ordinal != expected_ordinal:
            issues.append("non_contiguous_ordinals")
        label_id = str(row["label_id"] or "")
        if not label_id:
            issues.append("missing_label_id")
        decision_at = _parse_time(row["decision_at"])
        label_available_at = _parse_time(row["label_available_at"])
        if decision_at is None or label_available_at is None:
            issues.append("unparseable_replay_timestamp")
            continue
        if label_available_at <= decision_at:
            issues.append("label_not_strictly_after_own_decision")
        parsed.append((label_id, ordinal, decision_at, label_available_at))
    if len(parsed) != len(rows):
        return [], sorted(set(issues))
    decisions = [item[2] for item in parsed]
    if decisions != sorted(decisions):
        issues.append("decisions_not_chronological")
    selected: dict[str, tuple[str, int, datetime, datetime]] = {}
    for item in parsed:
        label_id, ordinal, decision_at, _label_available_at = item
        current = selected.get(label_id)
        if current is None or (decision_at, ordinal) > (current[2], current[1]):
            selected[label_id] = item
    samples = sorted(
        selected.values(),
        key=lambda item: (item[2], item[1], item[0]),
    )
    return samples, sorted(set(issues))


def _binance_tick_issues(value: Any) -> list[str]:
    if not isinstance(value, dict):
        return ["tick_not_object"]
    issues: list[str] = []
    if str(value.get("provider") or "").lower() != "binance":
        issues.append("provider")
    if str(value.get("pair") or "").upper() not in {"BTCUSDT", "ETHUSDT"}:
        issues.append("pair")
    if str(value.get("candle_interval") or "").lower() != "1m":
        issues.append("candle_interval")
    if str(value.get("price_field") or "").lower() != "close":
        issues.append("price_field")
    if value.get("fresh") is not True:
        issues.append("fresh")
    provider_timestamp = _parse_time(value.get("provider_timestamp"))
    received_at = _parse_time(value.get("received_at"))
    if provider_timestamp is None or received_at is None:
        issues.append("timestamps")
    elif received_at < provider_timestamp:
        issues.append("received_before_provider")
    if str(value.get("source_version") or "") != BINANCE_STREAM_SOURCE_VERSION:
        issues.append("source_version")
    payload_hash = str(value.get("payload_hash") or "")
    invalid_hash_character = any(
        character not in "0123456789abcdef" for character in payload_hash
    )
    if len(payload_hash) != 64 or invalid_hash_character:
        issues.append("payload_hash")
    return issues


def _has_rest_fallback(row: Any) -> bool:
    status = str(row["status"] or "")
    if "rest_fallback" in status:
        return True
    reasons = _json_list(row["reasons"])
    if any("rest" in str(item).lower() and "fallback" in str(item).lower() for item in reasons):
        return True
    health = _json_object(row["stream_health_json"])
    polymarket = health.get("polymarket")
    if isinstance(polymarket, dict):
        detail = polymarket.get("detail")
        if isinstance(detail, dict) and detail.get("rest_fallback_active"):
            return True
        if polymarket.get("rest_fallback_active"):
            return True
        if str(polymarket.get("status") or "") in {"disabled", "degraded"}:
            # Disabled/degraded stream forces REST analysis authority in shadow.
            return True
    if status in {"complete_rest_fallback", "degraded"}:
        return True
    return False


def _json_object(raw: Any) -> dict[str, Any]:
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        payload = json.loads(str(raw))
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _json_list(raw: Any) -> list[Any]:
    if raw is None:
        return []
    if isinstance(raw, list):
        return raw
    try:
        payload = json.loads(str(raw))
    except json.JSONDecodeError:
        return []
    return payload if isinstance(payload, list) else []


def _nested(payload: dict[str, Any], *keys: str) -> Any:
    current: Any = payload
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _as_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _parse_time(value: Any) -> datetime | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)
