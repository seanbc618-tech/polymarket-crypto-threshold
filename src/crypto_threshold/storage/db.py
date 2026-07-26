"""Versioned SQLite database and transaction boundary."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path
from sqlite3 import Connection, Row, complete_statement, connect

SCHEMA_VERSION = 5

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    version INTEGER NOT NULL,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS markets (
    market_id TEXT PRIMARY KEY,
    event_id TEXT,
    condition_id TEXT,
    question TEXT NOT NULL,
    slug TEXT,
    description TEXT,
    active INTEGER,
    closed INTEGER,
    accepting_orders INTEGER,
    enable_order_book INTEGER,
    end_date TEXT,
    gamma_end_date TEXT,
    outcomes TEXT,
    tokens TEXT,
    yes_token_id TEXT,
    no_token_id TEXT,
    raw_payload TEXT NOT NULL DEFAULT '{}',
    raw_observed_at TEXT,
    raw_received_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    source_version TEXT NOT NULL DEFAULT 'gamma-v1',
    event_start_time TEXT,
    series_slug TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS resolution_rules (
    rule_id TEXT PRIMARY KEY,
    market_id TEXT NOT NULL REFERENCES markets(market_id),
    event_id TEXT,
    condition_id TEXT,
    yes_token_id TEXT,
    no_token_id TEXT,
    asset TEXT NOT NULL DEFAULT '',
    quote TEXT NOT NULL DEFAULT '',
    settlement_source TEXT,
    pair TEXT,
    operator TEXT NOT NULL DEFAULT '',
    exact_operator TEXT,
    threshold TEXT NOT NULL DEFAULT '0',
    strike TEXT,
    candle_interval TEXT,
    price_field TEXT,
    timezone TEXT,
    observation_time TEXT,
    target_time_utc TEXT,
    gamma_end_date TEXT,
    rule_confidence REAL NOT NULL DEFAULT 0,
    tradable INTEGER NOT NULL DEFAULT 0,
    preview_only INTEGER NOT NULL DEFAULT 1,
    rejection_reason TEXT,
    raw_text TEXT,
    raw_description TEXT,
    parser_version TEXT,
    observed_at TEXT,
    received_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    source_version TEXT NOT NULL DEFAULT 'contract-parser-v1',
    contract_family TEXT NOT NULL DEFAULT 'daily_threshold',
    boundary_type TEXT NOT NULL DEFAULT 'fixed_strike',
    window_start_time_utc TEXT,
    affirmative_outcome TEXT NOT NULL DEFAULT 'Yes',
    negative_outcome TEXT NOT NULL DEFAULT 'No',
    series_slug TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS external_payloads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id TEXT REFERENCES markets(market_id),
    analysis_run_id TEXT,
    source TEXT NOT NULL,
    payload_kind TEXT NOT NULL,
    observed_at TEXT,
    received_at TEXT NOT NULL,
    source_version TEXT NOT NULL,
    raw_payload TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS market_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    market_id TEXT NOT NULL REFERENCES markets(market_id),
    token_id TEXT NOT NULL,
    outcome TEXT NOT NULL,
    best_bid TEXT,
    best_ask TEXT,
    midpoint TEXT,
    spread TEXT,
    bid_depth TEXT,
    ask_depth TEXT,
    observed_at TEXT NOT NULL,
    received_at TEXT NOT NULL,
    source_version TEXT NOT NULL,
    timestamp_trusted INTEGER NOT NULL DEFAULT 0,
    raw_payload TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS market_fee_schedules (
    schedule_id TEXT PRIMARY KEY,
    market_id TEXT NOT NULL REFERENCES markets(market_id),
    condition_id TEXT NOT NULL,
    fee_rate TEXT,
    exponent TEXT,
    taker_only INTEGER,
    valid INTEGER NOT NULL,
    rejection_reason TEXT,
    observed_at TEXT NOT NULL,
    received_at TEXT NOT NULL,
    source_version TEXT NOT NULL,
    raw_payload TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS price_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    market_id TEXT REFERENCES markets(market_id),
    asset TEXT NOT NULL,
    quote TEXT NOT NULL,
    provider TEXT NOT NULL,
    symbol TEXT NOT NULL,
    price TEXT NOT NULL,
    price_kind TEXT NOT NULL DEFAULT 'spot',
    observed_at TEXT NOT NULL,
    received_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    source_version TEXT NOT NULL DEFAULT 'unknown',
    raw_payload TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS price_cross_checks (
    check_id TEXT PRIMARY KEY,
    market_id TEXT REFERENCES markets(market_id),
    asset TEXT NOT NULL,
    primary_provider TEXT NOT NULL,
    secondary_provider TEXT NOT NULL,
    primary_price TEXT NOT NULL,
    secondary_price TEXT NOT NULL,
    relative_diff TEXT NOT NULL,
    ok INTEGER NOT NULL,
    reasons TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    received_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    source_version TEXT NOT NULL DEFAULT 'price-cross-check-v1',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS analysis_signals (
    signal_id TEXT PRIMARY KEY,
    market_id TEXT NOT NULL REFERENCES markets(market_id),
    analysis_run_id TEXT,
    asset TEXT NOT NULL DEFAULT '',
    threshold TEXT,
    deadline TEXT,
    estimated_probability TEXT,
    probability_low TEXT,
    probability_high TEXT,
    market_probability TEXT,
    edge TEXT,
    yes_midpoint TEXT,
    no_midpoint TEXT,
    yes_ask_vwap TEXT,
    no_ask_vwap TEXT,
    target_size_usdc TEXT,
    fee_rate TEXT,
    yes_fee_per_share TEXT,
    no_fee_per_share TEXT,
    yes_spread_cost TEXT,
    no_spread_cost TEXT,
    yes_slippage_cost TEXT,
    no_slippage_cost TEXT,
    yes_net_ev TEXT,
    no_net_ev TEXT,
    selected_outcome TEXT,
    net_ev TEXT,
    status TEXT NOT NULL DEFAULT 'rejected',
    model_name TEXT,
    model_version TEXT NOT NULL DEFAULT 'unknown',
    confidence TEXT,
    reasons TEXT NOT NULL DEFAULT '[]',
    input_payload_max_id INTEGER,
    observed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    received_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    source_version TEXT NOT NULL DEFAULT 'market-workflow-v1',
    contract_family TEXT NOT NULL DEFAULT 'daily_threshold',
    affirmative_outcome TEXT NOT NULL DEFAULT 'Yes',
    negative_outcome TEXT NOT NULL DEFAULT 'No',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

"""

