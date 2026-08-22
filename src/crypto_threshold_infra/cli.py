"""Command-line entrypoint for the strategy-neutral infrastructure."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from crypto_threshold_infra.capture import CaptureEngine, load_event_source
from crypto_threshold_infra.catalog import InstrumentCatalog, instruments_from_json
from crypto_threshold_infra.plugin import (
    AsyncPluginHost,
    PluginRecordStore,
    load_strategy_plugin,
    strategy_module_path,
)
from crypto_threshold_infra.safety import assert_capture_only_environment
from crypto_threshold_infra.store import CaptureStore, summarize_store


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="crypto-infra",
        description="Public-feed capture with isolated private strategy plugins",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    init = subparsers.add_parser("catalog-init", help="initialize an empty instrument catalog")
    init.add_argument("--catalog", type=Path, required=True)

    import_catalog = subparsers.add_parser(
        "catalog-import", help="replace enabled instruments from a JSON array"
    )
    import_catalog.add_argument("--catalog", type=Path, required=True)
    import_catalog.add_argument("--input", type=Path, required=True)

    check = subparsers.add_parser("plugin-check", help="load and validate a private plugin")
    check.add_argument("--strategy", required=True, help="module:factory")
    check.add_argument("--config", type=Path)

    capture = subparsers.add_parser("capture", help="run public raw capture")
    capture.add_argument("--catalog", type=Path, required=True)
    capture.add_argument("--output", type=Path, required=True)
    capture.add_argument("--source", action="append", required=True, help="module:factory")
    capture.add_argument("--source-config", type=Path)
    capture.add_argument("--strategy", help="private module:factory; omit for raw-only capture")
    capture.add_argument("--strategy-config", type=Path)
    capture.add_argument("--strategy-output", type=Path)
    capture.add_argument("--once", action="store_true")
    capture.add_argument("--duration-seconds", type=float)
    capture.add_argument("--poll-seconds", type=float, default=0.5)
    capture.add_argument("--batch-limit", type=int, default=10_000)

    status = subparsers.add_parser("status", help="read-only raw partition summary")
    status.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "catalog-init":
        InstrumentCatalog(args.catalog).initialize()
        print(json.dumps({"catalog": str(args.catalog), "status": "initialized"}))
        return 0
    if args.command == "catalog-import":
        rows = instruments_from_json(args.input)
        count = InstrumentCatalog(args.catalog).replace(rows)
        print(json.dumps({"catalog": str(args.catalog), "enabled": count}))
        return 0
    if args.command == "plugin-check":
        config = _json_object(args.config)
        plugin = load_strategy_plugin(args.strategy, config)
        print(
            json.dumps(
                {
                    "name": plugin.name,
                    "module_path": str(strategy_module_path(args.strategy)),
                    "status": "valid",
                },
                sort_keys=True,
            )
        )
        return 0
    if args.command == "status":
        print(json.dumps(summarize_store(args.output), sort_keys=True))
        return 0
    if args.command == "capture":
        return _capture(args)
    raise AssertionError(args.command)


def _capture(args: argparse.Namespace) -> int:
    assert_capture_only_environment(os.environ)
    source_config = _json_object(args.source_config)
    sources = tuple(load_event_source(spec, source_config) for spec in args.source)
    plugin_host: AsyncPluginHost | None = None
    if args.strategy:
        if args.strategy_output is None:
            raise SystemExit("--strategy-output is required with --strategy")
        strategy_config = _json_object(args.strategy_config)
        plugin = load_strategy_plugin(args.strategy, strategy_config)
        plugin_host = AsyncPluginHost(
            plugin,
            PluginRecordStore(args.strategy_output),
            config=strategy_config,
        )
    engine = CaptureEngine(
        catalog=InstrumentCatalog(args.catalog),
        store=CaptureStore(args.output),
        sources=sources,
        plugin_host=plugin_host,
        poll_seconds=args.poll_seconds,
        batch_limit=args.batch_limit,
    )
    result = engine.run(once=args.once, duration_seconds=args.duration_seconds)
    output: dict[str, object] = {
        "session_id": result.session_id,
        "status": result.status,
        "persisted_events": result.persisted_events,
        "duplicate_events": result.duplicate_events,
        "source_health": result.source_health,
        "authenticated": 0,
        "signed": 0,
        "submitted": 0,
    }
    if plugin_host is not None:
        output["plugin_health"] = plugin_host.health()
    print(json.dumps(output, sort_keys=True))
    return 0 if result.status == "completed" else 1


def _json_object(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"configuration must be a JSON object: {path}")
    return dict(value)


if __name__ == "__main__":
    raise SystemExit(main())
