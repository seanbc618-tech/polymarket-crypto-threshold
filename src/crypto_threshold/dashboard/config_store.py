"""Small non-secret ``.env`` updater used by the local dashboard."""

from __future__ import annotations

import os
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

FORBIDDEN_CONFIG_KEYS = frozenset({"POLYMARKET_PRIVATE_KEY"})


class ConfigStoreError(RuntimeError):
    """Raised when a non-secret dashboard config update cannot be persisted."""


@dataclass(frozen=True)
class ConfigFileSnapshot:
    existed: bool
    payload: bytes = b""
    mode: int = 0o600


def snapshot_config_file(path: str | Path) -> ConfigFileSnapshot:
    """Capture exact file state so a multi-store update can be rolled back."""

    target = Path(path)
    if not target.exists():
        return ConfigFileSnapshot(existed=False)
    return ConfigFileSnapshot(
        existed=True,
        payload=target.read_bytes(),
        mode=target.stat().st_mode & 0o777,
    )


def restore_config_file(path: str | Path, snapshot: ConfigFileSnapshot) -> None:
    """Restore an earlier config snapshot atomically."""

    target = Path(path)
    if not snapshot.existed:
        try:
            target.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise ConfigStoreError(f"failed to remove rolled-back config {target}: {exc}") from exc
        return
    _atomic_replace(target, snapshot.payload, mode=snapshot.mode)


def secret_keys_with_values(path: str | Path) -> tuple[str, ...]:
    """Return forbidden keys that already contain non-empty file values."""

    target = Path(path)
    if not target.exists():
        return ()
    found: list[str] = []
    for line in target.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        normalized_key = key.strip()
        if normalized_key in FORBIDDEN_CONFIG_KEYS and value.strip():
            found.append(normalized_key)
    return tuple(sorted(set(found)))


def update_env_file(
    path: str | Path,
    updates: Mapping[str, str | None],
) -> None:
    """Update selected keys while preserving comments and unrelated settings."""

    target = Path(path)
    forbidden = FORBIDDEN_CONFIG_KEYS.intersection(updates)
    if forbidden:
        raise ConfigStoreError(
            f"refusing to write secret config keys: {', '.join(sorted(forbidden))}"
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    original = target.read_text(encoding="utf-8") if target.exists() else ""
    pending = {str(key): value for key, value in updates.items()}
    rendered: list[str] = []
    seen: set[str] = set()
    for line in original.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            rendered.append(line)
            continue
        key = line.split("=", 1)[0].strip()
        if key not in pending:
            rendered.append(line)
            continue
        seen.add(key)
        value = pending[key]
        if value is not None:
            rendered.append(f"{key}={_validate_value(value)}")
    for key, value in pending.items():
        if key in seen or value is None:
            continue
        rendered.append(f"{key}={_validate_value(value)}")
    payload = ("\n".join(rendered).rstrip() + "\n").encode("utf-8")
    _atomic_replace(target, payload, mode=0o600)


def _atomic_replace(target: Path, payload: bytes, *, mode: int) -> None:
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "wb",
            dir=target.parent,
            prefix=f".{target.name}.",
            delete=False,
        ) as handle:
            handle.write(payload)
            temp_path = Path(handle.name)
        os.chmod(temp_path, mode)
        os.replace(temp_path, target)
    except OSError as exc:
        if temp_path is not None:
            try:
                temp_path.unlink()
            except OSError:
                pass
        raise ConfigStoreError(f"failed to update {target}: {exc}") from exc


def _validate_value(value: str) -> str:
    text = str(value).strip()
    if any(character in text for character in ("\n", "\r", "\x00")):
        raise ConfigStoreError("config values must be single-line text")
    return text
