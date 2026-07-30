"""CLI entrypoint for the read-only crypto-threshold research prototype."""

from __future__ import annotations

import json
import time
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlparse

import typer
from rich.console import Console
from rich.table import Table

from crypto_threshold.adapters.keychain import KeychainStore
from crypto_threshold.adapters.polymarket.client import GammaClobReadClient
from crypto_threshold.adapters.polymarket.stream import PolymarketStreamBridge
from crypto_threshold.adapters.polymarket.translator import translate_market
from crypto_threshold.adapters.prices.binance import BinanceProvider
from crypto_threshold.adapters.prices.binance_microstructure import (
    BinanceMicrostructureRestClient,
    BinanceMicrostructureStream,
)
from crypto_threshold.adapters.prices.chainlink_stream import (
    ChainlinkReferencePriceStream,
)
from crypto_threshold.adapters.prices.coinbase import CoinbaseProvider
from crypto_threshold.adapters.prices.polymarket_crypto import (
    parse_crypto_window_price,
)
from crypto_threshold.adapters.prices.stream import BinanceReferencePriceStream
from crypto_threshold.config import get_settings, load_settings
from crypto_threshold.domain.assets import (
    DAILY_THRESHOLD_ASSETS,
    SUPPORTED_CHAINLINK_PAIRS,
)
from crypto_threshold.domain.factor_research import (
    FactorComparator,
    FactorExperimentSpec,
    FactorObservation,
    FactorRule,
    FactorTradeSide,
)
from crypto_threshold.domain.rules import (
    DAILY_THRESHOLD_FAMILY,
    SHORT_UPDOWN_FAMILY,
    parse_contract,
)
from crypto_threshold.services.calibration_service import CalibrationService
from crypto_threshold.services.cex_direction_service import (
    CEX_DIRECTION_SUPPORTED_ASSETS,
    CexDirectionArtifact,
    CexDirectionTrainingService,
)
from crypto_threshold.services.discovery_service import DiscoveryService
from crypto_threshold.services.factor_screening_service import (
    FACTOR_SCREENING_SOURCE_VERSION,
    FactorScreeningService,
)
from crypto_threshold.services.hft_replay_service import HFT_REPLAY_SOURCE_VERSION
from crypto_threshold.services.market_workflow_service import MarketWorkflowService
from crypto_threshold.services.microstructure_shadow_service import (
    MicrostructureShadowConfig,
    MicrostructureShadowService,
)
from crypto_threshold.services.nautilus_execution_blueprint import (
    NautilusExecutionBlueprint,
)
from crypto_threshold.services.paper_ledger_service import PaperLedgerService
from crypto_threshold.services.phase2_acceptance_service import Phase2AcceptanceService
from crypto_threshold.services.pricing_service import cross_check_prices
from crypto_threshold.services.replay_service import ReplayService
from crypto_threshold.services.research_integrity_service import (
    RESEARCH_INTEGRITY_SOURCE_VERSION,
)
from crypto_threshold.services.settlement_service import SettlementService
from crypto_threshold.services.shadow_monitor_service import ShadowMonitorService
from crypto_threshold.services.short_challenger_service import ShortChallengerService
from crypto_threshold.services.stream_research_service import StreamResearchCoordinator
from crypto_threshold.storage.db import SCHEMA_VERSION, Database
from crypto_threshold.storage.microstructure_store import MicrostructureStore
from crypto_threshold.storage.repositories import Repository

app = typer.Typer(
    name="crypto-threshold",
    help="Auditable, read-only Polymarket crypto threshold research prototype.",
    no_args_is_help=True,
)
console = Console()


@app.command()
def init_db(
    db_path: str | None = typer.Option(None, help="Override DATABASE_PATH from settings"),
) -> None:
    """Initialize or migrate the SQLite research database."""
    settings = get_settings()
    path = db_path or settings.DATABASE_PATH
    database = Database(path)
    database.initialize()
    console.print(f"[green]Database schema v{SCHEMA_VERSION} initialized at:[/] {database.path}")


@app.command()
def dashboard(
    host: str = typer.Option("127.0.0.1", help="Bind address; localhost by default"),
    port: int = typer.Option(8765, min=1, max=65535),
    env_file: Path = typer.Option(Path(".env"), help="Non-secret local config file"),
) -> None:
    """Serve the read-only research dashboard and local Keychain wallet setup."""

    keychain = KeychainStore()
    settings = load_settings(
        env_file=env_file,
        reject_environment_secrets=True,
    )
    if not settings.TRADING_DISABLED:
        console.print("[red]TRADING_DISABLED=false blocks the Phase 2 dashboard.[/]")
        raise typer.Exit(code=2)
    if settings.POLYMARKET_STREAM_USER_CHANNEL_ENABLED:
        console.print("[red]User Channel must remain disabled in Phase 2.[/]")
        raise typer.Exit(code=2)
    from crypto_threshold.dashboard.server import serve_dashboard

    try:
        serve_dashboard(
            settings,
            host=host,
            port=port,
            env_file=env_file,
            keychain=keychain,
        )
    except ValueError as exc:
        console.print(f"[red]Dashboard refused to start:[/] {exc}")
        raise typer.Exit(code=2) from exc


