"""Wallet, Keychain, CSRF, and secret-hygiene tests."""

from __future__ import annotations

import subprocess
from pathlib import Path
from urllib.parse import urlencode

import pytest

import crypto_threshold.adapters.keychain as keychain_adapter
from crypto_threshold.adapters.keychain import KeychainError, KeychainStore
from crypto_threshold.config import Settings, load_settings
from crypto_threshold.dashboard.config_store import ConfigStoreError, update_env_file
from crypto_threshold.dashboard.csrf import csrf_token, require_sensitive_post
from crypto_threshold.dashboard.server import DashboardApp
from crypto_threshold.dashboard.setup_flow import apply_wallet_setup
from crypto_threshold.storage.db import Database


class FakeKeychain(KeychainStore):
    def __init__(self) -> None:
        super().__init__(service="test")
        self.data: dict[str, str] = {}

    def get_secret(self, account: str) -> str | None:
        return self.data.get(account)

    def set_secret(self, account: str, value: str) -> None:
        self.data[account] = value

    def delete_secret(self, account: str, *, missing_ok: bool = False) -> bool:
        return self.data.pop(account, None) is not None


def _settings(tmp_path: Path) -> Settings:
    database_path = tmp_path / "dashboard.db"
    Database(database_path).initialize()
    return Settings(_env_file=None, DATABASE_PATH=str(database_path), TRADING_DISABLED=True)


def _form(**values: str) -> bytes:
    return urlencode({"csrf_token": csrf_token(), "lang": "en", **values}).encode()


def test_wallet_post_stores_secret_only_in_keychain_and_renders_only_address(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# keep this comment\nDATABASE_PATH=" + settings.DATABASE_PATH + "\n",
        encoding="utf-8",
    )
    keychain = FakeKeychain()
    app = DashboardApp(settings, keychain=keychain, env_file=env_file)
    secret = "0x" + "11" * 32

    response = app.handle_post(
        "/setup/wallet",
        _form(polymarket_private_key=secret, derive_funder="1"),
        host_header="127.0.0.1:8765",
        origin_header="http://127.0.0.1:8765",
    )

    assert response.status.value == 303
    assert keychain.get_secret("POLYMARKET_PRIVATE_KEY") == secret
    persisted = env_file.read_text(encoding="utf-8")
    assert "# keep this comment" in persisted
    assert "POLYMARKET_PRIVATE_KEY" not in persisted
    assert secret not in persisted
    assert "TRADING_DISABLED=true" in persisted
    assert "POLYMARKET_FUNDER=0x19E7E376E7C213B7E7e7e46cc70A5dD086DAff2A" in persisted
    assert env_file.stat().st_mode & 0o777 == 0o600

    page = app.render("/setup/wallet?lang=en")
    assert page.status.value == 200
    assert secret not in page.body
    assert "0x19E7E376E7C213B7E7e7e46cc70A5dD086DAff2A" in page.body
    assert "Configured" in page.body
    assert "SecureClient" in page.body


