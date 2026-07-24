"""Per-process CSRF and exact-origin checks for dashboard POST requests."""

from __future__ import annotations

import hmac
import re
import secrets
from collections.abc import Mapping
from html import escape
from urllib.parse import urlsplit

_PROCESS_TOKEN = secrets.token_urlsafe(32)
CSRF_FIELD = "csrf_token"
LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "[::1]"})
_POST_FORM_RE = re.compile(
    r"(<form\b(?=[^>]*\bmethod\s*=\s*([\"'])post\2)[^>]*>)",
    re.IGNORECASE,
)


def csrf_token() -> str:
    return _PROCESS_TOKEN


def verify_csrf(form_token: str | None) -> bool:
    return bool(form_token) and hmac.compare_digest(str(form_token), _PROCESS_TOKEN)


def require_sensitive_post(
    form: Mapping[str, str],
    *,
    host_header: str | None,
    origin_header: str | None,
    allowed_public_origin: str | None = None,
) -> None:
    if not verify_csrf(form.get(CSRF_FIELD)):
        raise ValueError("invalid or missing CSRF token")
    if allowed_public_origin:
        expected = _canonical_origin(allowed_public_origin)
        if expected is None:
            raise ValueError("invalid configured public dashboard origin")
        expected_parts = urlsplit(expected)
        if _canonical_host(host_header, scheme=expected_parts.scheme) != _canonical_host(
            expected_parts.netloc, scheme=expected_parts.scheme
        ):
            raise ValueError("refusing unexpected Host header for sensitive action")
        if _canonical_origin(origin_header) != expected:
            raise ValueError("refusing missing or unexpected Origin for sensitive action")
        return
    if not _is_local_host(host_header):
        raise ValueError("refusing missing or non-local Host header for sensitive action")
    if origin_header:
        origin = _canonical_origin(origin_header)
        if origin is None or urlsplit(origin).hostname not in {
            "127.0.0.1",
            "localhost",
            "::1",
        }:
            raise ValueError("refusing non-local Origin for sensitive action")


def csrf_hidden_input() -> str:
    return f'<input type="hidden" name="{CSRF_FIELD}" value="{escape(csrf_token())}">'


def inject_csrf_into_post_forms(html: str) -> str:
    hidden = csrf_hidden_input()
    return _POST_FORM_RE.sub(lambda match: f"{match.group(1)}{hidden}", html)


def _is_local_host(value: str | None) -> bool:
    if value is None or not str(value).strip():
        return False
    host = str(value).strip().lower()
    if host.startswith("["):
        end = host.find("]")
        hostname = host[: end + 1] if end != -1 else host
    else:
        hostname = host.split(":", 1)[0]
    return hostname in LOCAL_HOSTS


def _canonical_origin(value: str | None) -> str | None:
    if value is None or not str(value).strip() or str(value).strip().lower() == "null":
        return None
    try:
        parsed = urlsplit(str(value).strip())
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        return None
    hostname = parsed.hostname.rstrip(".").lower()
    default_port = 443 if parsed.scheme.lower() == "https" else 80
    suffix = f":{port}" if port is not None and port != default_port else ""
    return f"{parsed.scheme.lower()}://{hostname}{suffix}"


def _canonical_host(value: str | None, *, scheme: str) -> str | None:
    if value is None or not str(value).strip():
        return None
    try:
        parsed = urlsplit(f"{scheme}://{str(value).strip()}")
        port = parsed.port
    except ValueError:
        return None
    if not parsed.hostname or parsed.username or parsed.password:
        return None
    default_port = 443 if scheme == "https" else 80
    suffix = f":{port}" if port is not None and port != default_port else ""
    return f"{parsed.hostname.rstrip('.').lower()}{suffix}"