@app.command()
def doctor(
    network: bool = typer.Option(
        True,
        "--network/--no-network",
        help="Perform live read-only Binance and Coinbase checks.",
    ),
) -> None:
    """Fail closed on DB, provider, URL, or trading-mode problems."""
    settings = get_settings()
    checks: list[tuple[str, bool, str]] = []

    db_path = Path(settings.DATABASE_PATH).expanduser()
    if not db_path.exists():
        checks.append(("database", False, f"missing: {db_path}; run init-db"))
    else:
        try:
            health = Database(db_path).health()
            db_ok = bool(
                health["ok"]
                and health["foreign_keys"]
                and health["journal_mode"] == "wal"
            )
            checks.append(("database", db_ok, str(health)))
        except Exception as exc:
            checks.append(("database", False, f"{type(exc).__name__}: {exc}"))

    checks.append(
        (
            "providers",
            settings.PRICE_PRIMARY_PROVIDER == "binance"
            and settings.PRICE_SECONDARY_PROVIDER == "coinbase",
            f"{settings.PRICE_PRIMARY_PROVIDER}/{settings.PRICE_SECONDARY_PROVIDER}",
        )
    )
    checks.append(
        (
            "stream_mode",
            (
                not settings.POLYMARKET_STREAM_ENABLED
                or (
                    settings.POLYMARKET_STREAM_SHADOW_MODE
                    and not settings.POLYMARKET_STREAM_USER_CHANNEL_ENABLED
                )
            ),
            (
                "disabled"
                if not settings.POLYMARKET_STREAM_ENABLED
                else "shadow/read-only"
                if settings.POLYMARKET_STREAM_SHADOW_MODE
                and not settings.POLYMARKET_STREAM_USER_CHANNEL_ENABLED
                else "unsafe stream configuration"
            ),
        )
    )
    checks.append(
        (
            "phase2_mode",
            settings.TRADING_DISABLED
            and settings.PAPER_MIN_NET_EV >= 0
            and (
                not settings.BINANCE_REFERENCE_STREAM_ENABLED
                or settings.SHADOW_ENABLED
            ),
            (
                "disabled"
                if not settings.SHADOW_ENABLED
                else "shadow/paper-only"
                if settings.TRADING_DISABLED
                else "unsafe"
            ),
        )
    )
    if settings.SHADOW_CONTRACT_FAMILY == DAILY_THRESHOLD_FAMILY:
        checks.append(
            (
                "shadow_reference_contract",
                True,
                "daily Binance REST/stream reference",
            )
        )
    else:
        try:
            artifact = CexDirectionArtifact.load(settings.SHORT_CEX_MODEL_PATH)
            checks.append(
                (
                    "short_cex_model",
                    True,
                    (
                        f"{artifact.runtime_model_version} "
                        f"checkpoint=T-{artifact.decision_lead_seconds}s"
                    ),
                )
            )
        except Exception as exc:
            checks.append(
                (
                    "short_cex_model",
                    False,
                    f"{type(exc).__name__}: {exc}",
                )
            )
    for name, value in (
        ("gamma_url", settings.POLYMARKET_GAMMA_API_BASE),
        ("clob_url", settings.POLYMARKET_CLOB_API_BASE),
        ("site_api_url", settings.POLYMARKET_SITE_API_BASE),
        ("binance_url", settings.BINANCE_API_BASE),
        ("coinbase_url", settings.COINBASE_API_BASE),
    ):
        checks.append((name, _valid_https_url(value), value))
    checks.append(
        (
            "binance_stream_url",
            _valid_wss_url(settings.BINANCE_STREAM_URL),
            settings.BINANCE_STREAM_URL,
        )
    )
    checks.append(
        (
            "trading_mode",
            settings.TRADING_DISABLED is True,
            "research/live-NO-GO" if settings.TRADING_DISABLED else "unsafe live flag enabled",
        )
    )

    if network:
        polymarket = GammaClobReadClient(settings)
        binance = BinanceProvider(settings.BINANCE_API_BASE)
        coinbase = CoinbaseProvider(settings.COINBASE_API_BASE)
        try:
            if settings.SHADOW_CONTRACT_FAMILY == SHORT_UPDOWN_FAMILY:
                now = datetime.now(UTC)
                discovered = polymarket.discover_updown_markets(
                    ("5m", "15m"),
                    start=now - timedelta(minutes=16),
                    end=now + timedelta(minutes=16),
                    limit=50,
                )
                checks.append(
                    (
                        "gamma_read_short_updown",
                        len(discovered) >= 14,
                        f"response_markets={len(discovered)}",
                    )
                )
                markets = [
                    translate_market(payload, received_at=now)
                    for payload in discovered
                ]
                active_market = next(
                    (
                        market
                        for market in markets
                        if market.event_start_time is not None
                        and market.gamma_end_date is not None
                        and market.event_start_time <= now < market.gamma_end_date
                    ),
                    None,
                )
                active_rule = (
                    parse_contract(active_market, now=now)
                    if active_market is not None
                    else None
                )
                if (
                    active_rule is None
                    or not active_rule.tradable
                    or not active_rule.pair
                    or not active_rule.candle_interval
                    or active_rule.window_start_time_utc is None
                    or active_rule.target_time_utc is None
                ):
                    checks.append(
                        (
                            "authoritative_window_price_read",
                            False,
                            "no active short Up/Down contract",
                        )
                    )
                else:
                    price_payload = polymarket.get_crypto_window_price(
                        active_rule.asset,
                        interval=active_rule.candle_interval,
                        start=active_rule.window_start_time_utc,
                        end=active_rule.target_time_utc,
                    )
                    price = parse_crypto_window_price(
                        price_payload,
                        asset=active_rule.asset,
                        pair=active_rule.pair,
                        interval=active_rule.candle_interval,
                        start=active_rule.window_start_time_utc,
                        end=active_rule.target_time_utc,
                        received_at=datetime.now(UTC),
                    )
                    checks.append(
                        (
                            "authoritative_window_price_read",
                            price.open_price > 0,
                            (
                                f"asset={price.asset} interval={price.interval} "
                                f"open_price={price.open_price}"
                            ),
                        )
                    )
            else:
                for asset in sorted(DAILY_THRESHOLD_ASSETS):
                    discovered = polymarket.discover_markets(asset, 1)
                    checks.append(
                        (
                            f"gamma_read_{asset}",
                            bool(discovered),
                            f"response_markets={len(discovered)}",
                        )
                    )
        except Exception as exc:
            checks.append(("gamma_reads", False, f"{type(exc).__name__}: {exc}"))
        try:
            server_time = polymarket.get_server_time()
            checks.append(("clob_read", True, f"server_time={server_time}"))
        except Exception as exc:
            checks.append(("clob_read", False, f"{type(exc).__name__}: {exc}"))
        if settings.SHADOW_CONTRACT_FAMILY == SHORT_UPDOWN_FAMILY:
            try:
                for asset in CEX_DIRECTION_SUPPORTED_ASSETS:
                    series = binance.get_klines(asset, interval="1m", limit=2)
                    snapshot = binance.latest_close_snapshot(series)
                    checks.append(
                        (
                            f"short_cex_kline_{asset}",
                            snapshot.price > 0,
                            f"{snapshot.symbol}={snapshot.price}",
                        )
                    )
            except Exception as exc:
                checks.append(
                    (
                        "short_cex_kline_reads",
                        False,
                        f"{type(exc).__name__}: {exc}",
                    )
                )
        if (
            settings.SHADOW_CONTRACT_FAMILY == SHORT_UPDOWN_FAMILY
            and settings.CHAINLINK_REFERENCE_STREAM_ENABLED
        ):
            stream = ChainlinkReferencePriceStream(
                stale_seconds=settings.CHAINLINK_REFERENCE_STREAM_STALE_SECONDS,
                history_seconds=settings.CHAINLINK_REFERENCE_STREAM_HISTORY_SECONDS,
                max_ticks_per_pair=(
                    settings.CHAINLINK_REFERENCE_STREAM_MAX_TICKS_PER_PAIR
                ),
            )
            try:
                stream.start()
                deadline = time.monotonic() + 12
                fresh_pairs: set[str] = set()
                while time.monotonic() < deadline:
                    detail = stream.health().get("detail")
                    if isinstance(detail, dict):
                        fresh_pairs = {
                            str(pair).upper()
                            for pair in detail.get("fresh_pairs", [])
                        }
                    if fresh_pairs == set(SUPPORTED_CHAINLINK_PAIRS):
                        break
                    time.sleep(0.1)
                checks.append(
                    (
                        "chainlink_stream_reads",
                        fresh_pairs == set(SUPPORTED_CHAINLINK_PAIRS),
                        f"fresh_pairs={sorted(fresh_pairs)}",
                    )
                )
            except Exception as exc:
                checks.append(
                    (
                        "chainlink_stream_reads",
                        False,
                        f"{type(exc).__name__}: {exc}",
                    )
                )
            finally:
                stream.stop()
        if settings.SHADOW_CONTRACT_FAMILY == DAILY_THRESHOLD_FAMILY:
            try:
                for asset in sorted(DAILY_THRESHOLD_ASSETS):
                    primary = binance.get_ticker_price(asset)
                    checks.append(
                        (f"binance_read_{asset}", primary.price > 0, str(primary.price))
                    )
            except Exception as exc:
                checks.append(("binance_reads", False, f"{type(exc).__name__}: {exc}"))
            try:
                for asset in sorted(DAILY_THRESHOLD_ASSETS):
                    secondary = coinbase.get_spot_price(asset)
                    checks.append(
                        (
                            f"coinbase_read_{asset}",
                            secondary.price > 0,
                            str(secondary.price),
                        )
                    )
            except Exception as exc:
                checks.append(("coinbase_reads", False, f"{type(exc).__name__}: {exc}"))
        polymarket.close()
        binance.close()
        coinbase.close()

    console.print("[bold]crypto-threshold doctor[/]")
    for name, ok, detail in checks:
        label = "[green]PASS[/]" if ok else "[red]FAIL[/]"
        console.print(f"  {label} {name}: {detail}")
    if not all(ok for _, ok, _ in checks):
        raise typer.Exit(code=1)