def test_wallet_post_requires_csrf_local_host_and_origin(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    keychain = FakeKeychain()
    app = DashboardApp(settings, keychain=keychain, env_file=tmp_path / ".env")
    secret = "0x" + "22" * 32

    bad_token = app.handle_post(
        "/setup/wallet",
        urlencode({"polymarket_private_key": secret}).encode(),
        host_header="127.0.0.1:8765",
        origin_header="http://127.0.0.1:8765",
    )
    bad_origin = app.handle_post(
        "/setup/wallet",
        _form(polymarket_private_key=secret),
        host_header="127.0.0.1:8765",
        origin_header="https://evil.example",
    )

    assert bad_token.status.value == 303
    assert bad_origin.status.value == 303
    assert "level=error" in bad_token.headers["Location"]
    assert "level=error" in bad_origin.headers["Location"]
    assert keychain.data == {}


def test_wallet_post_redacts_secret_even_if_dependency_error_echoes_it(
    tmp_path: Path,
) -> None:
    class LeakyFailureKeychain(FakeKeychain):
        def set_secret(self, account: str, value: str) -> None:
            raise KeychainError(f"dependency echoed {value}")

    secret = "0x" + "66" * 32
    app = DashboardApp(
        _settings(tmp_path),
        keychain=LeakyFailureKeychain(),
        env_file=tmp_path / ".env",
    )
    response = app.handle_post(
        "/setup/wallet",
        _form(polymarket_private_key=secret),
        host_header="127.0.0.1:8765",
        origin_header="http://127.0.0.1:8765",
    )

    assert response.status.value == 303
    assert secret not in response.headers["Location"]
    assert "redacted" in response.headers["Location"]


def test_unknown_post_is_rejected_without_mutation(tmp_path: Path) -> None:
    app = DashboardApp(
        _settings(tmp_path),
        keychain=FakeKeychain(),
        env_file=tmp_path / ".env",
    )
    response = app.handle_post(
        "/markets/analyze",
        _form(),
        host_header="localhost:8765",
        origin_header="http://localhost:8765",
    )
    assert response.status.value == 303
    assert response.headers["Location"].startswith("/?")
    assert not (tmp_path / ".env").exists()


def test_config_store_refuses_private_key_and_preserves_original(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("TRADING_DISABLED=true\n", encoding="utf-8")
    with pytest.raises(ConfigStoreError, match="secret"):
        update_env_file(env_file, {"POLYMARKET_PRIVATE_KEY": "do-not-write"})
    assert env_file.read_text(encoding="utf-8") == "TRADING_DISABLED=true\n"


def test_keychain_rejects_unknown_accounts_and_empty_secrets() -> None:
    store = KeychainStore(service="test")
    with pytest.raises(KeychainError, match="unsupported"):
        store.get_secret("SECOND_PRIVATE_KEY")
    with pytest.raises(KeychainError, match="empty"):
        store.set_secret("POLYMARKET_PRIVATE_KEY", "")


def test_keychain_secret_is_sent_over_stdin_not_process_arguments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    calls = 0

    def fake_run_security(
        argv: list[str],
        *,
        input_text: str | None = None,
        timeout: float = 15.0,
    ) -> subprocess.CompletedProcess[str]:
        nonlocal calls
        calls += 1
        captured.update(argv=argv, input_text=input_text, timeout=timeout)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(keychain_adapter, "_run_security", fake_run_security)
    secret = "0x" + "77" * 32
    KeychainStore(service="test").set_secret("POLYMARKET_PRIVATE_KEY", secret)

    argv = captured["argv"]
    assert isinstance(argv, list)
    assert argv[-1] == "-w"
    assert secret not in argv
    assert captured["input_text"] == f"{secret}\n"
    assert calls == 1


def test_dashboard_refuses_unsafe_runtime(tmp_path: Path) -> None:
    database_path = tmp_path / "unsafe.db"
    Database(database_path).initialize()
    with pytest.raises(ValueError, match="TRADING_DISABLED"):
        DashboardApp(
            Settings(
                _env_file=None,
                DATABASE_PATH=str(database_path),
                TRADING_DISABLED=False,
            ),
            keychain=FakeKeychain(),
            env_file=tmp_path / ".env",
        )
    with pytest.raises(ValueError, match="User Channel"):
        DashboardApp(
            Settings(
                _env_file=None,
                DATABASE_PATH=str(database_path),
                POLYMARKET_STREAM_USER_CHANNEL_ENABLED=True,
            ),
            keychain=FakeKeychain(),
            env_file=tmp_path / ".env",
        )


def test_dashboard_refuses_private_key_in_normal_config(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "POLYMARKET_PRIVATE_KEY=forbidden\nTRADING_DISABLED=true\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="macOS Keychain"):
        DashboardApp(settings, keychain=FakeKeychain(), env_file=env_file)


def test_public_origin_validation_is_exact() -> None:
    form = {"csrf_token": csrf_token()}
    require_sensitive_post(
        form,
        host_header="crypto.example.com",
        origin_header="https://crypto.example.com",
        allowed_public_origin="https://crypto.example.com",
    )
    with pytest.raises(ValueError, match="Origin"):
        require_sensitive_post(
            form,
            host_header="crypto.example.com",
            origin_header="https://sub.crypto.example.com",
            allowed_public_origin="https://crypto.example.com",
        )


def test_public_dashboard_disables_wallet_reads_and_writes(tmp_path: Path) -> None:
    database_path = tmp_path / "public.db"
    Database(database_path).initialize()
    keychain = FakeKeychain()
    secret = "0x" + "33" * 32
    keychain.set_secret("POLYMARKET_PRIVATE_KEY", secret)
    app = DashboardApp(
        Settings(
            _env_file=None,
            DATABASE_PATH=str(database_path),
            DASHBOARD_PUBLIC_ORIGIN="https://crypto.example.com",
        ),
        keychain=keychain,
        env_file=tmp_path / ".env",
    )

    page = app.render("/setup/wallet?lang=en")
    overview = app.render("/?lang=en")
    rejected = app.handle_post(
        "/setup/wallet",
        _form(polymarket_private_key=secret, derive_funder="1"),
        host_header="crypto.example.com",
        origin_header="https://crypto.example.com",
    )

    assert page.status.value == 403
    assert 'method="post"' not in page.body.lower()
    assert "loopback-only" in page.body
    assert "Not configured" in overview.body
    assert secret not in overview.body
    assert rejected.status.value == 303
    assert "level=error" in rejected.headers["Location"]
    assert keychain.get_secret("POLYMARKET_PRIVATE_KEY") == secret


def test_wallet_setup_restores_previous_keychain_secret_on_config_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    keychain = FakeKeychain()
    previous = "0x" + "44" * 32
    replacement = "0x" + "55" * 32
    keychain.set_secret("POLYMARKET_PRIVATE_KEY", previous)

    def fail_update(*args, **kwargs) -> None:
        raise ConfigStoreError("simulated persistence failure")

    monkeypatch.setattr(
        "crypto_threshold.dashboard.setup_flow.update_env_file",
        fail_update,
    )
    with pytest.raises(ConfigStoreError, match="simulated"):
        apply_wallet_setup(
            env_file=tmp_path / ".env",
            keychain=keychain,
            private_key=replacement,
            funder=None,
            derive_funder=True,
            delete_private_key=False,
        )

    assert keychain.get_secret("POLYMARKET_PRIVATE_KEY") == previous


def test_wallet_setup_restores_config_when_reload_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    keychain = FakeKeychain()
    previous = "0x" + "88" * 32
    replacement = "0x" + "99" * 32
    keychain.set_secret("POLYMARKET_PRIVATE_KEY", previous)
    env_file = tmp_path / ".env"
    original = b"# original\nTRADING_DISABLED=true\n"
    env_file.write_bytes(original)
    env_file.chmod(0o640)

    def fail_reload(*args, **kwargs) -> Settings:
        raise ValueError("simulated reload failure")

    monkeypatch.setattr(
        "crypto_threshold.dashboard.setup_flow.load_settings",
        fail_reload,
    )
    with pytest.raises(ValueError, match="simulated reload"):
        apply_wallet_setup(
            env_file=env_file,
            keychain=keychain,
            private_key=replacement,
            funder=None,
            derive_funder=True,
            delete_private_key=False,
        )

    assert keychain.get_secret("POLYMARKET_PRIVATE_KEY") == previous
    assert env_file.read_bytes() == original
    assert env_file.stat().st_mode & 0o777 == 0o640


def test_dashboard_settings_reject_private_key_from_process_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("POLYMARKET_PRIVATE_KEY", "0x" + "aa" * 32)
    with pytest.raises(ValueError, match="process environment"):
        load_settings(
            env_file=tmp_path / ".env",
            reject_environment_secrets=True,
        )
