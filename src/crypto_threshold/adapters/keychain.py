"""Narrow macOS Keychain adapter for dashboard-managed secrets."""

from __future__ import annotations

import logging
import platform
import subprocess
from collections.abc import Sequence

logger = logging.getLogger(__name__)

DEFAULT_SERVICE = "com.seanbc.polymarket-crypto-threshold"
SECURITY_BIN = "/usr/bin/security"
SECRET_ENV_KEYS: tuple[str, ...] = ("POLYMARKET_PRIVATE_KEY",)


class KeychainError(RuntimeError):
    """Raised when macOS Keychain is unavailable or rejects an operation."""


def is_keychain_available() -> bool:
    return platform.system() == "Darwin"


def _run_security(
    argv: list[str],
    *,
    input_text: str | None = None,
    timeout: float = 15.0,
) -> subprocess.CompletedProcess[str]:
    if not is_keychain_available():
        raise KeychainError("macOS Keychain is only available on Darwin")
    try:
        return subprocess.run(
            argv,
            check=False,
            capture_output=True,
            text=True,
            input=input_text,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise KeychainError("Keychain operation timed out") from exc
    except OSError as exc:
        raise KeychainError(f"Keychain subprocess failed: {exc}") from exc


class KeychainStore:
    """Get, replace, and delete named secrets under one application service."""

    def __init__(
        self,
        *,
        service: str = DEFAULT_SERVICE,
        security_bin: str = SECURITY_BIN,
    ) -> None:
        self.service = service
        self.security_bin = security_bin

    def get_secret(self, account: str) -> str | None:
        self._require_allowed_account(account)
        result = _run_security(
            [
                self.security_bin,
                "find-generic-password",
                "-s",
                self.service,
                "-a",
                account,
                "-w",
            ]
        )
        if result.returncode != 0:
            stderr = (result.stderr or "").lower()
            if "could not be found" in stderr or result.returncode == 44:
                return None
            raise KeychainError(
                f"Keychain get failed for {account}: {_safe_error(result)}"
            )
        value = (result.stdout or "").rstrip("\n")
        return value or None

    def set_secret(self, account: str, value: str) -> None:
        self._require_allowed_account(account)
        if not value:
            raise KeychainError("refusing to store empty secret")
        result = _run_security(
            [
                self.security_bin,
                "add-generic-password",
                "-U",
                "-s",
                self.service,
                "-a",
                account,
                "-w",
            ],
            input_text=f"{value}\n",
        )
        if result.returncode != 0:
            raise KeychainError(
                f"Keychain set failed for {account}: {_safe_error(result)}"
            )

    def delete_secret(self, account: str, *, missing_ok: bool = False) -> bool:
        self._require_allowed_account(account)
        result = _run_security(
            [
                self.security_bin,
                "delete-generic-password",
                "-s",
                self.service,
                "-a",
                account,
            ]
        )
        if result.returncode == 0:
            return True
        stderr = (result.stderr or "").lower()
        if missing_ok or "could not be found" in stderr or result.returncode == 44:
            return False
        raise KeychainError(
            f"Keychain delete failed for {account}: {_safe_error(result)}"
        )

    def get_all_secrets(self, accounts: Sequence[str] | None = None) -> dict[str, str]:
        keys = tuple(accounts) if accounts is not None else SECRET_ENV_KEYS
        secrets: dict[str, str] = {}
        for account in keys:
            try:
                value = self.get_secret(account)
            except KeychainError:
                logger.warning("keychain unavailable for %s", account)
                continue
            if value:
                secrets[account] = value
        return secrets

    def secret_status(self, accounts: Sequence[str] | None = None) -> dict[str, bool]:
        keys = tuple(accounts) if accounts is not None else SECRET_ENV_KEYS
        status: dict[str, bool] = {}
        for account in keys:
            try:
                status[account] = self.get_secret(account) is not None
            except KeychainError:
                status[account] = False
        return status

    @staticmethod
    def _require_allowed_account(account: str) -> None:
        if account not in SECRET_ENV_KEYS:
            raise KeychainError(f"unsupported Keychain account: {account}")


def _safe_error(result: subprocess.CompletedProcess[str]) -> str:
    """Never reflect ``security(1)`` output into logs, HTML, or exceptions."""

    return f"security exit {result.returncode}"
