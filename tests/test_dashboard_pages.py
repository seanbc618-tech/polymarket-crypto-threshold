"""Read-only Crypto Dashboard page and query-composition tests."""

from __future__ import annotations

import http.client
import inspect
import threading
from pathlib import Path

from crypto_threshold.config import Settings
from crypto_threshold.dashboard.server import DashboardApp, create_dashboard_server
from crypto_threshold.services.readiness_service import ResearchReadinessService
from crypto_threshold.storage.db import Database

from .test_dashboard_security import FakeKeychain


def test_all_dashboard_routes_render_crypto_research_truth(tmp_path: Path) -> None:
    database = Database(tmp_path / "pages.db")
    database.initialize()
    _seed_dashboard(database)
    settings = Settings(
        _env_file=None,
        DATABASE_PATH=str(database.path),
        TRADING_DISABLED=True,
    )
    app = DashboardApp(settings, keychain=FakeKeychain(), env_file=tmp_path / ".env")

    expected = {
        "/?lang=en": ("Live status: NO-GO", "Bitcoin above"),
        "/markets?lang=en": ("BTC / ETH threshold markets", "100000"),
        "/markets/m1?lang=en": ("Latest executable analysis", "YES / NO order-book"),
        "/calibration?lang=en": ("Replay datasets", "Brier"),
        "/paper?lang=en": ("Paper ledger", "1.25"),
        "/shadow?lang=en": ("WebSocket is a bounded hint layer", "degraded"),
        "/readiness?lang=en": (
            "Dashboard safety checks",
            "not Phase 2 empirical acceptance",
        ),
        "/setup/wallet?lang=en": ("Wallet configuration", "TRADING_DISABLED"),
    }
    for path, needles in expected.items():
        response = app.render(path)
        assert response.status.value == 200
        for needle in needles:
            assert needle in response.body

    missing = app.render("/markets/missing?lang=en")
    unknown = app.render("/orders?lang=en")
    assert missing.status.value == 404
    assert unknown.status.value == 404


def test_dashboard_pages_have_no_exchange_mutation_forms(tmp_path: Path) -> None:
    database = Database(tmp_path / "forms.db")
    database.initialize()
    _seed_dashboard(database)
    app = DashboardApp(
        Settings(_env_file=None, DATABASE_PATH=str(database.path)),
        keychain=FakeKeychain(),
        env_file=tmp_path / ".env",
    )

    combined = "".join(
        app.render(path).body
        for path in ("/", "/markets", "/markets/m1", "/calibration", "/paper", "/shadow")
    )
    assert 'method="post"' not in combined.lower()
    for forbidden in (
        "place_order",
        "cancel_order",
        "buy_yes",
        "buy_no",
        "/live/",
        "/reconciliation/run",
    ):
        assert forbidden not in combined.lower()

    wallet = app.render("/setup/wallet")
    assert wallet.body.lower().count('method="post"') == 1
    assert wallet.body.count('name="csrf_token"') == 1


def test_phase2_readiness_does_not_construct_or_reference_secure_client() -> None:
    source = inspect.getsource(ResearchReadinessService)
    assert "SecureClient" not in source
    assert "get_balances" not in source
    assert "get_orders" not in source
    assert "get_positions" not in source


def test_http_server_returns_no_store_and_browser_security_headers(tmp_path: Path) -> None:
    database = Database(tmp_path / "http.db")
    database.initialize()
    app = DashboardApp(
        Settings(_env_file=None, DATABASE_PATH=str(database.path)),
        keychain=FakeKeychain(),
        env_file=tmp_path / ".env",
    )
    server = create_dashboard_server(app, host="127.0.0.1", port=0)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        client = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        client.request("GET", "/?lang=en")
        response = client.getresponse()
        body = response.read().decode()
        assert response.status == 200
        assert response.getheader("Cache-Control") == "no-store, max-age=0"
        assert response.getheader("X-Frame-Options") == "DENY"
        assert "frame-ancestors 'none'" in response.getheader("Content-Security-Policy")
        assert "Live status: NO-GO" in body
        client.close()

        client = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        client.request("GET", "/favicon.ico")
        response = client.getresponse()
        assert response.status == 204
        assert response.read() == b""
        client.close()
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)