@app.command()
def prices(
    asset: str = typer.Option("BTC", help="Asset to fetch (BTC, ETH, SOL, or XRP)"),
) -> None:
    """Fetch Binance and Coinbase read-only sanity prices."""
    settings = get_settings()
    asset = asset.upper()
    binance = BinanceProvider(settings.BINANCE_API_BASE)
    coinbase = CoinbaseProvider(settings.COINBASE_API_BASE)
    try:
        primary = binance.get_ticker_price(asset)
        secondary = coinbase.get_spot_price(asset)
        check = cross_check_prices(
            primary,
            secondary,
            max_diff=settings.PRICE_CROSSCHECK_MAX_DIFF,
            max_age_seconds=settings.MAX_PRICE_AGE_SECONDS,
        )
    except (InvalidOperation, ValueError) as exc:
        console.print(f"[red]Price read failed:[/] {type(exc).__name__}: {exc}")
        raise typer.Exit(code=1) from exc
    finally:
        binance.close()
        coinbase.close()

    table = Table(title=f"{asset} read-only sanity prices")
    table.add_column("Provider")
    table.add_column("Pair")
    table.add_column("Price", justify="right")
    table.add_row(primary.provider, primary.symbol, f"{primary.price:,.2f}")
    table.add_row(secondary.provider, secondary.symbol, f"{secondary.price:,.2f}")
    console.print(table)
    console.print(f"Cross-check: {'PASS' if check.ok else 'FAIL'} ({check.relative_diff:.4%})")
    for reason in check.reasons:
        console.print(f"  {reason}")


@app.command()
def discover(
    asset: str | None = typer.Option(
        None, help="Optional BTC, ETH, SOL, or XRP filter"
    ),
    limit: int = typer.Option(100, min=1, max=1000),
) -> None:
    """Discover Gamma markets and persist raw payloads plus parsed rules."""
    settings = get_settings()
    database = Database(settings.DATABASE_PATH)
    database.initialize()
    repository = Repository(database)
    client = GammaClobReadClient(settings)
    try:
        results = DiscoveryService(client, repository).discover(asset=asset, limit=limit)
    except Exception as exc:
        console.print(f"[red]Discovery failed:[/] {type(exc).__name__}: {exc}")
        raise typer.Exit(code=1) from exc
    finally:
        client.close()
    tradable = sum(1 for result in results if result.rule.tradable)
    console.print(f"Persisted {len(results)} markets; {tradable} match the Phase 1 contract")


@app.command("markets")
def list_markets(limit: int = typer.Option(50, min=1, max=500)) -> None:
    """List stored markets from the canonical markets table."""
    settings = get_settings()
    database = Database(settings.DATABASE_PATH)
    database.initialize()
    rows = Repository(database).list_markets(limit=limit)
    table = Table(title="Stored Polymarket markets")
    table.add_column("Market ID")
    table.add_column("Question")
    table.add_column("Active")
    table.add_column("YES token")
    for row in rows:
        table.add_row(
            str(row["market_id"]),
            str(row["question"]),
            "yes" if row["active"] else "no",
            str(row["yes_token_id"] or "missing"),
        )
    console.print(table)


