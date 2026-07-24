"""Local wallet configuration without authenticated SDK or exchange access."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from eth_account import Account

from crypto_threshold.adapters.keychain import KeychainError, KeychainStore
from crypto_threshold.config import Settings, load_settings
from crypto_threshold.dashboard.config_store import (
    ConfigStoreError,
    restore_config_file,
    secret_keys_with_values,
    snapshot_config_file,
    update_env_file,
)

PRIVATE_KEY_ACCOUNT = "POLYMARKET_PRIVATE_KEY"
_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")


@dataclass(frozen=True)
class WalletStatus:
    private_key_configured: bool
    funder: str | None
    signer_address: str | None
    keychain_available: bool
    detail: str


def derive_signer_address(private_key: str) -> str:
    key = private_key.strip()
    if not key.startswith("0x"):
        key = "0x" + key
    return str(Account.from_key(key).address)


def read_wallet_status(settings: Settings, keychain: KeychainStore | None) -> WalletStatus:
    secret: str | None = None
    if keychain is None:
        available = False
        detail = "Keychain is not configured for this dashboard process"
    else:
        try:
            secret = keychain.get_secret(PRIVATE_KEY_ACCOUNT)
            available = True
            detail = "private key status read from macOS Keychain"
        except KeychainError as exc:
            available = False
            detail = str(exc)
    signer_address: str | None = None
    if secret:
        try:
            signer_address = derive_signer_address(secret)
        except Exception:  # noqa: BLE001 - status must not reflect secret/parser details
            detail = "private key is configured but its signer address could not be derived"
    return WalletStatus(
        private_key_configured=bool(secret),
        funder=settings.POLYMARKET_FUNDER,
        signer_address=signer_address,
        keychain_available=available,
        detail=detail,
    )


def apply_wallet_setup(
    *,
    env_file: str | Path,
    keychain: KeychainStore,
    private_key: str | None,
    funder: str | None,
    derive_funder: bool,
    delete_private_key: bool,
) -> Settings:
    """Persist one Keychain secret and non-sensitive wallet settings.

    The operation never imports or constructs ``SecureClient`` and always pins
    ``TRADING_DISABLED=true`` in the dashboard-managed config.
    """

    normalized_key = (private_key or "").strip()
    normalized_funder = (funder or "").strip() or None
    if delete_private_key and normalized_key:
        raise ValueError("cannot replace and delete the private key in one request")
    signer_address: str | None = None
    if normalized_key:
        try:
            signer_address = derive_signer_address(normalized_key)
        except Exception as exc:  # noqa: BLE001
            raise ValueError("invalid private key") from exc
    if derive_funder or (normalized_key and normalized_funder is None):
        if signer_address is None:
            raise ValueError("a new private key is required to derive the funder")
        normalized_funder = signer_address
    if normalized_funder is not None and not _ADDRESS_RE.fullmatch(normalized_funder):
        raise ValueError("POLYMARKET_FUNDER must be a 0x-prefixed 20-byte address")

    previous_secret = keychain.get_secret(PRIVATE_KEY_ACCOUNT)
    previous_config = snapshot_config_file(env_file)
    keychain_mutation_attempted = False
    try:
        update_env_file(
            env_file,
            {
                "POLYMARKET_FUNDER": normalized_funder,
                "TRADING_DISABLED": "true",
            },
        )
        if secret_keys_with_values(env_file):
            raise ValueError(
                "private keys must be removed from the dashboard config file "
                "and stored in macOS Keychain"
            )
        settings = load_settings(
            env_file=env_file,
            reject_environment_secrets=True,
        )
        if not settings.TRADING_DISABLED:
            raise ValueError(
                "TRADING_DISABLED is overridden outside the dashboard; refusing unsafe reload"
            )
        keychain_mutation_attempted = delete_private_key or bool(normalized_key)
        if delete_private_key:
            keychain.delete_secret(PRIVATE_KEY_ACCOUNT, missing_ok=True)
        elif normalized_key:
            keychain.set_secret(PRIVATE_KEY_ACCOUNT, normalized_key)
        return settings
    except Exception as exc:
        rollback_failed = False
        if keychain_mutation_attempted:
            try:
                _restore_secret(keychain, previous_secret)
            except KeychainError:
                rollback_failed = True
        try:
            restore_config_file(env_file, previous_config)
        except ConfigStoreError:
            rollback_failed = True
        if rollback_failed:
            raise RuntimeError(
                "wallet setup failed and local rollback was incomplete"
            ) from exc
        raise


def _restore_secret(keychain: KeychainStore, previous_secret: str | None) -> None:
    if previous_secret:
        keychain.set_secret(PRIVATE_KEY_ACCOUNT, previous_secret)
    else:
        keychain.delete_secret(PRIVATE_KEY_ACCOUNT, missing_ok=True)