PHASE2_SCHEMA = """
CREATE TABLE IF NOT EXISTS analysis_signal_inputs (
    signal_id TEXT NOT NULL REFERENCES analysis_signals(signal_id) ON DELETE CASCADE,
    payload_id INTEGER NOT NULL REFERENCES external_payloads(id),
    input_role TEXT NOT NULL,
    PRIMARY KEY (signal_id, payload_id, input_role)
);

CREATE TABLE IF NOT EXISTS settlement_labels (
    label_id TEXT PRIMARY KEY,
    market_id TEXT NOT NULL REFERENCES markets(market_id),
    target_time_utc TEXT NOT NULL,
    provider TEXT NOT NULL,
    pair TEXT NOT NULL,
    candle_interval TEXT NOT NULL,
    price_field TEXT NOT NULL,
    exact_operator TEXT NOT NULL,
    strike TEXT NOT NULL,
    observed_value TEXT NOT NULL,
    outcome_yes INTEGER NOT NULL,
    payload_id INTEGER NOT NULL REFERENCES external_payloads(id),
    observed_at TEXT NOT NULL,
    received_at TEXT NOT NULL,
    source_version TEXT NOT NULL,
    contract_family TEXT NOT NULL DEFAULT 'daily_threshold',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (market_id, target_time_utc, source_version)
);

CREATE TABLE IF NOT EXISTS settlement_attempts (
    market_id TEXT PRIMARY KEY REFERENCES markets(market_id),
    target_time_utc TEXT NOT NULL,
    contract_family TEXT NOT NULL,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    last_attempt_at TEXT,
    next_attempt_at TEXT,
    last_payload_id INTEGER REFERENCES external_payloads(id),
    last_payload_hash TEXT,
    last_status TEXT NOT NULL DEFAULT 'never'
        CHECK (last_status IN ('never', 'in_progress', 'pending', 'succeeded', 'error')),
    last_reason TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS replay_datasets (
    dataset_id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL CHECK (status IN ('building', 'sealed', 'failed')),
    manifest_hash TEXT,
    item_count INTEGER NOT NULL DEFAULT 0,
    config_json TEXT NOT NULL,
    source_version TEXT NOT NULL,
    created_at TEXT NOT NULL,
    sealed_at TEXT
);

CREATE TABLE IF NOT EXISTS replay_items (
    dataset_id TEXT NOT NULL REFERENCES replay_datasets(dataset_id) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL,
    signal_id TEXT NOT NULL REFERENCES analysis_signals(signal_id),
    label_id TEXT NOT NULL REFERENCES settlement_labels(label_id),
    decision_at TEXT NOT NULL,
    label_available_at TEXT NOT NULL,
    feature_payload TEXT NOT NULL,
    feature_hash TEXT NOT NULL,
    input_manifest_hash TEXT NOT NULL,
    PRIMARY KEY (dataset_id, signal_id),
    UNIQUE (dataset_id, ordinal)
);

CREATE TABLE IF NOT EXISTS calibration_runs (
    run_id TEXT PRIMARY KEY,
    dataset_id TEXT NOT NULL REFERENCES replay_datasets(dataset_id),
    status TEXT NOT NULL,
    method TEXT NOT NULL,
    bins INTEGER NOT NULL,
    min_train_size INTEGER NOT NULL,
    sample_count INTEGER NOT NULL,
    evaluated_count INTEGER NOT NULL,
    metrics_json TEXT NOT NULL,
    rejection_reason TEXT,
    model_version TEXT NOT NULL,
    source_version TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS paper_ledger (
    entry_id TEXT PRIMARY KEY,
    signal_id TEXT NOT NULL REFERENCES analysis_signals(signal_id),
    market_id TEXT NOT NULL REFERENCES markets(market_id),
    policy_version TEXT NOT NULL,
    action TEXT NOT NULL CHECK (action IN ('enter', 'skip')),
    outcome TEXT,
    status TEXT NOT NULL CHECK (status IN ('skipped', 'open', 'settled')),
    size_usdc TEXT NOT NULL,
    entry_vwap TEXT,
    fee_per_share TEXT,
    shares TEXT,
    total_fee TEXT,
    net_ev TEXT,
    label_id TEXT REFERENCES settlement_labels(label_id),
    outcome_yes INTEGER,
    payout_usdc TEXT,
    pnl_usdc TEXT,
    reasons TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    received_at TEXT NOT NULL,
    settled_at TEXT,
    source_version TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (signal_id, policy_version)
);

CREATE TABLE IF NOT EXISTS shadow_cycles (
    cycle_id TEXT PRIMARY KEY,
    mode TEXT NOT NULL CHECK (mode = 'shadow'),
    status TEXT NOT NULL,
    discovered_count INTEGER NOT NULL,
    analyzed_count INTEGER NOT NULL,
    paper_entered_count INTEGER NOT NULL,
    paper_skipped_count INTEGER NOT NULL,
    stream_health_json TEXT NOT NULL,
    reasons TEXT NOT NULL,
    source_version TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT NOT NULL,
    contract_family TEXT NOT NULL DEFAULT 'daily_threshold'
);

CREATE TRIGGER IF NOT EXISTS protect_settlement_labels_update
BEFORE UPDATE ON settlement_labels
BEGIN
    SELECT RAISE(ABORT, 'settlement labels are immutable');
END;

CREATE TRIGGER IF NOT EXISTS protect_settlement_labels_delete
BEFORE DELETE ON settlement_labels
BEGIN
    SELECT RAISE(ABORT, 'settlement labels are immutable');
END;

CREATE TRIGGER IF NOT EXISTS protect_sealed_replay_update
BEFORE UPDATE ON replay_datasets
WHEN OLD.status = 'sealed'
BEGIN
    SELECT RAISE(ABORT, 'sealed replay datasets are immutable');
END;

CREATE TRIGGER IF NOT EXISTS protect_sealed_replay_delete
BEFORE DELETE ON replay_datasets
WHEN OLD.status = 'sealed'
BEGIN
    SELECT RAISE(ABORT, 'sealed replay datasets are immutable');
END;

CREATE TRIGGER IF NOT EXISTS protect_replay_items_insert
BEFORE INSERT ON replay_items
WHEN (SELECT status FROM replay_datasets WHERE dataset_id = NEW.dataset_id) != 'building'
BEGIN
    SELECT RAISE(ABORT, 'replay items require a building dataset');
END;

CREATE TRIGGER IF NOT EXISTS protect_replay_items_update
BEFORE UPDATE ON replay_items
BEGIN
    SELECT RAISE(ABORT, 'replay items are immutable');
END;

CREATE TRIGGER IF NOT EXISTS protect_replay_items_delete
BEFORE DELETE ON replay_items
BEGIN
    SELECT RAISE(ABORT, 'replay items are immutable');
END;
"""

