---
name: test-generator
description: Generate tests for crypto threshold components
invocation: user-only
disable-model-invocation: true
---

# Test Generator Skill

## Purpose
Generate comprehensive tests for new crypto threshold features.

## Test Patterns

### 1. Unit Tests (Domain Logic)
```python
"""Tests for [component name]."""

from __future__ import annotations

from decimal import Decimal

from crypto_threshold.domain.[module] import [function]


class Test[Feature]:
    """Tests for [feature description]."""

    def test_basic_case(self) -> None:
        """Should handle basic case correctly."""
        # Arrange
        input_data = "..."

        # Act
        result = [function](input_data)

        # Assert
        assert result.field == expected_value

    def test_edge_case(self) -> None:
        """Should handle edge case correctly."""
        # Test boundary conditions

    def test_error_case(self) -> None:
        """Should handle error case correctly."""
        # Test error handling
```

### 2. Integration Tests (Adapters)
```python
"""Tests for [adapter name]."""

from __future__ import annotations

import httpx
import pytest

from crypto_threshold.adapters.[module] import [Class]


class Test[Adapter]:
    """Tests for [adapter description]."""

    def test_successful_request(self, httpx_mock) -> None:
        """Should handle successful API response."""
        # Mock API response
        httpx_mock.add_response(
            url="[api_url]",
            json={"key": "value"},
        )

        # Test
        provider = [Class]()
        result = provider.get_data()

        # Assert
        assert result.field == expected_value

    def test_error_handling(self, httpx_mock) -> None:
        """Should handle API errors gracefully."""
        # Mock error response
        httpx_mock.add_response(
            url="[api_url]",
            status_code=500,
        )

        # Test error handling
        with pytest.raises(Exception):
            provider = [Class]()
            provider.get_data()
```

### 3. CLI Tests (Typer Commands)
```python
"""Tests for CLI commands."""

from __future__ import annotations

from typer.testing import CliRunner

from crypto_threshold.cli import app

runner = CliRunner()


class TestCLI:
    """Tests for CLI commands."""

    def test_command_help(self) -> None:
        """Should show help text."""
        result = runner.invoke(app, ["[command]", "--help"])
        assert result.exit_code == 0
        assert "help text" in result.output

    def test_command_success(self) -> None:
        """Should execute command successfully."""
        result = runner.invoke(app, ["[command]", "args"])
        assert result.exit_code == 0
```

### 4. Mock Tests (External APIs)
```python
"""Tests with mocked external APIs."""

from __future__ import annotations

import httpx
import pytest

from crypto_threshold.adapters.prices.binance import BinanceProvider


class TestBinanceProvider:
    """Tests for Binance price provider."""

    def test_get_ticker_price(self, httpx_mock) -> None:
        """Should fetch and parse ticker price."""
        # Mock Binance API
        httpx_mock.add_response(
            url="https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT",
            json={"symbol": "BTCUSDT", "price": "64000.00"},
        )

        # Test
        provider = BinanceProvider()
        snapshot = provider.get_ticker_price("BTC")

        # Assert
        assert snapshot.asset == "BTC"
        assert snapshot.price == Decimal("64000.00")
        assert snapshot.provider == "binance"
```

## Test Categories

### 1. Rule Parser Tests
- Test all operator keywords (above, below, over, under, etc.)
- Test amount normalization ($100k, $100,000, $100m)
- Test date extraction
- Test rejection cases (SOL, DOGE, hit/touch, missing date)

### 2. Price Provider Tests
- Mock HTTP responses
- Test error handling
- Verify data parsing
- Test cross-check logic

### 3. Probability Tests
- Test Black-Scholes calculation
- Test volatility blending
- Test edge calculation
- Test confidence intervals

### 4. CLI Tests
- Test help output
- Test command execution
- Test error messages
- Test exit codes

## Test File Template

```python
"""Tests for [module name].

This module tests [brief description of what is being tested].
"""

from __future__ import annotations

from decimal import Decimal
from datetime import UTC, datetime

import pytest

from crypto_threshold.[module] import [Class], [function]


# ── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
def sample_data():
    """Provide sample data for tests."""
    return {
        "field1": "value1",
        "field2": Decimal("100"),
    }


# ── Tests ────────────────────────────────────────────────────────────────────

class Test[Feature]:
    """Tests for [feature]."""

    def test_basic_functionality(self, sample_data) -> None:
        """Should perform basic functionality correctly."""
        # Arrange
        input_data = sample_data

        # Act
        result = [function](input_data)

        # Assert
        assert result.field == expected_value

    def test_edge_cases(self) -> None:
        """Should handle edge cases correctly."""
        # Test boundary conditions

    def test_error_handling(self) -> None:
        """Should handle errors gracefully."""
        # Test error scenarios
```

## Running Tests

```bash
# Run all tests
uv run pytest -q

# Run specific test file
uv run pytest tests/test_[module].py -q

# Run with verbose output
uv run pytest tests/test_[module].py -v

# Run with coverage
uv run pytest --cov=src/crypto_threshold tests/
```

## Test Coverage Goals

- **Rule Parser:** 100% coverage
- **Price Providers:** 90% coverage
- **CLI Commands:** 80% coverage
- **Services:** 85% coverage

## Integration with CI/CD

Tests run automatically on:
- Every commit
- Pull requests
- Main branch merges

## Example: Adding Tests for New Feature

When adding a new feature:

1. Create test file: `tests/test_[feature].py`
2. Add test class: `Test[Feature]`
3. Add test methods following patterns above
4. Run tests: `uv run pytest tests/test_[feature].py -v`
5. Ensure all tests pass
6. Check coverage: `uv run pytest --cov=src/crypto_threshold tests/`

## Resources

- pytest documentation: https://docs.pytest.org/
- httpx_mock: https://github.com/Colin-b/pytest_httpx
- Existing tests: `tests/` directory
- Test patterns: `tests/test_rules.py`, `tests/test_binance_provider.py`