@app.command()
def analyze(
    market_id: str = typer.Option(..., "--market", help="Gamma market ID or condition ID"),
    size_usdc: str | None = typer.Option(None, help="Target executable ask size in USDC"),
) -> None:
    """Analyze one real market using Gamma, CLOB books, fees, and exchange prices."""
    settings = get_settings()
    try:
        target_size = Decimal(size_usdc) if size_usdc is not None else None
    except Exception as exc:
        console.print(f"[red]Invalid --size-usdc:[/] {size_usdc}")
        raise typer.Exit(code=2) from exc
    database = Database(settings.DATABASE_PATH)
    database.initialize()
    repository = Repository(database)
    client = GammaClobReadClient(settings)
    binance = BinanceProvider(settings.BINANCE_API_BASE)
    coinbase = CoinbaseProvider(settings.COINBASE_API_BASE)
    stream_coordinator: StreamResearchCoordinator | None = None
    if (
        settings.POLYMARKET_STREAM_ENABLED
        and settings.POLYMARKET_STREAM_SHADOW_MODE
        and not settings.POLYMARKET_STREAM_USER_CHANNEL_ENABLED
    ):
        bridge = PolymarketStreamBridge(
            enable_user_channel=False,
            stale_seconds=settings.POLYMARKET_STREAM_STALE_SECONDS,
            rest_verify_seconds=settings.POLYMARKET_STREAM_REST_VERIFY_SECONDS,
            max_quote_slots=settings.POLYMARKET_STREAM_MAX_QUOTE_SLOTS,
        )
        stream_coordinator = StreamResearchCoordinator(
            repository=repository,
            bridge=bridge,
            candidate_group_cap=settings.POLYMARKET_STREAM_CANDIDATE_GROUP_CAP,
        )
        stream_coordinator.start()
    service = MarketWorkflowService(
        client=client,
        repository=repository,
        binance=binance,
        coinbase=coinbase,
        settings=settings,
        stream_coordinator=stream_coordinator,
    )
    try:
        signal = service.analyze(market_id, target_size_usdc=target_size)
    except Exception as exc:
        console.print(f"[red]Analysis failed:[/] {type(exc).__name__}: {exc}")
        raise typer.Exit(code=1) from exc
    finally:
        client.close()
        binance.close()
        coinbase.close()
        if stream_coordinator is not None:
            stream_coordinator.stop()

    console.print(f"[bold]Status:[/] {signal.status}")
    console.print(f"Market: {signal.market_id}")
    if signal.estimated_probability is not None:
        console.print(
            "Model YES: "
            f"{signal.estimated_probability:.4%} "
            f"[{signal.probability_low:.4%}, {signal.probability_high:.4%}]"
        )
    console.print(f"YES ask VWAP: {signal.yes_ask_vwap}")
    console.print(f"NO ask VWAP:  {signal.no_ask_vwap}")
    console.print(f"YES net EV/share: {signal.yes_net_ev}")
    console.print(f"NO net EV/share:  {signal.no_net_ev}")
    console.print(f"Research selection: {signal.selected_outcome or 'none'}")
    for reason in signal.reasons:
        console.print(f"  {reason}")
    console.print("[yellow]Read-only analysis only. No BUY/SELL mutation was executed.[/]")
    if signal.status == "rejected":
        raise typer.Exit(code=2)


@app.command("settle")
def settle(
    market_id: str | None = typer.Option(None, "--market"),
    limit: int = typer.Option(100, min=1, max=1000),
) -> None:
    """Persist closed Binance 1m Close labels for supported markets."""
    settings = get_settings()
    database = Database(settings.DATABASE_PATH)
    database.initialize()
    repository = Repository(database)
    binance = BinanceProvider(settings.BINANCE_API_BASE)
    client = GammaClobReadClient(settings)
    service = SettlementService(
        repository=repository,
        binance=binance,
        client=client,
    )
    try:
        labels = (
            (service.settle_market(market_id),)
            if market_id is not None
            else service.settle_due(limit=limit)
        )
    except Exception as exc:
        console.print(f"[red]Settlement failed:[/] {type(exc).__name__}: {exc}")
        raise typer.Exit(code=2) from exc
    finally:
        client.close()
        binance.close()
    console.print(f"Persisted {len(labels)} immutable settlement labels")


@app.command("replay-build")
def replay_build(
    name: str = typer.Option(..., "--name"),
    family: str = typer.Option(
        DAILY_THRESHOLD_FAMILY,
        "--family",
        help="Contract family: daily_threshold or short_updown",
    ),
    training_label_count: int | None = typer.Option(
        None,
        "--training-label-count",
        min=1,
        help="Freeze the earliest N eligible unique labels as a training replay",
    ),
    training_dataset: str | None = typer.Option(
        None,
        "--training-dataset",
        help="Bind a combined replay to an existing frozen training replay",
    ),
) -> None:
    """Seal an offline replay manifest from exact analyzed inputs and labels."""
    settings = get_settings()
    database = Database(settings.DATABASE_PATH)
    database.initialize()
    try:
        if family not in {DAILY_THRESHOLD_FAMILY, SHORT_UPDOWN_FAMILY}:
            raise ValueError(f"unsupported contract family: {family}")
        result = ReplayService(Repository(database)).build(
            name,
            contract_family=family,
            training_label_count=training_label_count,
            training_dataset=training_dataset,
        )
    except Exception as exc:
        console.print(f"[red]Replay build failed:[/] {type(exc).__name__}: {exc}")
        raise typer.Exit(code=2) from exc
    console.print(
        f"Sealed {result.dataset_id}: items={result.item_count} "
        f"unique_labels={result.unique_label_count} hash={result.manifest_hash}"
    )
    if result.training_cutoff_at is not None:
        console.print(
            "  training cutoff: "
            f"{result.training_cutoff_at.isoformat()} / "
            f"{result.training_cutoff_label_id}"
        )
    for reason in result.rejection_reasons:
        console.print(f"  excluded: {reason}")
    if result.item_count == 0:
        console.print("[yellow]Replay dataset is empty and cannot pass acceptance.[/]")
        raise typer.Exit(code=2)


@app.command("replay-plan")
def replay_plan(
    db: Path | None = typer.Option(
        None,
        "--db",
        help="Existing evidence SQLite database; defaults to DATABASE_PATH",
    ),
    family: str = typer.Option(
        DAILY_THRESHOLD_FAMILY,
        "--family",
        help="Contract family: daily_threshold or short_updown",
    ),
    training_label_count: int = typer.Option(
        30,
        "--training-label-count",
        min=1,
        help="Required eligible unique labels in the frozen training replay",
    ),
) -> None:
    """Plan an exact training cutoff against an existing DB without writing."""
    database_path = db if db is not None else Path(get_settings().DATABASE_PATH)
    database = Database(database_path, read_only=True)
    try:
        if family not in {DAILY_THRESHOLD_FAMILY, SHORT_UPDOWN_FAMILY}:
            raise ValueError(f"unsupported contract family: {family}")
        result = ReplayService(Repository(database)).plan(
            training_label_count=training_label_count,
            contract_family=family,
        )
    except Exception as exc:
        console.print(f"[red]Replay plan failed:[/] {type(exc).__name__}: {exc}")
        raise typer.Exit(code=2) from exc
    verdict = "READY" if result.ready else "PENDING"
    console.print(
        f"Replay plan {verdict}: family={result.contract_family} "
        f"eligible_items={result.eligible_item_count} "
        f"eligible_unique_labels={result.eligible_unique_label_count}/"
        f"{result.requested_unique_label_count}"
    )
    if result.training_cutoff_at is not None:
        console.print(
            "  training cutoff: "
            f"{result.training_cutoff_at.isoformat()} / "
            f"{result.training_cutoff_label_id}"
        )
    if result.rejection_reasons:
        console.print(f"  rejected candidate signals: {len(result.rejection_reasons)}")
    if not result.ready:
        raise typer.Exit(code=1)