def _seed_dashboard(database: Database) -> None:
    with database.transaction() as connection:
        connection.execute(
            """
            INSERT INTO markets (
                market_id, event_id, condition_id, question, active, closed,
                accepting_orders, enable_order_book, gamma_end_date,
                yes_token_id, no_token_id, raw_payload, raw_received_at
            ) VALUES (
                'm1', 'event-1', 'condition-1',
                'Bitcoin above 100000 at noon ET?', 1, 0, 1, 1,
                '2026-07-24T16:00:00+00:00', 'yes-1', 'no-1', '{}',
                '2026-07-23T00:00:00+00:00'
            )
            """
        )
        connection.execute(
            """
            INSERT INTO resolution_rules (
                rule_id, market_id, asset, quote, settlement_source, pair,
                operator, exact_operator, threshold, strike, candle_interval,
                price_field, timezone, observation_time, target_time_utc,
                rule_confidence, tradable, preview_only, received_at, source_version
            ) VALUES (
                'rule:m1', 'm1', 'BTC', 'USDT', 'Binance', 'BTCUSDT',
                'above', '>', '100000', '100000', '1m', 'Close',
                'America/New_York', '12:00', '2026-07-24T16:00:00+00:00',
                1.0, 1, 0, '2026-07-23T00:00:00+00:00', 'test-rule'
            )
            """
        )
        for outcome, token, bid, ask in (
            ("YES", "yes-1", "0.40", "0.42"),
            ("NO", "no-1", "0.58", "0.60"),
        ):
            connection.execute(
                """
                INSERT INTO market_snapshots (
                    snapshot_id, market_id, token_id, outcome, best_bid, best_ask,
                    midpoint, spread, bid_depth, ask_depth, observed_at, received_at,
                    source_version, timestamp_trusted, raw_payload
                ) VALUES (?, 'm1', ?, ?, ?, ?, '0.50', '0.02', '100', '90',
                    '2026-07-23T00:01:00+00:00', '2026-07-23T00:01:01+00:00',
                    'test-book', 1, '{}')
                """,
                (f"book-{outcome}", token, outcome, bid, ask),
            )
        connection.execute(
            """
            INSERT INTO analysis_signals (
                signal_id, market_id, asset, threshold, estimated_probability,
                probability_low, probability_high, yes_ask_vwap, no_ask_vwap,
                yes_net_ev, no_net_ev, selected_outcome, net_ev, status,
                model_version, reasons, observed_at, received_at, source_version
            ) VALUES (
                'signal-1', 'm1', 'BTC', '100000', '0.55', '0.50', '0.60',
                '0.43', '0.61', '0.10', '-0.18', 'YES', '0.10', 'analyzed',
                'test-model', '["accepted"]', '2026-07-23T00:02:00+00:00',
                '2026-07-23T00:02:01+00:00', 'test-signal'
            )
            """
        )
        connection.execute(
            """
            INSERT INTO price_snapshots (
                snapshot_id, market_id, asset, quote, provider, symbol, price,
                price_kind, observed_at, received_at, source_version, raw_payload
            ) VALUES (
                'price-1', 'm1', 'BTC', 'USDT', 'binance', 'BTCUSDT', '99800',
                '1m_close', '2026-07-23T00:01:00+00:00',
                '2026-07-23T00:01:01+00:00', 'test-price', '{}'
            )
            """
        )
        connection.execute(
            """
            INSERT INTO replay_datasets (
                dataset_id, name, status, manifest_hash, item_count, config_json,
                source_version, created_at, sealed_at
            ) VALUES (
                'dataset-1', 'first-real', 'sealed', 'abc123', 31, '{}',
                'test-replay', '2026-07-23T01:00:00+00:00',
                '2026-07-23T01:00:01+00:00'
            )
            """
        )
        connection.execute(
            """
            INSERT INTO calibration_runs (
                run_id, dataset_id, status, method, bins, min_train_size,
                sample_count, evaluated_count, metrics_json, rejection_reason,
                model_version, source_version, started_at, completed_at
            ) VALUES (
                'cal-1', 'dataset-1', 'complete', 'walk-forward-histogram', 10, 30,
                31, 1, '{"raw":{"Brier":0.22}}', NULL, 'test-model',
                'test-calibration', '2026-07-23T01:01:00+00:00',
                '2026-07-23T01:01:01+00:00'
            )
            """
        )
        connection.execute(
            """
            INSERT INTO paper_ledger (
                entry_id, signal_id, market_id, policy_version, action, outcome,
                status, size_usdc, entry_vwap, fee_per_share, shares, total_fee,
                net_ev, payout_usdc, pnl_usdc, reasons, observed_at, received_at,
                settled_at, source_version
            ) VALUES (
                'paper-1', 'signal-1', 'm1', 'paper-v1', 'enter', 'YES',
                'settled', '10', '0.43', '0.01', '20', '0.20', '0.10',
                '11.25', '1.25', '["accepted"]', '2026-07-23T00:02:00+00:00',
                '2026-07-23T00:02:01+00:00', '2026-07-23T02:00:00+00:00',
                'test-paper'
            )
            """
        )
        connection.execute(
            """
            INSERT INTO shadow_cycles (
                cycle_id, mode, status, discovered_count, analyzed_count,
                paper_entered_count, paper_skipped_count, stream_health_json,
                reasons, source_version, started_at, completed_at
            ) VALUES (
                'cycle-1', 'shadow', 'degraded', 8, 2, 1, 1,
                '{"mode":"rest-fallback"}', '["stream unavailable"]',
                'test-shadow', '2026-07-23T03:00:00+00:00',
                '2026-07-23T03:01:00+00:00'
            )
            """
        )
