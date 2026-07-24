"""Calibration, paper-ledger, shadow, and readiness pages."""

from __future__ import annotations

import json

from crypto_threshold.adapters.keychain import KeychainStore
from crypto_threshold.config import Settings
from crypto_threshold.dashboard_ui.html import (
    definition_table,
    e,
    flash,
    link,
    render_page,
    section,
    table,
)
from crypto_threshold.dashboard_ui.i18n import t
from crypto_threshold.services.readiness_service import (
    ResearchReadinessService,
    required_readiness_ok,
)
from crypto_threshold.storage.repositories import Repository


def render_calibration(
    repository: Repository,
    *,
    lang: str,
    current_path: str,
    query: dict[str, list[str]],
) -> str:
    datasets = repository.list_replay_datasets()
    runs = repository.list_calibration_runs()
    body = (
        flash(query, lang)
        + f"<h2>{e(t(lang, 'calibration.title'))}</h2>"
        + section(
            "Replay datasets",
            table(
                ["Name", "Status", "Items", "Manifest", "Created", "Sealed"],
                [
                    [
                        row["name"],
                        row["status"],
                        row["item_count"],
                        row["manifest_hash"],
                        row["created_at"],
                        row["sealed_at"],
                    ]
                    for row in datasets
                ],
                lang,
            ),
        )
        + section(
            "Walk-forward calibration runs",
            table(
                [
                    "Dataset",
                    "Status",
                    "Samples",
                    "Evaluated",
                    "Method",
                    "Metrics",
                    "Rejection",
                    "Completed",
                ],
                [
                    [
                        row["dataset_name"],
                        row["status"],
                        row["sample_count"],
                        row["evaluated_count"],
                        row["method"],
                        _compact_json(row["metrics_json"]),
                        row["rejection_reason"],
                        row["completed_at"],
                    ]
                    for row in runs
                ],
                lang,
            ),
        )
    )
    return render_page(t(lang, "calibration.title"), body, lang, current_path)


def render_paper(
    repository: Repository,
    *,
    lang: str,
    current_path: str,
    query: dict[str, list[str]],
) -> str:
    summary = repository.paper_summary()
    entries = repository.list_paper_entries()
    body = (
        flash(query, lang)
        + f"<h2>{e(t(lang, 'paper.title'))}</h2>"
        + section(
            "Summary",
            definition_table(
                [
                    ("total", summary["total"]),
                    ("open", summary["open_count"]),
                    ("settled", summary["settled_count"]),
                    ("skipped", summary["skipped_count"]),
                    ("settled PnL (USDC)", summary["settled_pnl_usdc"]),
                ]
            ),
        )
        + section(
            t(lang, "paper.title"),
            table(
                [
                    "Observed",
                    "Market",
                    "Asset",
                    "Strike",
                    "Settlement",
                    "Action",
                    "Outcome",
                    "Status",
                    "Size",
                    "Entry VWAP",
                    "Net EV",
                    "PnL",
                    "Reasons",
                ],
                [
                    [
                        row["observed_at"],
                        link(f"/markets/{row['market_id']}", row["question"], lang),
                        row["asset"],
                        row["strike"],
                        row["target_time_utc"],
                        row["action"],
                        row["outcome"],
                        row["status"],
                        row["size_usdc"],
                        row["entry_vwap"],
                        row["net_ev"],
                        row["pnl_usdc"],
                        _compact_json(row["reasons"]),
                    ]
                    for row in entries
                ],
                lang,
            ),
        )
    )
    return render_page(t(lang, "paper.title"), body, lang, current_path)


def render_shadow(
    repository: Repository,
    *,
    lang: str,
    current_path: str,
    query: dict[str, list[str]],
) -> str:
    cycles = repository.list_shadow_cycles()
    content = table(
        [
            "Started",
            "Completed",
            "Status",
            "Discovered",
            "Analyzed",
            "Paper enter",
            "Paper skip",
            "Stream health",
            "Reasons",
        ],
        [
            [
                row["started_at"],
                row["completed_at"],
                row["status"],
                row["discovered_count"],
                row["analyzed_count"],
                row["paper_entered_count"],
                row["paper_skipped_count"],
                _compact_json(row["stream_health_json"]),
                _compact_json(row["reasons"]),
            ]
            for row in cycles
        ],
        lang,
    )
    body = (
        flash(query, lang)
        + f"<h2>{e(t(lang, 'shadow.title'))}</h2>"
        + '<p class="lede">WebSocket is a bounded hint layer; REST remains authoritative.</p>'
        + section(t(lang, "shadow.title"), content)
    )
    return render_page(t(lang, "shadow.title"), body, lang, current_path)


def render_readiness(
    settings: Settings,
    *,
    keychain: KeychainStore | None,
    lang: str,
    current_path: str,
    query: dict[str, list[str]],
) -> str:
    checks = ResearchReadinessService(settings, keychain=keychain).check()
    overall = required_readiness_ok(checks)
    content = table(
        ["Check", "Required", "Status", "OK", "Detail"],
        [
            [
                check.name,
                check.required,
                check.status,
                check.ok,
                check.detail,
            ]
            for check in checks
        ],
        lang,
    )
    body = (
        flash(query, lang)
        + f"<h2>{e(t(lang, 'readiness.title'))}</h2>"
        + f'<p class="lede">{e(t(lang, "readiness.scope"))}</p>'
        + section(
            t(lang, "readiness.required_gates"),
            f'<p class="{"ok" if overall else "danger"}">{"PASS" if overall else "FAIL"}</p>',
        )
        + section(t(lang, "readiness.title"), content)
    )
    return render_page(t(lang, "readiness.title"), body, lang, current_path)


def _compact_json(value: object) -> str:
    if value in (None, ""):
        return "-"
    try:
        parsed = json.loads(str(value))
    except json.JSONDecodeError:
        return str(value)
    return json.dumps(parsed, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