@app.command("replay-verify")
def replay_verify(
    dataset: str = typer.Option(..., "--dataset"),
) -> None:
    """Verify a replay manifest and its net-EV feature hashes offline."""
    settings = get_settings()
    database = Database(settings.DATABASE_PATH)
    database.initialize()
    try:
        result = ReplayService(Repository(database)).verify(dataset)
    except Exception as exc:
        console.print(f"[red]Replay verification failed:[/] {type(exc).__name__}: {exc}")
        raise typer.Exit(code=2) from exc
    console.print(
        f"Replay {result.dataset_id}: {result.verified_count}/{result.item_count} verified"
    )
    for reason in result.reasons:
        console.print(f"  {reason}")
    if not result.ok:
        raise typer.Exit(code=2)


@app.command("calibrate")
def calibrate(
    dataset: str = typer.Option(..., "--dataset"),
    bins: int | None = typer.Option(None, min=2, max=100),
    min_train_size: int | None = typer.Option(None, min=1),
) -> None:
    """Run chronological walk-forward calibration on a sealed replay set."""
    settings = get_settings()
    database = Database(settings.DATABASE_PATH)
    database.initialize()
    try:
        result = CalibrationService(Repository(database)).run(
            dataset,
            bins=bins or settings.CALIBRATION_BINS,
            min_train_size=min_train_size or settings.CALIBRATION_MIN_TRAIN_SIZE,
        )
    except Exception as exc:
        console.print(f"[red]Calibration failed:[/] {type(exc).__name__}: {exc}")
        raise typer.Exit(code=2) from exc
    console.print(
        f"Calibration {result.run_id}: {result.status}, "
        f"samples={result.sample_count}, evaluated={result.evaluated_count}"
    )
    if result.rejection_reason:
        console.print(f"  {result.rejection_reason}")
    if result.status != "complete":
        raise typer.Exit(code=2)


@app.command("paper-settle")
def paper_settle(
    limit: int = typer.Option(1000, min=1, max=5000),
) -> None:
    """Settle open paper entries from persisted authoritative labels only."""
    settings = get_settings()
    database = Database(settings.DATABASE_PATH)
    database.initialize()
    count = PaperLedgerService(
        Repository(database), min_net_ev=settings.PAPER_MIN_NET_EV
    ).settle_open(limit=limit)
    console.print(f"Settled {count} paper ledger entries; no exchange mutation executed")


@app.command("train-short-cex")
def train_short_cex(
    db: Path | None = typer.Option(
        None,
        "--db",
        help="Existing Up/Down evidence DB; defaults to DATABASE_PATH",
    ),
    output: Path | None = typer.Option(
        None,
        "--output",
        help="Atomic output path; defaults to SHORT_CEX_MODEL_PATH",
    ),
    decision_lead_seconds: int = typer.Option(
        60,
        "--decision-lead-seconds",
        min=30,
        max=300,
    ),
    min_samples: int = typer.Option(500, "--min-samples", min=100),
) -> None:
    """Train a sealed CEX-kline model on Chainlink labels with a time holdout."""
    settings = get_settings()
    database_path = db if db is not None else Path(settings.DATABASE_PATH)
    output_path = (
        output if output is not None else Path(settings.SHORT_CEX_MODEL_PATH)
    )
    database = Database(database_path, read_only=True)
    binance = BinanceProvider(settings.BINANCE_API_BASE)
    try:
        result = CexDirectionTrainingService(
            Repository(database),
            binance,
        ).train(
            output_path,
            decision_lead_seconds=decision_lead_seconds,
            min_samples=min_samples,
        )
    except Exception as exc:
        console.print(
            f"[red]Short CEX model training failed:[/] {type(exc).__name__}: {exc}"
        )
        raise typer.Exit(code=2) from exc
    finally:
        binance.close()
    console.print(
        f"[green]SEALED[/] {result.artifact.runtime_model_version} "
        f"samples={result.sample_count} train={result.training_count} "
        f"holdout={result.holdout_count}"
    )
    console.print(
        "  holdout: "
        f"Brier={result.holdout_brier:.6f} "
        f"log_loss={result.holdout_log_loss:.6f} "
        f"accuracy={result.holdout_accuracy:.4%}"
    )
    console.print(
        "  constant baseline: "
        f"Brier={result.baseline_brier:.6f} "
        f"log_loss={result.baseline_log_loss:.6f} "
        f"accuracy={result.baseline_accuracy:.4%}"
    )
    console.print(
        f"  artifact={result.output_path} hash={result.artifact.artifact_hash}"
    )


@app.command("phase2-acceptance")
def phase2_acceptance(
    db: Path = typer.Option(..., "--db", help="Existing evidence SQLite database"),
    output: Path = typer.Option(..., "--output", help="Markdown report output path"),
) -> None:
    """Mechanically accept or reject Phase 2 from concrete DB evidence only."""
    try:
        service = Phase2AcceptanceService.from_db_path(db)
        report = service.evaluate()
        path = service.write_report(report, output)
    except FileNotFoundError as exc:
        console.print(f"[red]Phase 2 acceptance refused:[/] {exc}")
        raise typer.Exit(code=2) from exc
    except Exception as exc:
        console.print(f"[red]Phase 2 acceptance failed:[/] {type(exc).__name__}: {exc}")
        raise typer.Exit(code=2) from exc

    label = "[green]ACCEPTED[/]" if report.accepted else "[yellow]PENDING/NOT ACCEPTED[/]"
    console.print(f"{label} report written to {path}")
    for check in report.checks:
        mark = "[green]PASS[/]" if check.ok else "[red]FAIL[/]"
        console.print(f"  {mark} {check.name}: {check.detail}")
    if not report.accepted:
        raise typer.Exit(code=1)


@app.command("execution-blueprint")
def execution_blueprint() -> None:
    """Print the pinned, non-executable Nautilus/Polymarket capability map."""
    manifest = NautilusExecutionBlueprint().manifest()
    console.print(
        "[yellow]REFERENCE BLUEPRINT ONLY[/] "
        f"NautilusTrader {manifest['nautilus_reference_tag']} "
        f"commit={manifest['nautilus_reference_commit']}"
    )
    console.print(
        "  live_submission=false credentials=false "
        "unknown_submit=remain_submitted_and_reconcile"
    )
    mapping = Table("Nautilus TIF", "Polymarket orderType")
    tif_mapping = manifest["tif_mapping"]
    if not isinstance(tif_mapping, dict):
        raise RuntimeError("execution blueprint TIF mapping is malformed")
    for tif, venue_type in tif_mapping.items():
        mapping.add_row(str(tif), str(venue_type))
    console.print(mapping)
    console.print(
        "  quantity: MARKET BUY=quote notional; "
        "MARKET SELL/LIMIT=token quantity"
    )
    console.print(
        "  mutation lock: submit/cancel/authenticated reconciliation not implemented"
    )