INDEXES = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_markets_condition_id
ON markets(condition_id) WHERE condition_id IS NOT NULL AND condition_id != '';

CREATE UNIQUE INDEX IF NOT EXISTS idx_resolution_rules_market
ON resolution_rules(market_id);

CREATE INDEX IF NOT EXISTS idx_external_payloads_market_id
ON external_payloads(market_id, id);

CREATE INDEX IF NOT EXISTS idx_market_snapshots_market_token
ON market_snapshots(market_id, token_id, received_at DESC);

CREATE INDEX IF NOT EXISTS idx_fee_schedules_market
ON market_fee_schedules(market_id, received_at DESC);

CREATE INDEX IF NOT EXISTS idx_price_snapshots_market_provider
ON price_snapshots(market_id, provider, received_at DESC);

CREATE INDEX IF NOT EXISTS idx_analysis_signals_market
ON analysis_signals(market_id, received_at DESC);

CREATE INDEX IF NOT EXISTS idx_analysis_signal_inputs_payload
ON analysis_signal_inputs(payload_id);

CREATE INDEX IF NOT EXISTS idx_settlement_labels_market
ON settlement_labels(market_id, target_time_utc);

CREATE INDEX IF NOT EXISTS idx_settlement_attempts_due
ON settlement_attempts(next_attempt_at, target_time_utc, market_id);

