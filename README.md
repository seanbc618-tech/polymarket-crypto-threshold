# Polymarket Crypto Infrastructure

Strategy-neutral infrastructure for recording public market data and running a
private research or shadow plugin outside the raw persistence path.

This repository intentionally contains **no alpha, model, threshold, feature,
quote rule, fill rule, hedge rule, private key, authenticated client, signer, or
order submission code**. Private plugins and their configuration belong in an
ignored local/VPS directory or a separate private package.

## Architecture

```text
public source adapters
        │
        ▼
persist-first capture engine ──► immutable 4-hour UTC raw SQLite partitions
        │
        └── after commit ──► bounded async plugin host ──► separate plugin DB
```

The raw recorder and plugin output never share a database. A slow or failed
plugin is marked failed without blocking raw persistence. Source overflow,
dropped observations, or fatal source status fail the capture process closed.

## Private strategy interface

A plugin implements `name`, `start`, `replace_instruments`, `observe`, `tick`,
and `finish`. Put implementation files under `private_plugins/` or install a
separate private Python package. Both `private_plugins/` and
`strategy_plugins/` are ignored.

```bash
crypto-infra plugin-check \
  --strategy private_plugins.my_strategy:create_strategy \
  --config /etc/my-private-strategy.json
```

The factory receives the decoded JSON object and returns the plugin instance.
The infrastructure does not interpret plugin records; it persists them in a
separate `plugin_records` table.

## Source adapters

Sources use the same `module:factory` boundary and supply public `RawEvent`
objects plus health, start, stop, drain, and catalog callbacks. The included
JSONL adapter is deterministic and useful for end-to-end validation:

```bash
crypto-infra capture \
  --catalog data/catalog.db \
  --output data/raw \
  --source crypto_threshold_infra.sources.jsonl:create_source \
  --source-config local-source.json \
  --once
```

Live venue adapters can be installed as separate packages without changing the
capture engine or plugin contract.

## Explicit catalog

The infrastructure does not select markets. Import an explicit instrument list:

```json
[
  {
    "instrument_id": "public-instrument-id",
    "venue": "venue-name",
    "market_id": "public-market-id",
    "token_id": "public-token-id",
    "asset": "BTC",
    "metadata": {}
  }
]
```

```bash
crypto-infra catalog-init --catalog data/catalog.db
crypto-infra catalog-import --catalog data/catalog.db --input instruments.json
```

## Safety boundary

The capture command rejects account keys, funder addresses, authenticated user
channels, and live-trading environment flags. There is no execution gateway in
this repository.

## Verification

```bash
uv sync
uv run pytest -q
uv run ruff check src tests examples
uv run mypy src/crypto_threshold_infra
git diff --check
```

See [Plugin contract](docs/PLUGIN-CONTRACT.md) for lifecycle and isolation rules.