@app.command("research-tooling-status")
def research_tooling_status() -> None:
    """Show R1/R2/R3 implementation and the evidence gates still pending."""
    console.print("R1 implementation: PUBLIC L2/TICK CAPTURE READY")
    console.print("R2 implementation: INTEGRITY GATES WIRED")
    console.print("R3 implementation: PREREGISTERED FACTOR SCREEN READY")
    console.print("R3 evidence: SEALED PLAN READY; SETTLED OOS EVIDENCE PENDING")
    table = Table("Work package", "Core", "Evidence status", "Source version")
    table.add_row(
        "R1 HFT replay",
        "L2 + feed/order latency + queue/fill grid + attribution",
        "SHADOW CAPTURE READY; SETTLED EXECUTABLE EVIDENCE PENDING",
        HFT_REPLAY_SOURCE_VERSION,
    )
    table.add_row(
        "R2 integrity",
        "lookahead + recursive + timestamps + purge/embargo",
        "RUNS ON CAPTURED SAMPLES; PASS/FAIL EVIDENCE PENDING",
        RESEARCH_INTEGRITY_SOURCE_VERSION,
    )
    table.add_row(
        "R3 factor screen",
        "pre-registered factors + fee-adjusted OOS vs market/frozen v4",
        "SEALED PLAN READY; SETTLED OOS EVIDENCE PENDING",
        FACTOR_SCREENING_SOURCE_VERSION,
    )
    console.print(table)
    console.print(
        "[yellow]LIVE NO-GO[/] submission=false signing=false "
        "authenticated_reconciliation=false"
    )


@app.command("microstructure-shadow")
def microstructure_shadow(
    once: bool = typer.Option(False, "--once", help="Run exactly one capture cycle"),
    duration_hours: float | None = typer.Option(
        None,
        "--duration-hours",
        min=0.001,
        help="Stop cleanly after this many wall-clock hours",
    ),
) -> None:
    """Capture public Binance L2/trades and run isolated R1/R2/R3 research."""
    settings = get_settings()
    if once and duration_hours is not None:
        console.print("[red]Use either --once or --duration-hours, not both.[/]")
        raise typer.Exit(code=2)
    if not settings.MICROSTRUCTURE_ENABLED:
        console.print("[yellow]Microstructure shadow is disabled by default.[/]")
        raise typer.Exit(code=2)
    if (
        not settings.TRADING_DISABLED
        or settings.POLYMARKET_PRIVATE_KEY is not None
        or settings.POLYMARKET_FUNDER is not None
    ):
        console.print("[red]Unsafe microstructure configuration; refusing to start.[/]")
        raise typer.Exit(code=2)

    store = MicrostructureStore(settings.MICROSTRUCTURE_DATABASE_PATH)
    store.initialize()
    stream = BinanceMicrostructureStream(
        symbols=settings.microstructure_symbols,
        stale_seconds=settings.MICROSTRUCTURE_STREAM_STALE_SECONDS,
        max_events=settings.MICROSTRUCTURE_STREAM_MAX_EVENTS,
        stream_url=settings.BINANCE_STREAM_URL,
        proxy_url=settings.BINANCE_STREAM_PROXY_URL,
    )
    rest = BinanceMicrostructureRestClient(
        spot_base_url=settings.BINANCE_API_BASE,
        futures_base_url=settings.BINANCE_FUTURES_API_BASE,
    )
    config = MicrostructureShadowConfig(
        symbols=settings.microstructure_symbols,
        poll_seconds=settings.MICROSTRUCTURE_POLL_SECONDS,
        snapshot_seconds=settings.MICROSTRUCTURE_SNAPSHOT_SECONDS,
        feature_seconds=settings.MICROSTRUCTURE_FEATURE_SECONDS,
        integrity_seconds=settings.MICROSTRUCTURE_INTEGRITY_SECONDS,
        depth_levels=settings.MICROSTRUCTURE_DEPTH_LEVELS,
        trade_lookback_seconds=settings.MICROSTRUCTURE_TRADE_LOOKBACK_SECONDS,
        event_batch_limit=settings.MICROSTRUCTURE_EVENT_BATCH_LIMIT,
        integrity_sample_limit=settings.MICROSTRUCTURE_INTEGRITY_SAMPLE_LIMIT,
    )
    service = MicrostructureShadowService(
        store=store,
        stream=stream,
        rest=rest,
        config=config,
    )
    try:
        session_id = service.run(
            duration_hours=(
                None
                if once
                else duration_hours or settings.MICROSTRUCTURE_DURATION_HOURS
            ),
            once=once,
        )
    except typer.Exit:
        raise
    except Exception as exc:
        console.print(
            f"[red]Microstructure shadow failed:[/] {type(exc).__name__}: {exc}"
        )
        raise typer.Exit(code=1) from exc
    summary = store.summary()
    console.print(
        f"[green]microstructure shadow complete[/] session={session_id} "
        f"events={summary['events']} features={summary['feature_samples']} "
        f"integrity_runs={summary['integrity_runs']} "
        f"factor_runs={summary['factor_runs']}"
    )


@app.command("microstructure-status")
def microstructure_status(
    db: Path = typer.Option(
        Path("data/microstructure-shadow.db"),
        "--db",
        help="Independent microstructure evidence database",
    ),
) -> None:
    """Read an independent microstructure database without opening writes."""
    try:
        summary = MicrostructureStore(db, read_only=True).summary()
    except Exception as exc:
        console.print(
            f"[red]Microstructure status failed:[/] {type(exc).__name__}: {exc}"
        )
        raise typer.Exit(code=2) from exc
    session = summary.get("session")
    session_status = (
        str(session.get("status"))
        if isinstance(session, dict)
        else "none"
    )
    console.print(
        f"session_status={session_status} events={summary['events']} "
        f"symbols={summary['symbols']} snapshots={summary['snapshots']} "
        f"depth={summary['depth_events']} trades={summary['trades']} "
        f"marks={summary['marks']} features={summary['feature_samples']} "
        f"integrity_runs={summary['integrity_runs']} "
        f"factor_runs={summary['factor_runs']}"
    )


@app.command("factor-screen")
def factor_screen(
    input_path: Path = typer.Argument(
        ...,
        help="JSON envelope with a sealed spec and independent settled observations",
    ),
    output: Path | None = typer.Option(
        None,
        "--output",
        help="Optional JSON report output path",
    ),
) -> None:
    """Run the sealed R3 factor screen on an offline observation envelope."""
    try:
        payload = json.loads(input_path.read_text())
        if not isinstance(payload, dict):
            raise ValueError("factor input must be a JSON object")
        spec = _factor_spec_from_json(payload.get("spec"))
        raw_observations = payload.get("observations")
        if not isinstance(raw_observations, list):
            raise ValueError("factor input observations must be a JSON array")
        observations = tuple(
            _factor_observation_from_json(item) for item in raw_observations
        )
        report = FactorScreeningService().screen(spec, observations)
        encoded = json.dumps(
            _jsonable({"source_version": FACTOR_SCREENING_SOURCE_VERSION, "report": report}),
            sort_keys=True,
            indent=2,
        ) + "\n"
        if output is not None:
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(encoded)
    except Exception as exc:
        console.print(f"[red]Factor screen failed:[/] {type(exc).__name__}: {exc}")
        raise typer.Exit(code=2) from exc
    console.print(
        f"factor_screen complete groups={report.event_group_count} "
        f"dates={report.date_count} trials={len(report.trials)} "
        f"failed={report.failed_trial_count} promotion_allowed="
        f"{report.promotion_allowed}"
    )