CREATE INDEX IF NOT EXISTS idx_replay_items_dataset_order
ON replay_items(dataset_id, ordinal);

CREATE INDEX IF NOT EXISTS idx_paper_ledger_market_status
ON paper_ledger(market_id, policy_version, status);

CREATE INDEX IF NOT EXISTS idx_shadow_cycles_started
ON shadow_cycles(started_at DESC);
"""


MARKET_COLUMNS: dict[str, str] = {
    "event_id": "TEXT",
    "condition_id": "TEXT",
    "accepting_orders": "INTEGER",
    "enable_order_book": "INTEGER",
    "gamma_end_date": "TEXT",
    "yes_token_id": "TEXT",
    "no_token_id": "TEXT",
    "raw_observed_at": "TEXT",
    "raw_received_at": "TEXT",
    "source_version": "TEXT NOT NULL DEFAULT 'gamma-v1'",
    "event_start_time": "TEXT",
    "series_slug": "TEXT",
}

RULE_COLUMNS: dict[str, str] = {
    "event_id": "TEXT",
    "condition_id": "TEXT",
    "yes_token_id": "TEXT",
    "no_token_id": "TEXT",
    "pair": "TEXT",
    "exact_operator": "TEXT",
    "strike": "TEXT",
    "candle_interval": "TEXT",
    "price_field": "TEXT",
    "timezone": "TEXT",
    "observation_time": "TEXT",
    "gamma_end_date": "TEXT",
    "preview_only": "INTEGER NOT NULL DEFAULT 1",
    "raw_description": "TEXT",
    "observed_at": "TEXT",
    "received_at": "TEXT",
    "source_version": "TEXT NOT NULL DEFAULT 'contract-parser-v1'",
    "updated_at": "TEXT",
    "contract_family": "TEXT NOT NULL DEFAULT 'daily_threshold'",
    "boundary_type": "TEXT NOT NULL DEFAULT 'fixed_strike'",
    "window_start_time_utc": "TEXT",
    "affirmative_outcome": "TEXT NOT NULL DEFAULT 'Yes'",
    "negative_outcome": "TEXT NOT NULL DEFAULT 'No'",
    "series_slug": "TEXT",
}

PRICE_COLUMNS: dict[str, str] = {
    "market_id": "TEXT",
    "price_kind": "TEXT NOT NULL DEFAULT 'spot'",
    "received_at": "TEXT",
    "source_version": "TEXT NOT NULL DEFAULT 'unknown'",
}

CROSS_CHECK_COLUMNS: dict[str, str] = {
    "market_id": "TEXT",
    "observed_at": "TEXT",
    "received_at": "TEXT",
    "source_version": "TEXT NOT NULL DEFAULT 'price-cross-check-v1'",
}

SIGNAL_COLUMNS: dict[str, str] = {
    "analysis_run_id": "TEXT",
    "yes_midpoint": "TEXT",
    "no_midpoint": "TEXT",
    "yes_ask_vwap": "TEXT",
    "no_ask_vwap": "TEXT",
    "target_size_usdc": "TEXT",
    "fee_rate": "TEXT",
    "yes_fee_per_share": "TEXT",
    "no_fee_per_share": "TEXT",
    "yes_spread_cost": "TEXT",
    "no_spread_cost": "TEXT",
    "yes_slippage_cost": "TEXT",
    "no_slippage_cost": "TEXT",
    "yes_net_ev": "TEXT",
    "no_net_ev": "TEXT",
    "selected_outcome": "TEXT",
    "net_ev": "TEXT",
    "status": "TEXT NOT NULL DEFAULT 'rejected'",
    "model_version": "TEXT NOT NULL DEFAULT 'unknown'",
    "input_payload_max_id": "INTEGER",
    "observed_at": "TEXT",
    "received_at": "TEXT",
    "source_version": "TEXT NOT NULL DEFAULT 'market-workflow-v1'",
    "contract_family": "TEXT NOT NULL DEFAULT 'daily_threshold'",
    "affirmative_outcome": "TEXT NOT NULL DEFAULT 'Yes'",
    "negative_outcome": "TEXT NOT NULL DEFAULT 'No'",
}

EXTERNAL_PAYLOAD_COLUMNS: dict[str, str] = {
    "analysis_run_id": "TEXT",
}

SETTLEMENT_LABEL_COLUMNS: dict[str, str] = {
    "contract_family": "TEXT NOT NULL DEFAULT 'daily_threshold'",
}

SHADOW_CYCLE_COLUMNS: dict[str, str] = {
    "contract_family": "TEXT NOT NULL DEFAULT 'daily_threshold'",
}

ANALYSIS_SIGNALS_V2 = """
CREATE TABLE analysis_signals (
    signal_id TEXT PRIMARY KEY,
    market_id TEXT NOT NULL REFERENCES markets(market_id),
    analysis_run_id TEXT,
    asset TEXT NOT NULL DEFAULT '',
    threshold TEXT,
    deadline TEXT,
    estimated_probability TEXT,
    probability_low TEXT,
    probability_high TEXT,
    market_probability TEXT,
    edge TEXT,
    yes_midpoint TEXT,
    no_midpoint TEXT,
    yes_ask_vwap TEXT,
    no_ask_vwap TEXT,
    target_size_usdc TEXT,
    fee_rate TEXT,
    yes_fee_per_share TEXT,
    no_fee_per_share TEXT,
    yes_spread_cost TEXT,
    no_spread_cost TEXT,
    yes_slippage_cost TEXT,
    no_slippage_cost TEXT,
    yes_net_ev TEXT,
    no_net_ev TEXT,
    selected_outcome TEXT,
    net_ev TEXT,
    status TEXT NOT NULL DEFAULT 'rejected',
    model_name TEXT,
    model_version TEXT NOT NULL DEFAULT 'unknown',
    confidence TEXT,
    reasons TEXT NOT NULL DEFAULT '[]',
    input_payload_max_id INTEGER,
    observed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    received_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    source_version TEXT NOT NULL DEFAULT 'market-workflow-v1',
    contract_family TEXT NOT NULL DEFAULT 'daily_threshold',
    affirmative_outcome TEXT NOT NULL DEFAULT 'Yes',
    negative_outcome TEXT NOT NULL DEFAULT 'No',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""


class Database:
    """SQLite connection owner with migrations and explicit transactions."""

    def __init__(
        self,
        path: str | Path = "crypto_threshold.db",
        *,
        read_only: bool = False,
    ) -> None:
        self.path = Path(path).expanduser().resolve()
        self.read_only = read_only

    def connect(self) -> Connection:
        """Open a configured SQLite connection."""
        if self.read_only:
            if not self.path.is_file():
                raise FileNotFoundError(f"database does not exist: {self.path}")
            connection = connect(
                f"{self.path.as_uri()}?mode=ro",
                timeout=30,
                uri=True,
            )
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            connection = connect(self.path, timeout=30)
        connection.row_factory = Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        if self.read_only:
            connection.execute("PRAGMA query_only = ON")
        else:
            connection.execute("PRAGMA journal_mode = WAL")
        return connection

    def initialize(self) -> None:
        """Create or migrate the schema to the current version."""
        if self.read_only:
            raise RuntimeError("cannot initialize a read-only database")
        with self.transaction() as connection:
            _execute_statements(connection, SCHEMA)
            _ensure_columns(connection, "markets", MARKET_COLUMNS)
            _ensure_columns(connection, "resolution_rules", RULE_COLUMNS)
            _ensure_columns(connection, "price_snapshots", PRICE_COLUMNS)
            _ensure_columns(connection, "price_cross_checks", CROSS_CHECK_COLUMNS)
            _ensure_columns(connection, "analysis_signals", SIGNAL_COLUMNS)
            _ensure_columns(connection, "external_payloads", EXTERNAL_PAYLOAD_COLUMNS)
            _rebuild_legacy_analysis_signals(connection)
            _execute_statements(connection, PHASE2_SCHEMA)
            _ensure_columns(connection, "settlement_labels", SETTLEMENT_LABEL_COLUMNS)
            _ensure_columns(connection, "shadow_cycles", SHADOW_CYCLE_COLUMNS)
            _execute_statements(connection, INDEXES)
            connection.execute(
                "INSERT OR IGNORE INTO schema_meta (id, version) VALUES (1, ?)",
                (SCHEMA_VERSION,),
            )
            connection.execute(
                "UPDATE schema_meta SET version = ?, updated_at = CURRENT_TIMESTAMP WHERE id = 1",
                (SCHEMA_VERSION,),
            )

    init_schema = initialize

    @contextmanager
    def transaction(self) -> Iterator[Connection]:
        """Commit the unit of work or roll it back on any exception."""
        if self.read_only:
            raise RuntimeError("cannot open a write transaction on a read-only database")
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def health(self) -> dict[str, object]:
        """Return concrete DB health evidence for ``doctor``."""
        with closing(self.connect()) as connection:
            connection.execute("SELECT 1").fetchone()
            row = connection.execute(
                "SELECT version FROM schema_meta WHERE id = 1"
            ).fetchone()
            foreign_keys = int(connection.execute("PRAGMA foreign_keys").fetchone()[0])
            journal_mode = str(connection.execute("PRAGMA journal_mode").fetchone()[0])
        return {
            "ok": row is not None and int(row["version"]) == SCHEMA_VERSION,
            "schema_version": int(row["version"]) if row is not None else None,
            "foreign_keys": foreign_keys == 1,
            "journal_mode": journal_mode.lower(),
            "path": str(self.path),
        }


def _ensure_columns(connection: Connection, table: str, columns: dict[str, str]) -> None:
    existing = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}
    for name, definition in columns.items():
        if name not in existing:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def _execute_statements(connection: Connection, script: str) -> None:
    statement = ""
    for line in script.splitlines(keepends=True):
        statement += line
        if complete_statement(statement):
            connection.execute(statement)
            statement = ""
    if statement.strip():
        raise ValueError("incomplete SQL migration statement")


