"""Main read-only research overview."""

from __future__ import annotations

from crypto_threshold.adapters.keychain import KeychainStore
from crypto_threshold.config import Settings
from crypto_threshold.dashboard.setup_flow import read_wallet_status
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


def render_overview(
    repository: Repository,
    settings: Settings,
    *,
    keychain: KeychainStore | None,
    lang: str,
    current_path: str,
    query: dict[str, list[str]],
) -> str:
    counts = repository.dashboard_counts()
    paper = repository.paper_summary()
    markets = repository.list_dashboard_markets(limit=8)
    cycles = repository.list_shadow_cycles(limit=5)
    readiness = ResearchReadinessService(settings, keychain=keychain).check()
    wallet = read_wallet_status(settings, keychain)
    body = [
        flash(query, lang),
        '<p class="eyebrow">BTC · ETH · SOL · XRP · Binance 1m Close</p>',
        f"<h2>{e(t(lang, 'overview.title'))}</h2>",
        f"<p class=\"lede\">{e(t(lang, 'overview.subtitle'))}</p>",
        section(
            t(lang, "app.no_go"),
            (
                f'<p><span class="badge danger">{e(t(lang, "app.classification"))}</span></p>'
                "<p>Public reads and local research writes only. "
                "No signer, order placement, cancellation, or account "
                "reconciliation is connected.</p>"
            ),
            css="no-go",
        ),
        _count_cards(counts, lang),
        section(
            t(lang, "overview.paper"),
            definition_table(
                [
                    ("total", paper["total"]),
                    ("open", paper["open_count"]),
                    ("settled", paper["settled_count"]),
                    ("skipped", paper["skipped_count"]),
                    ("settled PnL (USDC)", paper["settled_pnl_usdc"]),
                ]
            ),
        ),
        section(t(lang, "overview.top_markets"), _market_table(markets, lang)),
        section(t(lang, "overview.latest_shadow"), _shadow_table(cycles, lang)),
        section(
            t(lang, "readiness.title"),
            definition_table(
                [
                    (
                        t(lang, "readiness.required_gates"),
                        "PASS" if required_readiness_ok(readiness) else "FAIL",
                    ),
                    (
                        "wallet",
                        (
                            t(lang, "common.configured")
                            if wallet.private_key_configured and wallet.funder
                            else t(lang, "common.not_configured")
                        ),
                    ),
                    ("signer", wallet.signer_address),
                    ("TRADING_DISABLED", settings.TRADING_DISABLED),
                    ("User Channel", settings.POLYMARKET_STREAM_USER_CHANNEL_ENABLED),
                ]
            ),
        ),
    ]
    return render_page(t(lang, "app.title"), "".join(body), lang, current_path)


def _count_cards(counts: dict[str, int], lang: str) -> str:
    cards = "".join(
        f'<article class="card"><div class="stat">{value}</div>'
        f'<div class="muted">{e(name)}</div></article>'
        for name, value in counts.items()
    )
    return (
        f'<section><h3>{e(t(lang, "overview.counts"))}</h3>'
        f'<div class="grid">{cards}</div></section>'
    )


def _market_table(rows, lang: str) -> str:
    return table(
        ["Market", "Asset", "Strike", "Settlement", "Outcome", "Net EV", "Status"],
        [
            [
                link(f"/markets/{row['market_id']}", row["question"], lang),
                row["asset"],
                row["strike"],
                row["target_time_utc"],
                row["selected_outcome"],
                row["net_ev"],
                row["signal_status"] or row["rule_rejection_reason"],
            ]
            for row in rows
        ],
        lang,
    )


def _shadow_table(rows, lang: str) -> str:
    return table(
        ["Started", "Status", "Discovered", "Analyzed", "Paper enter", "Paper skip"],
        [
            [
                row["started_at"],
                row["status"],
                row["discovered_count"],
                row["analyzed_count"],
                row["paper_entered_count"],
                row["paper_skipped_count"],
            ]
            for row in rows
        ],
        lang,
    )