@app.command("short-challenger-status")
def short_challenger_status(
    db_path: Path = typer.Option(
        Path("crypto_threshold.db"),
        "--db",
        help="Read-only Up/Down evidence database",
    ),
) -> None:
    """Show R0 checkpoint, latency, and settled-paper collection counts."""
    repository = Repository(Database(db_path, read_only=True))
    summary = repository.short_challenger_summary()
    console.print(
        "R0 challenger: "
        f"observations={summary['observations']} "
        f"targets={summary['target_times']} "
        f"assets={summary['assets']} "
        f"latency_replays={summary['latency_replays']} "
        f"settled={summary['settled_replays']} "
        f"paper_pnl={float(summary['settled_pnl_usdc']):.6f}"
    )

    checkpoint_table = Table("Checkpoint", "Observed", "Captured", "Rejected", "Labeled")
    for row in repository.short_challenger_checkpoint_summary_rows():
        checkpoint_table.add_row(
            f"T-{row['checkpoint_lead_seconds']}s",
            str(row["observations"]),
            str(row["captured"]),
            str(row["rejected"]),
            str(row["labeled"]),
        )
    console.print(checkpoint_table)

    latency_table = Table(
        "Latency",
        "Replays",
        "Entered",
        "Skipped",
        "Open",
        "Settled",
        "Settled PnL",
    )
    for row in repository.short_latency_summary_rows():
        latency_table.add_row(
            f"{row['latency_ms']} ms",
            str(row["replays"]),
            str(row["entered"]),
            str(row["skipped"]),
            str(row["open_count"]),
            str(row["settled"]),
            f"{float(row['settled_pnl_usdc']):.6f}",
        )
    console.print(latency_table)


@app.command("shadow")
def shadow(
    once: bool = typer.Option(False, "--once", help="Run exactly one research cycle"),
    duration_hours: float | None = typer.Option(
        None,
        "--duration-hours",
        min=0.001,
        help="Stop cleanly after this many wall-clock hours",
    ),
) -> None:
    """Run opt-in real-data shadow monitoring with a persistent paper ledger."""
    settings = get_settings()
    if once and duration_hours is not None:
        console.print("[red]Use either --once or --duration-hours, not both.[/]")
        raise typer.Exit(code=2)
    if not settings.SHADOW_ENABLED:
        console.print("[yellow]Shadow monitoring is disabled by default.[/]")
        raise typer.Exit(code=2)
    if (
        not settings.TRADING_DISABLED
        or not settings.POLYMARKET_STREAM_SHADOW_MODE
        or settings.POLYMARKET_STREAM_USER_CHANNEL_ENABLED
        or settings.POLYMARKET_PRIVATE_KEY is not None
    ):
        console.print("[red]Unsafe shadow configuration; refusing to start.[/]")
        raise typer.Exit(code=2)
    if settings.SHADOW_CONTRACT_FAMILY == SHORT_UPDOWN_FAMILY:
        try:
            CexDirectionArtifact.load(settings.SHORT_CEX_MODEL_PATH)
        except Exception as exc:
            console.print(
                "[red]short_updown shadow requires a valid sealed CEX model:[/] "
                f"{type(exc).__name__}: {exc}"
            )
            raise typer.Exit(code=2) from exc
    database = Database(settings.DATABASE_PATH)
    database.initialize()
    repository = Repository(database)
    client = GammaClobReadClient(settings)
    binance = BinanceProvider(settings.BINANCE_API_BASE)
    coinbase = CoinbaseProvider(settings.COINBASE_API_BASE)
    stream_coordinator: StreamResearchCoordinator | None = None
    if (
        settings.POLYMARKET_STREAM_ENABLED
        and settings.POLYMARKET_STREAM_SHADOW_MODE
        and not settings.POLYMARKET_STREAM_USER_CHANNEL_ENABLED
    ):
        bridge = PolymarketStreamBridge(
            enable_user_channel=False,
            stale_seconds=settings.POLYMARKET_STREAM_STALE_SECONDS,
            rest_verify_seconds=settings.POLYMARKET_STREAM_REST_VERIFY_SECONDS,
            max_quote_slots=settings.POLYMARKET_STREAM_MAX_QUOTE_SLOTS,
        )
        stream_coordinator = StreamResearchCoordinator(
            repository=repository,
            bridge=bridge,
            candidate_group_cap=settings.POLYMARKET_STREAM_CANDIDATE_GROUP_CAP,
        )
    reference_stream = (
        BinanceReferencePriceStream(
            stale_seconds=settings.BINANCE_REFERENCE_STREAM_STALE_SECONDS,
            max_tick_slots=settings.BINANCE_REFERENCE_STREAM_MAX_TICK_SLOTS,
            stream_url=settings.BINANCE_STREAM_URL,
            proxy_url=settings.BINANCE_STREAM_PROXY_URL,
        )
        if (
            settings.SHADOW_CONTRACT_FAMILY == DAILY_THRESHOLD_FAMILY
            and settings.BINANCE_REFERENCE_STREAM_ENABLED
        )
        else None
    )
    chainlink_stream = (
        ChainlinkReferencePriceStream(
            stale_seconds=settings.CHAINLINK_REFERENCE_STREAM_STALE_SECONDS,
            history_seconds=settings.CHAINLINK_REFERENCE_STREAM_HISTORY_SECONDS,
            max_ticks_per_pair=(
                settings.CHAINLINK_REFERENCE_STREAM_MAX_TICKS_PER_PAIR
            ),
        )
        if (
            settings.SHADOW_CONTRACT_FAMILY == SHORT_UPDOWN_FAMILY
            and settings.CHAINLINK_REFERENCE_STREAM_ENABLED
        )
        else None
    )
    workflow = MarketWorkflowService(
        client=client,
        repository=repository,
        binance=binance,
        coinbase=coinbase,
        settings=settings,
        stream_coordinator=stream_coordinator,
    )
    challenger = (
        ShortChallengerService(
            repository,
            client,
            min_net_ev=settings.PAPER_MIN_NET_EV,
            checkpoints_seconds=settings.short_challenger_checkpoints,
            latencies_ms=settings.short_challenger_latencies_ms,
            max_book_age_seconds=settings.MAX_BOOK_AGE_SECONDS,
        )
        if (
            settings.SHADOW_CONTRACT_FAMILY == SHORT_UPDOWN_FAMILY
            and settings.SHORT_CHALLENGER_ENABLED
        )
        else None
    )
    monitor = ShadowMonitorService(
        repository=repository,
        discovery=DiscoveryService(client, repository),
        workflow=workflow,
        paper=PaperLedgerService(
            repository, min_net_ev=settings.PAPER_MIN_NET_EV
        ),
        challenger=challenger,
        settlement=SettlementService(
            repository=repository,
            binance=binance,
            client=client,
        ),
        stream_coordinator=stream_coordinator,
        reference_stream=reference_stream,
        chainlink_stream=chainlink_stream,
        contract_family=settings.SHADOW_CONTRACT_FAMILY,
        discovery_limit=settings.SHADOW_DISCOVERY_LIMIT,
        analysis_limit=settings.SHADOW_ANALYSIS_LIMIT,
        settlement_limit=settings.SHADOW_SETTLEMENT_LIMIT,
    )
    deadline = (
        time.monotonic() + duration_hours * 3600
        if duration_hours is not None
        else None
    )
    completed_cycles = 0
    try:
        monitor.start()
        while True:
            if (
                deadline is not None
                and completed_cycles > 0
                and time.monotonic() >= deadline
            ):
                console.print(
                    f"Shadow duration complete after {completed_cycles} cycles"
                )
                break
            cycle = monitor.run_once()
            completed_cycles += 1
            console.print(
                f"shadow {cycle.status}: discovered={cycle.discovered_count} "
                f"analyzed={cycle.analyzed_count} entered={cycle.paper_entered_count}"
            )
            if once:
                if cycle.status == "degraded":
                    raise typer.Exit(code=2)
                break
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    console.print(
                        f"Shadow duration complete after {completed_cycles} cycles"
                    )
                    break
                time.sleep(min(settings.SHADOW_INTERVAL_SECONDS, remaining))
            else:
                time.sleep(settings.SHADOW_INTERVAL_SECONDS)
    except KeyboardInterrupt:
        console.print("Shadow monitoring stopped")
    except typer.Exit:
        raise
    except Exception as exc:
        console.print(f"[red]Shadow failed:[/] {type(exc).__name__}: {exc}")
        raise typer.Exit(code=1) from exc
    finally:
        monitor.stop()
        client.close()
        binance.close()
        coinbase.close()