def _rebuild_legacy_analysis_signals(connection: Connection) -> None:
    columns = list(connection.execute("PRAGMA table_info(analysis_signals)"))
    threshold = next((row for row in columns if row["name"] == "threshold"), None)
    if threshold is None or int(threshold["notnull"]) == 0:
        return

    connection.execute("ALTER TABLE analysis_signals RENAME TO analysis_signals_legacy")
    connection.execute(ANALYSIS_SIGNALS_V2)
    connection.execute(
        """
        INSERT INTO analysis_signals (
            signal_id, market_id, analysis_run_id, asset, threshold, deadline,
            estimated_probability, probability_low, probability_high,
            market_probability, edge, model_name, model_version, confidence,
            reasons, status, observed_at, received_at, source_version, created_at
        )
        SELECT
            signal_id, market_id, analysis_run_id, asset, threshold, deadline,
            estimated_probability, probability_low, probability_high,
            market_probability, edge, model_name,
            COALESCE(model_version, 'legacy'), confidence,
            COALESCE(reasons, '[]'), COALESCE(status, 'legacy'),
            COALESCE(observed_at, created_at, CURRENT_TIMESTAMP),
            COALESCE(received_at, created_at, CURRENT_TIMESTAMP),
            COALESCE(source_version, 'legacy'), created_at
        FROM analysis_signals_legacy
        """
    )
    connection.execute("DROP TABLE analysis_signals_legacy")
