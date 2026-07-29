"""Continuous paper ledger driven only by persisted read-only signals."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from sqlite3 import Row
from uuid import uuid4

from crypto_threshold.domain.probability import AnalysisSignal
from crypto_threshold.domain.research import PaperLedgerEntry
from crypto_threshold.storage.repositories import Repository

PAPER_POLICY_VERSION = "net-ev-threshold-v1"


class PaperLedgerService:
    """Record hypothetical entries and settle them without an exchange client."""

    def __init__(
        self,
        repository: Repository,
        *,
        min_net_ev: Decimal,
        policy_version: str = PAPER_POLICY_VERSION,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.repository = repository
        self.min_net_ev = min_net_ev
        self.policy_version = policy_version
        self.clock = clock or (lambda: datetime.now(UTC))

    def record(self, signal: AnalysisSignal) -> tuple[PaperLedgerEntry, bool]:
        now = _utc(self.clock())
        reasons: list[str] = []
        outcome = signal.selected_outcome
        entry_vwap = signal.yes_ask_vwap if outcome == "YES" else signal.no_ask_vwap
        fee_per_share = (
            signal.yes_fee_per_share if outcome == "YES" else signal.no_fee_per_share
        )
        action = "enter"
        if signal.status != "analyzed":
            reasons.append("signal_not_analyzed")
        if outcome not in {"YES", "NO"}:
            reasons.append("no_selected_outcome")
        if signal.net_ev is None or signal.net_ev < self.min_net_ev:
            reasons.append("net_ev_below_paper_threshold")
        if entry_vwap is None or entry_vwap <= 0:
            reasons.append("missing_executable_vwap")
        if fee_per_share is None or fee_per_share < 0:
            reasons.append("missing_fee_per_share")
        if self.repository.has_open_paper_market(signal.market_id, self.policy_version):
            reasons.append("market_already_open")
        if reasons:
            action = "skip"

        shares = (
            signal.target_size_usdc / entry_vwap
            if action == "enter" and entry_vwap is not None
            else None
        )
        total_fee = (
            shares * fee_per_share
            if shares is not None and fee_per_share is not None
            else None
        )
        entry = PaperLedgerEntry(
            entry_id=f"paper:{uuid4()}",
            signal_id=signal.signal_id,
            market_id=signal.market_id,
            policy_version=self.policy_version,
            action=action,
            outcome=outcome if action == "enter" else None,
            status="open" if action == "enter" else "skipped",
            size_usdc=signal.target_size_usdc,
            entry_vwap=entry_vwap if action == "enter" else None,
            fee_per_share=fee_per_share if action == "enter" else None,
            shares=shares,
            total_fee=total_fee,
            net_ev=signal.net_ev,
            reasons=tuple(reasons),
            observed_at=signal.observed_at,
            received_at=now,
        )
        row, created = self.repository.save_paper_entry(entry)
        return _entry_from_row(row), created

    def settle_open(self, *, limit: int = 1000) -> int:
        settled = 0
        now = _utc(self.clock())
        for entry in self.repository.settleable_open_paper_rows(limit=limit):
            outcome_yes = bool(entry["settlement_outcome_yes"])
            selected = str(entry["outcome"])
            won = (selected == "YES" and outcome_yes) or (
                selected == "NO" and not outcome_yes
            )
            shares = Decimal(str(entry["shares"]))
            cost = Decimal(str(entry["size_usdc"]))
            fee = Decimal(str(entry["total_fee"]))
            payout = shares if won else Decimal("0")
            self.repository.settle_paper_entry(
                entry_id=str(entry["entry_id"]),
                label_id=str(entry["settlement_label_id"]),
                outcome_yes=outcome_yes,
                payout_usdc=payout,
                pnl_usdc=payout - cost - fee,
                settled_at=now,
            )
            settled += 1
        return settled


def _entry_from_row(record: Row) -> PaperLedgerEntry:
    return PaperLedgerEntry(
        entry_id=str(record["entry_id"]),
        signal_id=str(record["signal_id"]),
        market_id=str(record["market_id"]),
        policy_version=str(record["policy_version"]),
        action=str(record["action"]),
        outcome=str(record["outcome"]) if record["outcome"] else None,
        status=str(record["status"]),
        size_usdc=Decimal(str(record["size_usdc"])),
        entry_vwap=_optional_decimal(record["entry_vwap"]),
        fee_per_share=_optional_decimal(record["fee_per_share"]),
        shares=_optional_decimal(record["shares"]),
        total_fee=_optional_decimal(record["total_fee"]),
        net_ev=_optional_decimal(record["net_ev"]),
        reasons=tuple(json.loads(str(record["reasons"]))),
        observed_at=datetime.fromisoformat(str(record["observed_at"])),
        received_at=datetime.fromisoformat(str(record["received_at"])),
        source_version=str(record["source_version"]),
    )


def _optional_decimal(value: object) -> Decimal | None:
    return Decimal(str(value)) if value is not None else None


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
