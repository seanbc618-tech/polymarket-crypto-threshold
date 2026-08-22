from __future__ import annotations

import pytest

from crypto_threshold_infra.safety import assert_capture_only_environment


def test_capture_rejects_account_credentials() -> None:
    with pytest.raises(RuntimeError, match="forbidden credentials"):
        assert_capture_only_environment({"POLYMARKET_PRIVATE_KEY": "secret"})


def test_capture_accepts_public_only_environment() -> None:
    assert_capture_only_environment({"CAPTURE_OUTPUT": "data/raw"})
