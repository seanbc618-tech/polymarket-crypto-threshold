"""Local server, router, and composition root for the read-only dashboard."""

from __future__ import annotations

import logging
import re
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlencode, urlparse

from crypto_threshold.adapters.keychain import KeychainStore
from crypto_threshold.config import Settings
from crypto_threshold.dashboard.config_store import secret_keys_with_values
from crypto_threshold.dashboard.setup_flow import apply_wallet_setup
from crypto_threshold.dashboard_ui.app import render_overview
from crypto_threshold.dashboard_ui.html import render_page, single
from crypto_threshold.dashboard_ui.i18n import DEFAULT_LANG, SUPPORTED_LANGS, t
from crypto_threshold.dashboard_ui.markets import render_market_detail, render_markets
from crypto_threshold.dashboard_ui.research import (
    render_calibration,
    render_paper,
    render_readiness,
    render_shadow,
)
from crypto_threshold.dashboard_ui.setup import render_wallet_setup
from crypto_threshold.storage.db import Database
from crypto_threshold.storage.repositories import Repository

logger = logging.getLogger(__name__)
MAX_FORM_BYTES = 64 * 1024


class DashboardResponse:
    def __init__(
        self,
        status: HTTPStatus,
        body: str,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status = status
        self.body = body
        self.headers = headers or {}


class DashboardApp:
    """Own mutable local settings and route requests to pure page renderers."""

    def __init__(
        self,
        settings: Settings,
        *,
        keychain: KeychainStore | None,
        env_file: str | Path = ".env",
    ) -> None:
        if not settings.TRADING_DISABLED:
            raise ValueError("TRADING_DISABLED=false blocks the Phase 2 dashboard")
        if settings.POLYMARKET_STREAM_USER_CHANNEL_ENABLED:
            raise ValueError("User Channel must remain disabled in the Phase 2 dashboard")
        forbidden = secret_keys_with_values(env_file)
        if forbidden:
            raise ValueError(
                "private keys must be removed from the dashboard config file "
                "and stored in macOS Keychain"
            )
        self.wallet_setup_enabled = settings.DASHBOARD_PUBLIC_ORIGIN is None
        if settings.POLYMARKET_PRIVATE_KEY:
            raise ValueError(
                "POLYMARKET_PRIVATE_KEY must not be loaded into dashboard settings; "
                "store it only in macOS Keychain"
            )
        self._settings = settings
        self.keychain = keychain if self.wallet_setup_enabled else None
        self.env_file = Path(env_file)
        self._settings_lock = threading.RLock()

    @property
    def settings(self) -> Settings:
        with self._settings_lock:
            return self._settings

    def render(self, raw_path: str, cookie_header: str | None = None) -> DashboardResponse:
        parsed = urlparse(raw_path)
        query = parse_qs(parsed.query)
        lang = _request_lang(query, cookie_header)
        current_path = parsed.path + (f"?{parsed.query}" if parsed.query else "")
        settings = self.settings
        repository = Repository(Database(settings.DATABASE_PATH))

        if parsed.path == "/":
            body = render_overview(
                repository,
                settings,
                keychain=self.keychain,
                lang=lang,
                current_path=current_path,
                query=query,
            )
        elif parsed.path == "/markets":
            body = render_markets(
                repository,
                lang=lang,
                current_path=current_path,
                query=query,
            )
        elif parsed.path.startswith("/markets/"):
            market_id = unquote(parsed.path.removeprefix("/markets/"))
            market_detail = render_market_detail(
                repository,
                market_id,
                lang=lang,
                current_path=current_path,
                query=query,
            )
            if market_detail is None:
                return _not_found(lang, current_path)
            body = market_detail
        elif parsed.path == "/calibration":
            body = render_calibration(
                repository,
                lang=lang,
                current_path=current_path,
                query=query,
            )
        elif parsed.path == "/paper":
            body = render_paper(
                repository,
                lang=lang,
                current_path=current_path,
                query=query,
            )
        elif parsed.path == "/shadow":
            body = render_shadow(
                repository,
                lang=lang,
                current_path=current_path,
                query=query,
            )
        elif parsed.path == "/readiness":
            body = render_readiness(
                settings,
                keychain=self.keychain,
                lang=lang,
                current_path=current_path,
                query=query,
            )
        elif parsed.path == "/setup/wallet":
            if not self.wallet_setup_enabled:
                return _wallet_setup_forbidden(lang, current_path)
            body = render_wallet_setup(
                settings,
                keychain=self.keychain,
                lang=lang,
                current_path=current_path,
                query=query,
            )
        else:
            return _not_found(lang, current_path)
        return DashboardResponse(HTTPStatus.OK, body, _language_headers(query, lang))

    def handle_post(
        self,
        raw_path: str,
        body: bytes,
        cookie_header: str | None = None,
        *,
        host_header: str | None,
        origin_header: str | None,
    ) -> DashboardResponse:
        parsed = urlparse(raw_path)
        query = parse_qs(parsed.query)
        lang = _request_lang(query, cookie_header)
        try:
            if not self.wallet_setup_enabled:
                raise ValueError("wallet setup is disabled for public dashboard mode")
            form = _parse_form(body)
            lang = _form_lang(form, query, cookie_header)
            from crypto_threshold.dashboard.csrf import require_sensitive_post

            require_sensitive_post(
                form,
                host_header=host_header,
                origin_header=origin_header,
                allowed_public_origin=self.settings.DASHBOARD_PUBLIC_ORIGIN,
            )
            if parsed.path != "/setup/wallet":
                raise ValueError(t(lang, "error.invalid_post"))
            if self.keychain is None:
                raise ValueError("macOS Keychain is unavailable")
            updated = apply_wallet_setup(
                env_file=self.env_file,
                keychain=self.keychain,
                private_key=form.get("polymarket_private_key"),
                funder=form.get("polymarket_funder"),
                derive_funder=form.get("derive_funder") == "1",
                delete_private_key=form.get("delete_private_key") == "1",
            )
            with self._settings_lock:
                self._settings = updated
            return _redirect(
                "/setup/wallet",
                lang,
                "flash.wallet_saved",
                headers=_language_headers({"lang": [lang]}, lang),
            )
        except Exception as exc:  # noqa: BLE001 - bounded, redacted request failure
            logger.warning("dashboard POST rejected: %s", type(exc).__name__)
            return _redirect(
                "/setup/wallet" if parsed.path == "/setup/wallet" else "/",
                lang,
                "flash.error",
                level="error",
                detail=_redacted_detail(exc),
                headers=_language_headers({"lang": [lang]}, lang),
            )


def serve_dashboard(
    settings: Settings,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    env_file: str | Path = ".env",
    keychain: KeychainStore | None = None,
) -> None:
    if host not in {"127.0.0.1", "localhost", "::1"} and not settings.DASHBOARD_PUBLIC_ORIGIN:
        raise ValueError(
            "non-local dashboard binding requires DASHBOARD_PUBLIC_ORIGIN "
            "and an external access boundary"
        )
    app = DashboardApp(
        settings,
        keychain=keychain if keychain is not None else KeychainStore(),
        env_file=env_file,
    )
    database = Database(settings.DATABASE_PATH)
    database.initialize()
    server = create_dashboard_server(app, host=host, port=port)
    print(f"Crypto Threshold dashboard listening at http://{host}:{server.server_port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def create_dashboard_server(
    app: DashboardApp,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
) -> ThreadingHTTPServer:
    """Build a server without starting it, for tests and controlled launchers."""

    def handler_factory(*args, **kwargs):
        return DashboardHandler(app, *args, **kwargs)

    return ThreadingHTTPServer((host, port), handler_factory)


class DashboardHandler(BaseHTTPRequestHandler):
    def __init__(self, app: DashboardApp, *args, **kwargs) -> None:
        self.app = app
        super().__init__(*args, **kwargs)

    def do_GET(self) -> None:
        if urlparse(self.path).path == "/favicon.ico":
            self._send(
                DashboardResponse(
                    HTTPStatus.NO_CONTENT,
                    "",
                    {"Content-Type": "image/x-icon"},
                )
            )
            return
        try:
            response = self.app.render(self.path, self.headers.get("Cookie"))
        except Exception:  # noqa: BLE001 - return a bounded failure page
            logger.exception("dashboard GET failed")
            response = DashboardResponse(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "Dashboard temporarily unavailable.",
                {"Content-Type": "text/plain; charset=utf-8"},
            )
        self._send(response)

    def do_POST(self) -> None:
        try:
            length = int(self.headers.get("Content-Length", "0") or "0")
        except ValueError:
            length = MAX_FORM_BYTES + 1
        if length < 0:
            self._send(
                DashboardResponse(
                    HTTPStatus.BAD_REQUEST,
                    "Invalid Content-Length.",
                    {"Content-Type": "text/plain; charset=utf-8"},
                )
            )
            return
        if length > MAX_FORM_BYTES:
            self._send(
                DashboardResponse(
                    HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                    "Request body too large.",
                    {"Content-Type": "text/plain; charset=utf-8"},
                )
            )
            return
        body = self.rfile.read(length) if length else b""
        response = self.app.handle_post(
            self.path,
            body,
            self.headers.get("Cookie"),
            host_header=self.headers.get("Host"),
            origin_header=self.headers.get("Origin"),
        )
        self._send(response)

    def _send(self, response: DashboardResponse) -> None:
        encoded = response.body.encode("utf-8")
        self.send_response(response.status.value)
        headers = {
            "Content-Type": "text/html; charset=utf-8",
            "Cache-Control": "no-store, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
            "Content-Security-Policy": (
                "default-src 'self'; style-src 'unsafe-inline'; "
                "img-src 'self' data:; form-action 'self'; frame-ancestors 'none'"
            ),
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
            **response.headers,
        }
        for key, value in headers.items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        try:
            self.wfile.write(encoded)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            logger.debug("dashboard client disconnected before response completed")

    def log_message(self, format: str, *args) -> None:
        return


def _parse_form(body: bytes) -> dict[str, str]:
    if len(body) > MAX_FORM_BYTES:
        raise ValueError("request body too large")
    decoded = body.decode("utf-8", errors="strict")
    parsed = parse_qs(decoded, keep_blank_values=True, strict_parsing=False)
    return {key: values[-1] for key, values in parsed.items() if values}


def _request_lang(query: dict[str, list[str]], cookie_header: str | None) -> str:
    explicit = single(query, "lang")
    if explicit in SUPPORTED_LANGS:
        return explicit
    if cookie_header:
        for part in cookie_header.split(";"):
            key, _, value = part.strip().partition("=")
            if key == "crypto_threshold_lang" and value in SUPPORTED_LANGS:
                return value
    return DEFAULT_LANG


def _form_lang(
    form: dict[str, str],
    query: dict[str, list[str]],
    cookie_header: str | None,
) -> str:
    value = form.get("lang") or single(query, "lang")
    return value if value in SUPPORTED_LANGS else _request_lang(query, cookie_header)


def _language_headers(query: dict[str, list[str]], lang: str) -> dict[str, str]:
    if single(query, "lang") in SUPPORTED_LANGS:
        return {
            "Set-Cookie": (
                f"crypto_threshold_lang={lang}; Path=/; SameSite=Lax; "
                "HttpOnly"
            )
        }
    return {}


def _redirect(
    path: str,
    lang: str,
    flash_key: str,
    *,
    level: str = "ok",
    detail: str | None = None,
    headers: dict[str, str] | None = None,
) -> DashboardResponse:
    query: dict[str, str] = {"lang": lang, "flash": flash_key, "level": level}
    if detail:
        query["detail"] = detail
    return DashboardResponse(
        HTTPStatus.SEE_OTHER,
        "",
        {**(headers or {}), "Location": f"{path}?{urlencode(query)}"},
    )


def _not_found(lang: str, current_path: str) -> DashboardResponse:
    body = render_page(
        t(lang, "error.not_found"),
        f"<h2>{t(lang, 'error.not_found')}</h2><p>{t(lang, 'error.unknown_page')}</p>",
        lang,
        current_path,
    )
    return DashboardResponse(HTTPStatus.NOT_FOUND, body)


def _wallet_setup_forbidden(lang: str, current_path: str) -> DashboardResponse:
    body = render_page(
        t(lang, "wallet.title"),
        f"<h2>{t(lang, 'wallet.title')}</h2>"
        f"<p>{t(lang, 'wallet.public_disabled')}</p>",
        lang,
        current_path,
    )
    return DashboardResponse(HTTPStatus.FORBIDDEN, body)


def _redacted_detail(exc: BaseException) -> str:
    text = re.sub(
        r"(?i)(?:0x)?[0-9a-f]{64}",
        "[redacted]",
        str(exc),
    )
    lowered = text.lower()
    if any(token in lowered for token in ("private_key", "password", "secret", "bearer ")):
        return f"{type(exc).__name__}: [redacted]"
    return text[:240]