def _valid_https_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme == "https" and bool(parsed.netloc)


def _valid_wss_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme == "wss" and bool(parsed.netloc)


def _factor_spec_from_json(value: object) -> FactorExperimentSpec:
    if not isinstance(value, dict):
        raise ValueError("factor input spec must be an object")
    raw_rules = value.get("rules")
    if not isinstance(raw_rules, list):
        raise ValueError("factor input spec rules must be an array")
    raw_assets = value.get("required_assets")
    if not isinstance(raw_assets, list):
        raise ValueError("factor input required_assets must be an array")
    rules: list[FactorRule] = []
    for raw_rule in raw_rules:
        if not isinstance(raw_rule, dict):
            raise ValueError("factor rule must be an object")
        rules.append(
            FactorRule(
                rule_id=str(raw_rule["rule_id"]),
                factor_name=str(raw_rule["factor_name"]),
                comparator=FactorComparator(str(raw_rule["comparator"])),
                threshold=Decimal(str(raw_rule["threshold"])),
                trade_side=FactorTradeSide(str(raw_rule["trade_side"])),
            )
        )
    return FactorExperimentSpec(
        experiment_id=str(value["experiment_id"]),
        spec_version=str(value["spec_version"]),
        created_at=_json_datetime(value["created_at"], "created_at"),
        training_cutoff_at=_json_datetime(
            value["training_cutoff_at"],
            "training_cutoff_at",
        ),
        minimum_oos_groups=int(value["minimum_oos_groups"]),
        minimum_dates=int(value["minimum_dates"]),
        minimum_groups_per_asset=int(value["minimum_groups_per_asset"]),
        required_assets=tuple(str(item).upper() for item in raw_assets),
        stake_usdc=Decimal(str(value["stake_usdc"])),
        frozen_model_version=str(value["frozen_model_version"]),
        market_baseline_version=str(value["market_baseline_version"]),
        integrity_source_version=str(value["integrity_source_version"]),
        replay_source_version=str(value["replay_source_version"]),
        rules=tuple(rules),
        spec_hash=str(value["spec_hash"]),
    )


def _factor_observation_from_json(value: object) -> FactorObservation:
    if not isinstance(value, dict):
        raise ValueError("factor observation must be an object")
    raw_factors = value.get("factor_values")
    if not isinstance(raw_factors, dict):
        raise ValueError("factor observation factor_values must be an object")
    return FactorObservation(
        observation_id=str(value["observation_id"]),
        event_group_id=str(value["event_group_id"]),
        asset=str(value["asset"]).upper(),
        target_time_utc=_json_datetime(value["target_time_utc"], "target_time_utc"),
        decision_at=_json_datetime(value["decision_at"], "decision_at"),
        factor_values={
            str(key): Decimal(str(item)) for key, item in raw_factors.items()
        },
        candidate_probability=Decimal(str(value["candidate_probability"])),
        market_probability=Decimal(str(value["market_probability"])),
        frozen_v4_probability=Decimal(str(value["frozen_v4_probability"])),
        outcome_yes=_json_boolean(value["outcome_yes"], "outcome_yes"),
        executable_yes_price=Decimal(str(value["executable_yes_price"])),
        executable_no_price=Decimal(str(value["executable_no_price"])),
        fee_rate=Decimal(str(value["fee_rate"])),
        fill_ratio=Decimal(str(value["fill_ratio"])),
        integrity_manifest_hash=str(value["integrity_manifest_hash"]),
        replay_manifest_hash=str(value["replay_manifest_hash"]),
    )


def _json_datetime(value: object, field: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be an ISO timestamp string")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return parsed.astimezone(UTC)


def _json_boolean(value: object, field: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{field} must be a JSON boolean")
    return value


def _jsonable(value: object) -> Any:
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if hasattr(value, "value") and not isinstance(value, (str, bytes)):
        enum_value = getattr(value, "value")
        if isinstance(enum_value, (str, int, float, bool)):
            return enum_value
    if hasattr(value, "__dataclass_fields__"):
        return _jsonable(asdict(cast(Any, value)))
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(item) for item in value]
    return value
