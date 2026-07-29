"""Single persistence owner for markets, snapshots, rules, and signals."""

from __future__ import annotations

import hashlib
import json
from contextlib import closing
from datetime import UTC, datetime
from decimal import Decimal
from sqlite3 import Row
from typing import Any
from uuid import uuid4

from crypto_threshold.domain.fees import MarketFeeSchedule
from crypto_threshold.domain.markets import CryptoMarket, OrderBookSnapshot
from crypto_threshold.domain.prices import PriceCrossCheck, PriceSnapshot
from crypto_threshold.domain.probability import (
    SHORT_UPDOWN_WORKFLOW_SOURCE_VERSION,
    AnalysisSignal,
)
from crypto_threshold.domain.research import (
    PaperLedgerEntry,
    SettlementLabel,
    ShadowCycleResult,
)
from crypto_threshold.domain.rules import CryptoResolutionRule
from crypto_threshold.storage.db import Database


class Repository:
    """Repository is the only production SQL boundary."""

    def __init__(self, database: Database) -> None:
        self.database = database

    def upsert_market(self, market: CryptoMarket) -> None:
        """Persist the complete Gamma payload before rule or signal decisions."""
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO markets (
                    market_id, event_id, condition_id, question, slug, description,
                    active, closed, accepting_orders, enable_order_book,
                    end_date, gamma_end_date, outcomes, tokens, yes_token_id,
                    no_token_id, raw_payload, raw_observed_at, raw_received_at,
                    source_version, event_start_time, series_slug, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                          ?, ?)
                ON CONFLICT(market_id) DO UPDATE SET
                    event_id=excluded.event_id,
                    condition_id=excluded.condition_id,
                    question=excluded.question,
                    slug=excluded.slug,
                    description=excluded.description,
                    active=excluded.active,
                    closed=excluded.closed,
                    accepting_orders=excluded.accepting_orders,
                    enable_order_book=excluded.enable_order_book,
                    end_date=excluded.end_date,
                    gamma_end_date=excluded.gamma_end_date,
                    outcomes=excluded.outcomes,
                    tokens=excluded.tokens,
                    yes_token_id=excluded.yes_token_id,
                    no_token_id=excluded.no_token_id,
                    raw_payload=excluded.raw_payload,
                    raw_observed_at=excluded.raw_observed_at,
                    raw_received_at=excluded.raw_received_at,
                    source_version=excluded.source_version,
                    event_start_time=excluded.event_start_time,
                    series_slug=excluded.series_slug,
                    updated_at=excluded.updated_at
                """,
                (
                    market.market_id,
                    market.event_id,
                    market.condition_id,
                    market.question,
                    market.slug,
                    market.description,
                    _bool_int(market.active),
                    _bool_int(market.closed),
                    _bool_int(market.accepting_orders),
                    _bool_int(market.enable_order_book),
                    _iso(market.received_at),
                    _iso(market.gamma_end_date),
                    _json(list(market.outcomes)),
                    _json(
                        {"yes_token_id": market.yes_token_id, "no_token_id": market.no_token_id}
                    ),
                    market.yes_token_id,
                    market.no_token_id,
                    _json(market.raw_payload),
                    _iso(market.gamma_end_date),
                    _iso(market.received_at),
                    "gamma-markets-v1",
                    _iso(market.event_start_time),
                    market.series_slug,
                    _iso(market.received_at),
                ),
            )

    def record_external_payload(
        self,
        *,
        market_id: str | None,
        source: str,
        payload_kind: str,
        payload: Any,
        observed_at: datetime | None,
        received_at: datetime,
        source_version: str,
        analysis_run_id: str | None = None,
    ) -> int:
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """
                INSERT INTO external_payloads (
                    market_id, analysis_run_id, source, payload_kind, observed_at,
                    received_at, source_version, raw_payload
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    market_id,
                    analysis_run_id,
                    source,
                    payload_kind,
                    _iso(observed_at),
                    _iso(received_at),
                    source_version,
                    _json(payload),
                ),
            )
            if cursor.lastrowid is None:
                raise RuntimeError("external payload insert did not return an id")
            return int(cursor.lastrowid)

    def start_settlement_attempt(
        self,
        *,
        market_id: str,
        target_time_utc: datetime,
        contract_family: str,
        attempted_at: datetime,
    ) -> int:
        """Begin one durable settlement attempt and return its ordinal."""
        attempted = _iso(attempted_at)
        target = _iso(target_time_utc)
        if attempted is None or target is None:
            raise ValueError("settlement attempt timestamps are required")
        with self.database.transaction() as connection:
            row = connection.execute(
                """
                SELECT attempt_count
                FROM settlement_attempts
                WHERE market_id = ?
                """,
                (market_id,),
            ).fetchone()
            attempt_count = int(row["attempt_count"]) + 1 if row else 1
            if row:
                connection.execute(
                    """
                    UPDATE settlement_attempts
                    SET target_time_utc = ?,
                        contract_family = ?,
                        attempt_count = ?,
                        last_attempt_at = ?,
                        next_attempt_at = ?,
                        last_status = 'in_progress',
                        last_reason = NULL,
                        updated_at = ?
                    WHERE market_id = ?
                    """,
                    (
                        target,
                        contract_family,
                        attempt_count,
                        attempted,
                        attempted,
                        attempted,
                        market_id,
                    ),
                )
            else:
                connection.execute(
                    """
                    INSERT INTO settlement_attempts (
                        market_id, target_time_utc, contract_family,
                        attempt_count, last_attempt_at, next_attempt_at,
                        last_status, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 'in_progress', ?)
                    """,
                    (
                        market_id,
                        target,
                        contract_family,
                        attempt_count,
                        attempted,
                        attempted,
                        attempted,
                    ),
                )
        return attempt_count

    def record_settlement_payload_if_changed(
        self,
        *,
        market_id: str,
        source: str,
        payload_kind: str,
        payload: Any,
        observed_at: datetime | None,
        received_at: datetime,
        source_version: str,
        payload_fingerprint: str | None = None,
    ) -> tuple[int, bool]:
        """Persist a resolution payload only when its settlement meaning changes."""
        encoded = _json(payload)
        fingerprint_source = payload_fingerprint or encoded
        payload_hash = hashlib.sha256(fingerprint_source.encode("utf-8")).hexdigest()
        with self.database.transaction() as connection:
            attempt = connection.execute(
                """
                SELECT last_payload_id, last_payload_hash
                FROM settlement_attempts
                WHERE market_id = ?
                """,
                (market_id,),
            ).fetchone()
            if attempt is None:
                raise RuntimeError("settlement attempt must start before payload capture")

            previous_id = attempt["last_payload_id"]
            if (
                previous_id is not None
                and attempt["last_payload_hash"] == payload_hash
            ):
                return int(previous_id), False

            # Reconcile state created before schema v5 with its latest full
            # payload before deciding that a new raw body must be stored.
            if previous_id is None or attempt["last_payload_hash"] is None:
                previous = connection.execute(
                    """
                    SELECT id, raw_payload
                    FROM external_payloads
                    WHERE market_id = ?
                      AND source = ?
                      AND payload_kind = ?
                      AND source_version = ?
                    ORDER BY id DESC
                    LIMIT 1
                    """,
                    (market_id, source, payload_kind, source_version),
                ).fetchone()
                if previous is not None and previous["raw_payload"] == encoded:
                    connection.execute(
                        """
                        UPDATE settlement_attempts
                        SET last_payload_id = ?,
                            last_payload_hash = ?,
                            updated_at = ?
                        WHERE market_id = ?
                        """,
                        (
                            int(previous["id"]),
                            payload_hash,
                            _iso(received_at),
                            market_id,
                        ),
                    )
                    return int(previous["id"]), False

            cursor = connection.execute(
                """
                INSERT INTO external_payloads (
                    market_id, source, payload_kind, observed_at,
                    received_at, source_version, raw_payload
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    market_id,
                    source,
                    payload_kind,
                    _iso(observed_at),
                    _iso(received_at),
                    source_version,
                    encoded,
                ),
            )
            if cursor.lastrowid is None:
                raise RuntimeError("settlement payload insert did not return an id")
            payload_id = int(cursor.lastrowid)
            connection.execute(
                """
                UPDATE settlement_attempts
                SET last_payload_id = ?,
                    last_payload_hash = ?,
                    updated_at = ?
                WHERE market_id = ?
                """,
                (payload_id, payload_hash, _iso(received_at), market_id),
            )
            return payload_id, True

    def finish_settlement_attempt(
        self,
        *,
        market_id: str,
        status: str,
        next_attempt_at: datetime,
        reason: str | None,
        updated_at: datetime,
    ) -> None:
        """Persist the result and next eligible time for one attempt."""
        if status not in {"pending", "succeeded", "error"}:
            raise ValueError(f"unsupported settlement attempt status: {status}")
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE settlement_attempts
                SET next_attempt_at = ?,
                    last_status = ?,
                    last_reason = ?,
                    updated_at = ?
                WHERE market_id = ?
                """,
                (
                    _iso(next_attempt_at),
                    status,
                    reason,
                    _iso(updated_at),
                    market_id,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("settlement attempt state was not persisted")

    def save_resolution_rule(
        self,
        market_id: str,
        rule: CryptoResolutionRule,
        *,
        observed_at: datetime | None = None,
        received_at: datetime | None = None,
    ) -> None:
        observed_at = observed_at or datetime.now(UTC)
        received_at = received_at or datetime.now(UTC)
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO resolution_rules (
                    rule_id, market_id, event_id, condition_id, yes_token_id, no_token_id,
                    asset, quote, settlement_source, pair, operator, exact_operator,
                    threshold, strike, candle_interval, price_field, timezone,
                    observation_time, target_time_utc, gamma_end_date, rule_confidence,
                    tradable, preview_only, rejection_reason, raw_text, raw_description,
                    parser_version, observed_at, received_at, source_version, updated_at,
                    contract_family, boundary_type, window_start_time_utc,
                    affirmative_outcome, negative_outcome, series_slug
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                          ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(rule_id) DO UPDATE SET
                    event_id=excluded.event_id,
                    condition_id=excluded.condition_id,
                    yes_token_id=excluded.yes_token_id,
                    no_token_id=excluded.no_token_id,
                    asset=excluded.asset,
                    quote=excluded.quote,
                    settlement_source=excluded.settlement_source,
                    pair=excluded.pair,
                    operator=excluded.operator,
                    exact_operator=excluded.exact_operator,
                    threshold=excluded.threshold,
                    strike=excluded.strike,
                    candle_interval=excluded.candle_interval,
                    price_field=excluded.price_field,
                    timezone=excluded.timezone,
                    observation_time=excluded.observation_time,
                    target_time_utc=excluded.target_time_utc,
                    gamma_end_date=excluded.gamma_end_date,
                    rule_confidence=excluded.rule_confidence,
                    tradable=excluded.tradable,
                    preview_only=excluded.preview_only,
                    rejection_reason=excluded.rejection_reason,
                    raw_text=excluded.raw_text,
                    raw_description=excluded.raw_description,
                    parser_version=excluded.parser_version,
                    observed_at=excluded.observed_at,
                    received_at=excluded.received_at,
                    source_version=excluded.source_version,
                    contract_family=excluded.contract_family,
                    boundary_type=excluded.boundary_type,
                    window_start_time_utc=excluded.window_start_time_utc,
                    affirmative_outcome=excluded.affirmative_outcome,
                    negative_outcome=excluded.negative_outcome,
                    series_slug=excluded.series_slug,
                    updated_at=excluded.updated_at
                """,
                (
                    f"rule:{market_id}",
                    market_id,
                    rule.event_id,
                    rule.condition_id,
                    rule.yes_token_id,
                    rule.no_token_id,
                    rule.asset,
                    rule.quote,
                    rule.settlement_provider,
                    rule.pair,
                    rule.exact_operator,
                    rule.exact_operator,
                    str(rule.strike),
                    str(rule.strike),
                    rule.candle_interval,
                    rule.price_field,
                    rule.timezone,
                    rule.observation_time,
                    _iso(rule.target_time_utc),
                    _iso(rule.gamma_end_date),
                    rule.rule_confidence,
                    int(rule.tradable),
                    int(rule.preview_only),
                    rule.rejection_reason,
                    rule.raw_text,
                    rule.raw_description,
                    rule.parser_version,
                    _iso(observed_at),
                    _iso(received_at),
                    "contract-parser-v3",
                    _iso(received_at),
                    rule.contract_family,
                    rule.boundary_type,
                    _iso(rule.window_start_time_utc),
                    rule.affirmative_outcome,
                    rule.negative_outcome,
                    rule.series_slug,
                ),
            )

    def save_market_snapshot(self, snapshot: OrderBookSnapshot) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO market_snapshots (
                    snapshot_id, market_id, token_id, outcome, best_bid, best_ask,
                    midpoint, spread, bid_depth, ask_depth, observed_at, received_at,
                    source_version, timestamp_trusted, raw_payload
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    f"book:{uuid4()}",
                    snapshot.market_id,
                    snapshot.token_id,
                    snapshot.outcome,
                    _decimal(snapshot.best_bid),
                    _decimal(snapshot.best_ask),
                    _decimal(snapshot.midpoint),
                    _decimal(snapshot.spread),
                    str(snapshot.bid_depth),
                    str(snapshot.ask_depth),
                    _iso(snapshot.observed_at),
                    _iso(snapshot.received_at),
                    snapshot.source_version,
                    int(snapshot.timestamp_trusted),
                    _json(snapshot.raw_payload),
                ),
            )

    def save_fee_schedule(self, schedule: MarketFeeSchedule) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO market_fee_schedules (
                    schedule_id, market_id, condition_id, fee_rate, exponent,
                    taker_only, valid, rejection_reason, observed_at, received_at,
                    source_version, raw_payload
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    f"fee:{uuid4()}",
                    schedule.market_id,
                    schedule.condition_id,
                    _decimal(schedule.fee_rate),
                    _decimal(schedule.exponent),
                    _bool_int(schedule.taker_only),
                    int(schedule.valid),
                    schedule.rejection_reason,
                    _iso(schedule.observed_at),
                    _iso(schedule.received_at),
                    schedule.source_version,
                    _json(schedule.raw_payload),
                ),
            )

    def save_price_snapshot(self, snapshot: PriceSnapshot, *, market_id: str | None) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO price_snapshots (
                    snapshot_id, market_id, asset, quote, provider, symbol, price,
                    price_kind, observed_at, received_at, source_version, raw_payload
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    f"price:{uuid4()}",
                    market_id,
                    snapshot.asset,
                    snapshot.quote,
                    snapshot.provider,
                    snapshot.symbol,
                    str(snapshot.price),
                    snapshot.price_kind,
                    _iso(snapshot.observed_at),
                    _iso(snapshot.received_at),
                    snapshot.source_version,
                    _json(snapshot.raw_payload),
                ),
            )

    def save_price_cross_check(
        self, check: PriceCrossCheck, *, market_id: str | None
    ) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO price_cross_checks (
                    check_id, market_id, asset, primary_provider, secondary_provider,
                    primary_price, secondary_price, relative_diff, ok, reasons,
                    observed_at, received_at, source_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    f"check:{uuid4()}",
                    market_id,
                    check.asset,
                    check.primary_provider,
                    check.secondary_provider,
                    str(check.primary_price),
                    str(check.secondary_price),
                    str(check.relative_diff),
                    int(check.ok),
                    _json(check.reasons),
                    _iso(check.observed_at),
                    _iso(check.received_at),
                    check.source_version,
                ),
            )

    def save_analysis_signal(
        self,
        signal: AnalysisSignal,
        *,
        analysis_run_id: str | None = None,
        input_payloads: tuple[tuple[int, str], ...] = (),
    ) -> None:
        """Persist a decision and its exact raw-input links atomically."""
        with self.database.transaction() as connection:
            for payload_id, _role in input_payloads:
                payload_row = connection.execute(
                    """
                    SELECT market_id, analysis_run_id
                    FROM external_payloads WHERE id = ?
                    """,
                    (payload_id,),
                ).fetchone()
                if payload_row is None:
                    raise ValueError(f"missing signal input payload: {payload_id}")
                if (
                    payload_row["market_id"] != signal.market_id
                    or payload_row["analysis_run_id"] != analysis_run_id
                ):
                    raise ValueError("signal input payload belongs to another market or run")
            input_payload_max_id = max(
                (payload_id for payload_id, _role in input_payloads), default=None
            )
            connection.execute(
                """
                INSERT INTO analysis_signals (
                    signal_id, market_id, analysis_run_id, asset, threshold, deadline,
                    estimated_probability, probability_low, probability_high,
                    market_probability, edge, yes_midpoint, no_midpoint,
                    yes_ask_vwap, no_ask_vwap, target_size_usdc, fee_rate,
                    yes_fee_per_share, no_fee_per_share, yes_spread_cost,
                    no_spread_cost, yes_slippage_cost, no_slippage_cost,
                    yes_net_ev, no_net_ev, selected_outcome, net_ev, status,
                    model_name, model_version, confidence, reasons,
                    input_payload_max_id, observed_at, received_at, source_version,
                    contract_family, affirmative_outcome, negative_outcome
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                          ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    signal.signal_id,
                    signal.market_id,
                    analysis_run_id,
                    signal.asset,
                    _decimal(signal.threshold),
                    _iso(signal.deadline),
                    _decimal(signal.estimated_probability),
                    _decimal(signal.probability_low),
                    _decimal(signal.probability_high),
                    None,
                    None,
                    _decimal(signal.yes_midpoint),
                    _decimal(signal.no_midpoint),
                    _decimal(signal.yes_ask_vwap),
                    _decimal(signal.no_ask_vwap),
                    str(signal.target_size_usdc),
                    _decimal(signal.fee_rate),
                    _decimal(signal.yes_fee_per_share),
                    _decimal(signal.no_fee_per_share),
                    _decimal(signal.yes_spread_cost),
                    _decimal(signal.no_spread_cost),
                    _decimal(signal.yes_slippage_cost),
                    _decimal(signal.no_slippage_cost),
                    _decimal(signal.yes_net_ev),
                    _decimal(signal.no_net_ev),
                    signal.selected_outcome,
                    _decimal(signal.net_ev),
                    signal.status,
                    signal.model_name,
                    signal.model_version,
                    signal.confidence,
                    _json(list(signal.reasons)),
                    input_payload_max_id,
                    _iso(signal.observed_at),
                    _iso(signal.received_at),
                    signal.source_version,
                    signal.contract_family,
                    signal.affirmative_outcome,
                    signal.negative_outcome,
                ),
            )
            connection.executemany(
                """
                INSERT INTO analysis_signal_inputs (signal_id, payload_id, input_role)
                VALUES (?, ?, ?)
                """,
                (
                    (signal.signal_id, payload_id, role)
                    for payload_id, role in input_payloads
                ),
            )

    def save_settlement_label(self, label: SettlementLabel) -> SettlementLabel:
        """Store one immutable settlement observation per market target/version."""
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO settlement_labels (
                    label_id, market_id, target_time_utc, provider, pair,
                    candle_interval, price_field, exact_operator, strike,
                    observed_value, outcome_yes, payload_id, observed_at,
                    received_at, source_version, contract_family
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(market_id, target_time_utc, source_version) DO NOTHING
                """,
                (
                    label.label_id,
                    label.market_id,
                    _iso(label.target_time_utc),
                    label.provider,
                    label.pair,
                    label.candle_interval,
                    label.price_field,
                    label.exact_operator,
                    str(label.strike),
                    str(label.observed_value),
                    int(label.outcome_yes),
                    label.payload_id,
                    _iso(label.observed_at),
                    _iso(label.received_at),
                    label.source_version,
                    label.contract_family,
                ),
            )
            row = connection.execute(
                """
                SELECT * FROM settlement_labels
                WHERE market_id = ? AND target_time_utc = ? AND source_version = ?
                """,
                (label.market_id, _iso(label.target_time_utc), label.source_version),
            ).fetchone()
        if row is None:
            raise RuntimeError("settlement label was not persisted")
        return _settlement_label(row)

    def settlement_candidates(self, *, ready_before: datetime, limit: int) -> list[Row]:
        """Return supported rules whose final candle should now be closed.

        A rule can become preview-only after its deadline. Preserve settlement
        eligibility when an analyzed, pre-deadline decision proves that the
        contract was supported while it was live.
        """
        with closing(self.database.connect()) as connection:
            return list(
                connection.execute(
                    """
                    WITH eligible AS (
                        SELECT
                            m.market_id,
                            r.*,
                            CASE WHEN EXISTS (
                                SELECT 1
                                FROM analysis_signals AS s
                                WHERE s.market_id = m.market_id
                                  AND s.status = 'analyzed'
                                  AND s.observed_at < r.target_time_utc
                                  AND s.asset = r.asset
                                  AND s.contract_family = r.contract_family
                                  AND (
                                      r.contract_family != 'short_updown'
                                      OR s.source_version = ?
                                  )
                                  AND (
                                      (r.contract_family = 'daily_threshold'
                                       AND s.threshold = r.strike)
                                      OR
                                      (r.contract_family = 'short_updown'
                                       AND s.estimated_probability IS NOT NULL)
                                  )
                                  AND s.deadline = r.target_time_utc
                            ) THEN 1 ELSE 0 END AS had_analyzed_predeadline_signal,
                            a.next_attempt_at AS next_attempt_at,
                            CASE WHEN a.market_id IS NULL THEN 1 ELSE 0 END
                                AS candidate_group
                        FROM markets AS m
                        JOIN resolution_rules AS r ON r.market_id = m.market_id
                        LEFT JOIN settlement_labels AS l
                          ON l.market_id = m.market_id
                         AND l.target_time_utc = r.target_time_utc
                        LEFT JOIN settlement_attempts AS a
                          ON a.market_id = m.market_id
                        WHERE (
                            (
                                r.contract_family = 'daily_threshold'
                                AND r.tradable = 1
                            )
                            OR EXISTS (
                                SELECT 1
                                FROM analysis_signals AS s
                                WHERE s.market_id = m.market_id
                                  AND s.status = 'analyzed'
                                  AND s.observed_at < r.target_time_utc
                                  AND s.asset = r.asset
                                  AND s.contract_family = r.contract_family
                                  AND (
                                      r.contract_family != 'short_updown'
                                      OR s.source_version = ?
                                  )
                                  AND (
                                      (r.contract_family = 'daily_threshold'
                                       AND s.threshold = r.strike)
                                      OR
                                      (r.contract_family = 'short_updown'
                                       AND s.estimated_probability IS NOT NULL)
                                  )
                                  AND s.deadline = r.target_time_utc
                            )
                          )
                          AND r.target_time_utc IS NOT NULL
                          AND r.target_time_utc <= ?
                          AND l.label_id IS NULL
                          AND (
                              a.next_attempt_at IS NULL
                              OR a.next_attempt_at <= ?
                          )
                    ),
                    ranked AS (
                        SELECT
                            eligible.*,
                            ROW_NUMBER() OVER (
                                PARTITION BY candidate_group
                                ORDER BY
                                    COALESCE(next_attempt_at, target_time_utc),
                                    target_time_utc,
                                    market_id
                            ) AS candidate_rank
                        FROM eligible
                    )
                    SELECT *
                    FROM ranked
                    ORDER BY candidate_rank, candidate_group, target_time_utc, market_id
                    LIMIT ?
                    """,
                    (
                        SHORT_UPDOWN_WORKFLOW_SOURCE_VERSION,
                        SHORT_UPDOWN_WORKFLOW_SOURCE_VERSION,
                        _iso(ready_before),
                        _iso(ready_before),
                        limit,
                    ),
                )
            )

    def get_settlement_label(self, market_id: str) -> Row | None:
        with closing(self.database.connect()) as connection:
            return connection.execute(
                """
                SELECT * FROM settlement_labels
                WHERE market_id = ? ORDER BY received_at DESC LIMIT 1
                """,
                (market_id,),
            ).fetchone()

    def cex_direction_training_rows(self, *, assets: tuple[str, ...]) -> list[Row]:
        """Return authoritative short labels without requiring historical signals."""
        if not assets:
            return []
        placeholders = ",".join("?" for _ in assets)
        with closing(self.database.connect()) as connection:
            return list(
                connection.execute(
                    f"""
                    SELECT
                        l.label_id,
                        l.market_id,
                        l.target_time_utc,
                        l.outcome_yes,
                        l.received_at AS label_received_at,
                        l.source_version AS label_source_version,
                        r.asset,
                        r.candle_interval,
                        r.window_start_time_utc
                    FROM settlement_labels AS l
                    JOIN resolution_rules AS r ON r.market_id = l.market_id
                    WHERE l.contract_family = 'short_updown'
                      AND l.provider = 'chainlink'
                      AND r.contract_family = 'short_updown'
                      AND r.asset IN ({placeholders})
                      AND r.candle_interval IN ('5m', '15m')
                      AND r.window_start_time_utc IS NOT NULL
                      AND l.target_time_utc = r.target_time_utc
                    ORDER BY l.target_time_utc, r.asset, r.candle_interval, l.market_id
                    """,
                    assets,
                )
            )

    def replay_candidate_rows(self, *, contract_family: str) -> list[Row]:
        """Return labeled analyzed signals in decision-time order."""
        with closing(self.database.connect()) as connection:
            return list(
                connection.execute(
                    """
                    SELECT s.*, l.label_id, l.outcome_yes,
                           l.strike AS label_strike,
                           l.received_at AS label_received_at,
                           l.target_time_utc AS label_target_time_utc
                    FROM analysis_signals AS s
                    JOIN settlement_labels AS l
                      ON l.market_id = s.market_id
                     AND l.target_time_utc = s.deadline
                     AND l.contract_family = s.contract_family
                    WHERE s.status = 'analyzed'
                      AND s.contract_family = ?
                      AND (
                          s.contract_family != 'short_updown'
                          OR s.source_version = ?
                      )
                      AND s.estimated_probability IS NOT NULL
                      AND s.yes_ask_vwap IS NOT NULL
                      AND s.no_ask_vwap IS NOT NULL
                    ORDER BY s.observed_at, s.signal_id
                    """,
                    (contract_family, SHORT_UPDOWN_WORKFLOW_SOURCE_VERSION),
                )
            )

    def signal_input_rows(self, signal_id: str) -> list[Row]:
        with closing(self.database.connect()) as connection:
            return list(
                connection.execute(
                    """
                    SELECT p.*, i.input_role
                    FROM analysis_signal_inputs AS i
                    JOIN external_payloads AS p ON p.id = i.payload_id
                    WHERE i.signal_id = ?
                    ORDER BY p.id, i.input_role
                    """,
                    (signal_id,),
                )
            )

    def seal_replay_dataset(
        self,
        *,
        dataset_id: str,
        name: str,
        manifest_hash: str,
        config: dict[str, Any],
        source_version: str,
        created_at: datetime,
        items: tuple[dict[str, Any], ...],
    ) -> None:
        """Create an immutable replay manifest and all items in one transaction."""
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO replay_datasets (
                    dataset_id, name, status, manifest_hash, item_count, config_json,
                    source_version, created_at, sealed_at
                ) VALUES (?, ?, 'building', ?, ?, ?, ?, ?, NULL)
                """,
                (
                    dataset_id,
                    name,
                    manifest_hash,
                    len(items),
                    _json(config),
                    source_version,
                    _iso(created_at),
                ),
            )
            connection.executemany(
                """
                INSERT INTO replay_items (
                    dataset_id, ordinal, signal_id, label_id, decision_at,
                    label_available_at, feature_payload, feature_hash,
                    input_manifest_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    (
                        dataset_id,
                        item["ordinal"],
                        item["signal_id"],
                        item["label_id"],
                        item["decision_at"],
                        item["label_available_at"],
                        item["feature_payload"],
                        item["feature_hash"],
                        item["input_manifest_hash"],
                    )
                    for item in items
                ),
            )
            connection.execute(
                """
                UPDATE replay_datasets
                SET status = 'sealed', sealed_at = ?
                WHERE dataset_id = ? AND status = 'building'
                """,
                (_iso(created_at), dataset_id),
            )

    def get_replay_dataset(self, dataset: str) -> Row | None:
        with closing(self.database.connect()) as connection:
            return connection.execute(
                """
                SELECT * FROM replay_datasets WHERE dataset_id = ? OR name = ?
                """,
                (dataset, dataset),
            ).fetchone()

    def replay_item_rows(self, dataset_id: str) -> list[Row]:
        with closing(self.database.connect()) as connection:
            return list(
                connection.execute(
                    """
                    SELECT * FROM replay_items
                    WHERE dataset_id = ? ORDER BY ordinal
                    """,
                    (dataset_id,),
                )
            )

    def save_calibration_run(
        self,
        *,
        run_id: str,
        dataset_id: str,
        status: str,
        method: str,
        bins: int,
        min_train_size: int,
        sample_count: int,
        evaluated_count: int,
        metrics: dict[str, Any],
        rejection_reason: str | None,
        model_version: str,
        source_version: str,
        started_at: datetime,
        completed_at: datetime,
    ) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO calibration_runs (
                    run_id, dataset_id, status, method, bins, min_train_size,
                    sample_count, evaluated_count, metrics_json, rejection_reason,
                    model_version, source_version, started_at, completed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    dataset_id,
                    status,
                    method,
                    bins,
                    min_train_size,
                    sample_count,
                    evaluated_count,
                    _json(metrics),
                    rejection_reason,
                    model_version,
                    source_version,
                    _iso(started_at),
                    _iso(completed_at),
                ),
            )

    def save_paper_entry(self, entry: PaperLedgerEntry) -> tuple[Row, bool]:
        """Idempotently append a paper decision; never mutates exchange state."""
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """
                INSERT INTO paper_ledger (
                    entry_id, signal_id, market_id, policy_version, action, outcome,
                    status, size_usdc, entry_vwap, fee_per_share, shares, total_fee,
                    net_ev, reasons, observed_at, received_at, source_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(signal_id, policy_version) DO NOTHING
                """,
                (
                    entry.entry_id,
                    entry.signal_id,
                    entry.market_id,
                    entry.policy_version,
                    entry.action,
                    entry.outcome,
                    entry.status,
                    str(entry.size_usdc),
                    _decimal(entry.entry_vwap),
                    _decimal(entry.fee_per_share),
                    _decimal(entry.shares),
                    _decimal(entry.total_fee),
                    _decimal(entry.net_ev),
                    _json(entry.reasons),
                    _iso(entry.observed_at),
                    _iso(entry.received_at),
                    entry.source_version,
                ),
            )
            row = connection.execute(
                """
                SELECT * FROM paper_ledger
                WHERE signal_id = ? AND policy_version = ?
                """,
                (entry.signal_id, entry.policy_version),
            ).fetchone()
        if row is None:
            raise RuntimeError("paper ledger entry was not persisted")
        return row, cursor.rowcount == 1

    def has_open_paper_market(self, market_id: str, policy_version: str) -> bool:
        with closing(self.database.connect()) as connection:
            row = connection.execute(
                """
                SELECT 1 FROM paper_ledger
                WHERE market_id = ? AND policy_version = ? AND status = 'open'
                LIMIT 1
                """,
                (market_id, policy_version),
            ).fetchone()
            return row is not None

    def open_paper_rows(self, limit: int = 1000) -> list[Row]:
        with closing(self.database.connect()) as connection:
            return list(
                connection.execute(
                    """
                    SELECT * FROM paper_ledger WHERE status = 'open'
                    ORDER BY observed_at, entry_id LIMIT ?
                    """,
                    (limit,),
                )
            )

    def settleable_open_paper_rows(self, limit: int = 1000) -> list[Row]:
        """Return open entries that already have their exact decision label.

        Filtering before LIMIT prevents a large historical unlabeled backlog
        from starving newer entries whose authoritative labels are available.
        """
        with closing(self.database.connect()) as connection:
            return list(
                connection.execute(
                    """
                    SELECT
                        p.*,
                        l.label_id AS settlement_label_id,
                        l.outcome_yes AS settlement_outcome_yes
                    FROM paper_ledger AS p
                    JOIN analysis_signals AS s ON s.signal_id = p.signal_id
                    JOIN settlement_labels AS l ON l.label_id = (
                        SELECT candidate.label_id
                        FROM settlement_labels AS candidate
                        WHERE candidate.market_id = p.market_id
                          AND candidate.target_time_utc = s.deadline
                          AND candidate.contract_family = s.contract_family
                        ORDER BY candidate.received_at DESC, candidate.label_id DESC
                        LIMIT 1
                    )
                    WHERE p.status = 'open'
                    ORDER BY l.received_at, p.observed_at, p.entry_id
                    LIMIT ?
                    """,
                    (limit,),
                )
            )

    def settle_paper_entry(
        self,
        *,
        entry_id: str,
        label_id: str,
        outcome_yes: bool,
        payout_usdc: Decimal,
        pnl_usdc: Decimal,
        settled_at: datetime,
    ) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                """
                UPDATE paper_ledger
                SET status = 'settled', label_id = ?, outcome_yes = ?,
                    payout_usdc = ?, pnl_usdc = ?, settled_at = ?
                WHERE entry_id = ? AND status = 'open'
                """,
                (
                    label_id,
                    int(outcome_yes),
                    str(payout_usdc),
                    str(pnl_usdc),
                    _iso(settled_at),
                    entry_id,
                ),
            )

    def save_shadow_cycle(self, cycle: ShadowCycleResult) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO shadow_cycles (
                    cycle_id, mode, status, discovered_count, analyzed_count,
                    paper_entered_count, paper_skipped_count, stream_health_json,
                    reasons, source_version, started_at, completed_at
                    , contract_family
                ) VALUES (?, 'shadow', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    cycle.cycle_id,
                    cycle.status,
                    cycle.discovered_count,
                    cycle.analyzed_count,
                    cycle.paper_entered_count,
                    cycle.paper_skipped_count,
                    _json(cycle.stream_health),
                    _json(cycle.reasons),
                    cycle.source_version,
                    _iso(cycle.started_at),
                    _iso(cycle.completed_at),
                    cycle.contract_family,
                ),
            )

    def get_market(self, market_id: str) -> Row | None:
        with closing(self.database.connect()) as connection:
            return connection.execute(
                "SELECT * FROM markets WHERE market_id = ?", (market_id,)
            ).fetchone()

    def list_markets(self, limit: int = 100) -> list[Row]:
        with closing(self.database.connect()) as connection:
            return list(
                connection.execute(
                    "SELECT * FROM markets ORDER BY updated_at DESC, market_id LIMIT ?",
                    (limit,),
                )
            )

    def get_resolution_rule(self, market_id: str) -> Row | None:
        with closing(self.database.connect()) as connection:
            return connection.execute(
                "SELECT * FROM resolution_rules WHERE market_id = ?", (market_id,)
            ).fetchone()

    def latest_signal(self, market_id: str) -> Row | None:
        with closing(self.database.connect()) as connection:
            return connection.execute(
                """
                SELECT * FROM analysis_signals
                WHERE market_id = ? ORDER BY received_at DESC, rowid DESC LIMIT 1
                """,
                (market_id,),
            ).fetchone()

    def dashboard_counts(self) -> dict[str, int]:
        """Return bounded research-table counts for the read-only dashboard."""

        tables = (
            "markets",
            "analysis_signals",
            "settlement_labels",
            "replay_datasets",
            "calibration_runs",
            "paper_ledger",
            "shadow_cycles",
        )
        with closing(self.database.connect()) as connection:
            return {
                table: int(
                    connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                )
                for table in tables
            }

    def list_dashboard_markets(self, limit: int = 200) -> list[Row]:
        """Join each market to its rule, latest signal, and latest YES/NO books."""

        with closing(self.database.connect()) as connection:
            return list(
                connection.execute(
                    _DASHBOARD_MARKET_SELECT
                    + """
                    ORDER BY
                        CASE WHEN s.net_ev IS NULL THEN 1 ELSE 0 END,
                        CAST(s.net_ev AS REAL) DESC,
                        COALESCE(r.target_time_utc, m.gamma_end_date) ASC,
                        m.market_id
                    LIMIT ?
                    """,
                    (limit,),
                )
            )

    def get_dashboard_market(self, market_id: str) -> Row | None:
        with closing(self.database.connect()) as connection:
            return connection.execute(
                _DASHBOARD_MARKET_SELECT + " WHERE m.market_id = ? LIMIT 1",
                (market_id,),
            ).fetchone()

    def list_market_snapshot_history(self, market_id: str, limit: int = 20) -> list[Row]:
        with closing(self.database.connect()) as connection:
            return list(
                connection.execute(
                    """
                    SELECT * FROM market_snapshots
                    WHERE market_id = ?
                    ORDER BY received_at DESC, rowid DESC
                    LIMIT ?
                    """,
                    (market_id, limit),
                )
            )

    def list_market_signal_history(self, market_id: str, limit: int = 20) -> list[Row]:
        with closing(self.database.connect()) as connection:
            return list(
                connection.execute(
                    """
                    SELECT * FROM analysis_signals
                    WHERE market_id = ?
                    ORDER BY received_at DESC, rowid DESC
                    LIMIT ?
                    """,
                    (market_id, limit),
                )
            )

    def list_market_price_history(self, market_id: str, limit: int = 20) -> list[Row]:
        with closing(self.database.connect()) as connection:
            return list(
                connection.execute(
                    """
                    SELECT * FROM price_snapshots
                    WHERE market_id = ?
                    ORDER BY received_at DESC, rowid DESC
                    LIMIT ?
                    """,
                    (market_id, limit),
                )
            )

    def list_replay_datasets(self, limit: int = 100) -> list[Row]:
        with closing(self.database.connect()) as connection:
            return list(
                connection.execute(
                    """
                    SELECT * FROM replay_datasets
                    ORDER BY created_at DESC, dataset_id DESC
                    LIMIT ?
                    """,
                    (limit,),
                )
            )

    def list_calibration_runs(self, limit: int = 100) -> list[Row]:
        with closing(self.database.connect()) as connection:
            return list(
                connection.execute(
                    """
                    SELECT c.*, d.name AS dataset_name
                    FROM calibration_runs AS c
                    JOIN replay_datasets AS d ON d.dataset_id = c.dataset_id
                    ORDER BY c.completed_at DESC, c.run_id DESC
                    LIMIT ?
                    """,
                    (limit,),
                )
            )

    def list_paper_entries(self, limit: int = 200) -> list[Row]:
        with closing(self.database.connect()) as connection:
            return list(
                connection.execute(
                    """
                    SELECT p.*, m.question, r.asset, r.strike, r.target_time_utc
                    FROM paper_ledger AS p
                    JOIN markets AS m ON m.market_id = p.market_id
                    LEFT JOIN resolution_rules AS r ON r.market_id = p.market_id
                    ORDER BY p.observed_at DESC, p.entry_id DESC
                    LIMIT ?
                    """,
                    (limit,),
                )
            )

    def paper_summary(self) -> Row:
        with closing(self.database.connect()) as connection:
            row = connection.execute(
                """
                SELECT
                    COUNT(*) AS total,
                    SUM(CASE WHEN status = 'open' THEN 1 ELSE 0 END) AS open_count,
                    SUM(CASE WHEN status = 'settled' THEN 1 ELSE 0 END) AS settled_count,
                    SUM(CASE WHEN status = 'skipped' THEN 1 ELSE 0 END) AS skipped_count,
                    COALESCE(SUM(
                        CASE WHEN status = 'settled' THEN CAST(pnl_usdc AS REAL) ELSE 0 END
                    ), 0) AS settled_pnl_usdc
                FROM paper_ledger
                """
            ).fetchone()
        if row is None:
            raise RuntimeError("paper summary query returned no row")
        return row

    def list_shadow_cycles(self, limit: int = 100) -> list[Row]:
        with closing(self.database.connect()) as connection:
            return list(
                connection.execute(
                    """
                    SELECT * FROM shadow_cycles
                    ORDER BY started_at DESC, cycle_id DESC
                    LIMIT ?
                    """,
                    (limit,),
                )
            )

    def list_stream_market_rows(self, limit: int = 2000) -> list[Row]:
        """Return canonical market/rule rows used for event-ladder subscriptions."""
        with closing(self.database.connect()) as connection:
            return list(
                connection.execute(
                    """
                    SELECT
                        m.market_id, m.event_id, m.condition_id,
                        m.yes_token_id, m.no_token_id,
                        r.asset, r.settlement_source AS settlement_provider,
                        r.pair, r.target_time_utc, r.candle_interval, r.price_field
                    FROM markets AS m
                    JOIN resolution_rules AS r ON r.market_id = m.market_id
                    WHERE COALESCE(m.closed, 0) = 0
                      AND r.tradable = 1
                      AND m.yes_token_id IS NOT NULL
                      AND m.no_token_id IS NOT NULL
                    ORDER BY m.updated_at DESC, m.market_id
                    LIMIT ?
                    """,
                    (limit,),
                )
            )

    def list_ranked_stream_candidates(self, limit: int = 200) -> list[Row]:
        """Rank each market's latest analyzed signal by executable net EV."""
        with closing(self.database.connect()) as connection:
            return list(
                connection.execute(
                    """
                    WITH latest AS (
                        SELECT market_id, MAX(rowid) AS latest_rowid
                        FROM analysis_signals
                        GROUP BY market_id
                    )
                    SELECT s.market_id, s.net_ev, s.selected_outcome, s.received_at
                    FROM latest
                    JOIN analysis_signals AS s ON s.rowid = latest.latest_rowid
                    WHERE s.status = 'analyzed' AND s.net_ev IS NOT NULL
                    ORDER BY CAST(s.net_ev AS REAL) DESC, s.received_at DESC
                    LIMIT ?
                    """,
                    (limit,),
                )
            )

    def table_count(self, table: str) -> int:
        allowed = {
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
            "settlement_attempts",
            "replay_datasets",
            "replay_items",
            "calibration_runs",
            "paper_ledger",
            "shadow_cycles",
        }
        if table not in allowed:
            raise ValueError(f"unsupported table: {table}")
        with closing(self.database.connect()) as connection:
            row = connection.execute(f"SELECT COUNT(*) AS count FROM {table}").fetchone()
            return int(row["count"])

    def list_sqlite_tables(self) -> list[str]:
        """Return user tables present in the evidence database (read-only)."""
        with closing(self.database.connect()) as connection:
            rows = connection.execute(
                """
                SELECT name FROM sqlite_master
                WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
                ORDER BY name
                """
            ).fetchall()
        return [str(row["name"]) for row in rows]

    def foreign_key_violations(self) -> list[Row]:
        """Return concrete foreign-key integrity failures without changing the DB."""
        with closing(self.database.connect()) as connection:
            return list(connection.execute("PRAGMA foreign_key_check"))

    def sealed_replay_datasets(self) -> list[Row]:
        """Return sealed replay manifests for mechanical acceptance checks."""
        with closing(self.database.connect()) as connection:
            return list(
                connection.execute(
                    """
                    SELECT * FROM replay_datasets
                    WHERE status = 'sealed'
                    ORDER BY created_at ASC, dataset_id ASC
                    """
                )
            )

    def complete_calibration_runs(self) -> list[Row]:
        """Return complete calibration runs with metrics for acceptance."""
        with closing(self.database.connect()) as connection:
            return list(
                connection.execute(
                    """
                    SELECT * FROM calibration_runs
                    WHERE status = 'complete'
                    ORDER BY completed_at ASC, run_id ASC
                    """
                )
            )

    def all_shadow_cycles(self) -> list[Row]:
        """Return every shadow cycle ordered by wall-clock coverage."""
        with closing(self.database.connect()) as connection:
            return list(
                connection.execute(
                    """
                    SELECT * FROM shadow_cycles
                    ORDER BY started_at ASC, completed_at ASC, cycle_id ASC
                    """
                )
            )

    def rejected_signal_count(self) -> int:
        """Count analysis signals that store structured rejection reasons."""
        with closing(self.database.connect()) as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) AS count
                FROM analysis_signals
                WHERE status = 'rejected'
                  AND reasons IS NOT NULL
                  AND reasons != '[]'
                  AND TRIM(reasons) != ''
                """
            ).fetchone()
        return int(row["count"]) if row is not None else 0

    def max_external_payload_id(self) -> int:
        """Return the append-only raw-payload boundary for one shadow cycle."""
        with closing(self.database.connect()) as connection:
            row = connection.execute(
                "SELECT COALESCE(MAX(id), 0) AS max_id FROM external_payloads"
            ).fetchone()
        return int(row["max_id"]) if row is not None else 0

    def external_payload_rows_after(self, payload_id: int) -> list[Row]:
        """Return public raw payloads appended after a captured boundary."""
        with closing(self.database.connect()) as connection:
            return list(
                connection.execute(
                    """
                    SELECT id, source, payload_kind, source_version, raw_payload
                    FROM external_payloads
                    WHERE id > ?
                    ORDER BY id ASC
                    """,
                    (payload_id,),
                )
            )


_DASHBOARD_MARKET_SELECT = """
WITH
latest_signal AS (
    SELECT market_id, MAX(rowid) AS latest_rowid
    FROM analysis_signals
    GROUP BY market_id
),
latest_yes AS (
    SELECT market_id, MAX(rowid) AS latest_rowid
    FROM market_snapshots
    WHERE UPPER(outcome) = 'YES'
    GROUP BY market_id
),
latest_no AS (
    SELECT market_id, MAX(rowid) AS latest_rowid
    FROM market_snapshots
    WHERE UPPER(outcome) = 'NO'
    GROUP BY market_id
)
SELECT
    m.market_id,
    m.event_id,
    m.condition_id,
    m.question,
    m.slug,
    m.active,
    m.closed,
    m.accepting_orders,
    m.enable_order_book,
    m.gamma_end_date,
    m.yes_token_id,
    m.no_token_id,
    m.updated_at,
    r.asset,
    r.pair,
    r.settlement_source,
    r.exact_operator,
    r.strike,
    r.target_time_utc,
    r.candle_interval,
    r.price_field,
    r.tradable,
    r.preview_only,
    r.rejection_reason AS rule_rejection_reason,
    r.rule_confidence,
    s.signal_id,
    s.estimated_probability,
    s.probability_low,
    s.probability_high,
    s.yes_ask_vwap,
    s.no_ask_vwap,
    s.yes_net_ev,
    s.no_net_ev,
    s.selected_outcome,
    s.net_ev,
    s.status AS signal_status,
    s.reasons AS signal_reasons,
    s.observed_at AS signal_observed_at,
    ys.best_bid AS yes_best_bid,
    ys.best_ask AS yes_best_ask,
    ys.midpoint AS yes_midpoint,
    ys.spread AS yes_spread,
    ys.bid_depth AS yes_bid_depth,
    ys.ask_depth AS yes_ask_depth,
    ys.observed_at AS yes_book_observed_at,
    ns.best_bid AS no_best_bid,
    ns.best_ask AS no_best_ask,
    ns.midpoint AS no_midpoint,
    ns.spread AS no_spread,
    ns.bid_depth AS no_bid_depth,
    ns.ask_depth AS no_ask_depth,
    ns.observed_at AS no_book_observed_at
FROM markets AS m
LEFT JOIN resolution_rules AS r ON r.market_id = m.market_id
LEFT JOIN latest_signal AS ls ON ls.market_id = m.market_id
LEFT JOIN analysis_signals AS s ON s.rowid = ls.latest_rowid
LEFT JOIN latest_yes AS ly ON ly.market_id = m.market_id
LEFT JOIN market_snapshots AS ys ON ys.rowid = ly.latest_rowid
LEFT JOIN latest_no AS ln ON ln.market_id = m.market_id
LEFT JOIN market_snapshots AS ns ON ns.rowid = ln.latest_rowid
"""


def _json(value: Any) -> str:
    return json.dumps(value, default=str, sort_keys=True, separators=(",", ":"))


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _decimal(value: Decimal | None) -> str | None:
    return str(value) if value is not None else None


def _bool_int(value: bool | None) -> int | None:
    return int(value) if value is not None else None


def _settlement_label(row: Row) -> SettlementLabel:
    return SettlementLabel(
        label_id=str(row["label_id"]),
        market_id=str(row["market_id"]),
        target_time_utc=datetime.fromisoformat(str(row["target_time_utc"])),
        provider=str(row["provider"]),
        pair=str(row["pair"]),
        candle_interval=str(row["candle_interval"]),
        price_field=str(row["price_field"]),
        exact_operator=str(row["exact_operator"]),
        strike=Decimal(str(row["strike"])),
        observed_value=Decimal(str(row["observed_value"])),
        outcome_yes=bool(row["outcome_yes"]),
        payload_id=int(row["payload_id"]),
        observed_at=datetime.fromisoformat(str(row["observed_at"])),
        received_at=datetime.fromisoformat(str(row["received_at"])),
        source_version=str(row["source_version"]),
        contract_family=(
            str(row["contract_family"])
            if "contract_family" in row.keys()
            else "daily_threshold"
        ),
    )
