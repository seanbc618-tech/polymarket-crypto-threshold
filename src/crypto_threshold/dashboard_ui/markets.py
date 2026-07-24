"""BTC/ETH market list and evidence detail pages."""

from __future__ import annotations

import json
from urllib.parse import quote

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
from crypto_threshold.storage.repositories import Repository


def render_markets(
    repository: Repository,
    *,
    lang: str,
    current_path: str,
    query: dict[str, list[str]],
) -> str:
    rows = repository.list_dashboard_markets(limit=200)
    content = table(
        [
            "Market",
            "Asset",
            "Strike",
            "Settlement UTC",
            "YES bid/ask",
            "NO bid/ask",
            "YES/NO ask VWAP",
            "Selected",
            "Net EV",
            "Status",
        ],
        [
            [
                link(f"/markets/{quote(str(row['market_id']), safe='')}", row["question"], lang),
                row["asset"],
                row["strike"],
                row["target_time_utc"],
                _pair(row["yes_best_bid"], row["yes_best_ask"]),
                _pair(row["no_best_bid"], row["no_best_ask"]),
                _pair(row["yes_ask_vwap"], row["no_ask_vwap"]),
                row["selected_outcome"],
                row["net_ev"],
                row["signal_status"] or row["rule_rejection_reason"],
            ]
            for row in rows
        ],
        lang,
    )
    body = (
        flash(query, lang)
        + '<p class="eyebrow">Gamma · CLOB · executable ask depth</p>'
        + f"<h2>{e(t(lang, 'markets.title'))}</h2>"
        + section(t(lang, "markets.title"), content)
    )
    return render_page(t(lang, "markets.title"), body, lang, current_path)


def render_market_detail(
    repository: Repository,
    market_id: str,
    *,
    lang: str,
    current_path: str,
    query: dict[str, list[str]],
) -> str | None:
    market = repository.get_dashboard_market(market_id)
    if market is None:
        return None
    snapshots = repository.list_market_snapshot_history(market_id, limit=20)
    signals = repository.list_market_signal_history(market_id, limit=20)
    prices = repository.list_market_price_history(market_id, limit=20)
    body = [
        flash(query, lang),
        f'<p class="eyebrow">{e(market["asset"] or "unsupported")} · {e(market_id)}</p>',
        f"<h2>{e(market['question'])}</h2>",
        section(
            "Contract",
            definition_table(
                [
                    ("event_id", market["event_id"]),
                    ("condition_id", market["condition_id"]),
                    ("pair", market["pair"]),
                    ("settlement source", market["settlement_source"]),
                    ("operator", market["exact_operator"]),
                    ("strike", market["strike"]),
                    ("candle", market["candle_interval"]),
                    ("field", market["price_field"]),
                    ("target UTC", market["target_time_utc"]),
                    ("tradable parser result", market["tradable"]),
                    ("preview only", market["preview_only"]),
                    ("rule rejection", market["rule_rejection_reason"]),
                ]
            ),
        ),
        section(
            "Latest executable analysis",
            definition_table(
                [
                    ("signal", market["signal_id"]),
                    ("estimated P(YES)", market["estimated_probability"]),
                    (
                        "probability interval",
                        _pair(market["probability_low"], market["probability_high"]),
                    ),
                    ("YES ask VWAP", market["yes_ask_vwap"]),
                    ("NO ask VWAP", market["no_ask_vwap"]),
                    ("YES net EV", market["yes_net_ev"]),
                    ("NO net EV", market["no_net_ev"]),
                    ("selected outcome", market["selected_outcome"]),
                    ("selected net EV", market["net_ev"]),
                    ("status", market["signal_status"]),
                    ("reasons", _json_summary(market["signal_reasons"])),
                    ("observed", market["signal_observed_at"]),
                ]
            ),
        ),
        section(
            "YES / NO order-book history",
            table(
                ["Observed", "Outcome", "Bid", "Ask", "Mid", "Spread", "Bid depth", "Ask depth"],
                [
                    [
                        row["observed_at"],
                        row["outcome"],
                        row["best_bid"],
                        row["best_ask"],
                        row["midpoint"],
                        row["spread"],
                        row["bid_depth"],
                        row["ask_depth"],
                    ]
                    for row in snapshots
                ],
                lang,
            ),
        ),
        section(
            "Reference price history",
            table(
                ["Observed", "Provider", "Symbol", "Kind", "Price", "Source version"],
                [
                    [
                        row["observed_at"],
                        row["provider"],
                        row["symbol"],
                        row["price_kind"],
                        row["price"],
                        row["source_version"],
                    ]
                    for row in prices
                ],
                lang,
            ),
        ),
        section(
            "Signal history",
            table(
                ["Observed", "Status", "P(YES)", "Selected", "Net EV", "Reasons"],
                [
                    [
                        row["observed_at"],
                        row["status"],
                        row["estimated_probability"],
                        row["selected_outcome"],
                        row["net_ev"],
                        _json_summary(row["reasons"]),
                    ]
                    for row in signals
                ],
                lang,
            ),
        ),
    ]
    return render_page(t(lang, "markets.detail"), "".join(body), lang, current_path)


def _pair(left: object, right: object) -> str:
    left_value = left if left not in (None, "") else "-"
    right_value = right if right not in (None, "") else "-"
    return f"{left_value} / {right_value}"


def _json_summary(value: object) -> str:
    if value in (None, ""):
        return "-"
    try:
        parsed = json.loads(str(value))
    except json.JSONDecodeError:
        return str(value)
    if isinstance(parsed, list):
        return "; ".join(str(item) for item in parsed) or "-"
    if isinstance(parsed, dict):
        return "; ".join(f"{key}={item}" for key, item in sorted(parsed.items())) or "-"
    return str(parsed)
