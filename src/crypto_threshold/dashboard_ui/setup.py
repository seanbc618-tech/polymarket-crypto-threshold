"""Wallet status and local Keychain configuration page."""

from __future__ import annotations

from crypto_threshold.adapters.keychain import KeychainStore
from crypto_threshold.config import Settings
from crypto_threshold.dashboard.setup_flow import read_wallet_status
from crypto_threshold.dashboard_ui.html import (
    definition_table,
    e,
    flash,
    href,
    render_page,
    section,
)
from crypto_threshold.dashboard_ui.i18n import t


def render_wallet_setup(
    settings: Settings,
    *,
    keychain: KeychainStore | None,
    lang: str,
    current_path: str,
    query: dict[str, list[str]],
) -> str:
    status = read_wallet_status(settings, keychain)
    configured = (
        t(lang, "common.configured")
        if status.private_key_configured
        else t(lang, "common.not_configured")
    )
    form = (
        f'<form method="post" action="{e(href("/setup/wallet", lang))}" autocomplete="off">'
        + definition_table(
            [
                (t(lang, "wallet.private_status"), configured),
                (t(lang, "wallet.funder"), status.funder),
                (t(lang, "wallet.signer"), status.signer_address),
                (
                    t(lang, "wallet.keychain"),
                    "available" if status.keychain_available else status.detail,
                ),
            ]
        )
        + f'<label>{e(t(lang, "wallet.private_key"))}'
        + f'<span class="muted">{e(t(lang, "wallet.blank_keep"))}</span>'
        + '<input type="password" name="polymarket_private_key" '
        + 'autocomplete="new-password" spellcheck="false" placeholder="••••••••"></label>'
        + f'<label>{e(t(lang, "wallet.funder"))}'
        + f'<input name="polymarket_funder" value="{e(status.funder or "")}" '
        + 'autocomplete="off" spellcheck="false" placeholder="0x…"></label>'
        + '<label class="check"><input type="checkbox" name="derive_funder" value="1">'
        + f'{e(t(lang, "wallet.derive"))}</label>'
        + '<label class="check danger"><input type="checkbox" name="delete_private_key" value="1">'
        + f'{e(t(lang, "wallet.delete"))}</label>'
        + f'<p class="warning">{e(t(lang, "wallet.safety"))}</p>'
        + f'<button type="submit">{e(t(lang, "common.save"))}</button>'
        + "</form>"
    )
    body = (
        flash(query, lang)
        + f"<h2>{e(t(lang, 'wallet.title'))}</h2>"
        + f'<p class="lede">{e(t(lang, "wallet.help"))}</p>'
        + section(t(lang, "wallet.title"), form)
    )
    return render_page(t(lang, "wallet.title"), body, lang, current_path)
