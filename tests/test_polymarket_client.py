"""Public-only Polymarket HTTP client tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest

from crypto_threshold.adapters.polymarket.client import GammaClobReadClient
from crypto_threshold.config import Settings
from tests.conftest import make_market_payload


def test_discovery_queries_both_threshold_directions() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        query = request.url.params["q"]
        suffix = "above" if "above" in query else "below"
        market = make_market_payload(id=f"market-{suffix}", conditionId=f"condition-{suffix}")
        return httpx.Response(
            200,
            json={"events": [{"id": "event-1", "markets": [market]}]},
        )

    transport = httpx.MockTransport(handler)
    settings = Settings(_env_file=None)
    client = GammaClobReadClient(settings, client=httpx.Client(transport=transport))
    markets = client.discover_markets("BTC", 4)
    assert {market["id"] for market in markets} == {"market-above", "market-below"}
    assert {request.url.params["q"] for request in requests} == {
        "Bitcoin above",
        "Bitcoin below",
    }
    assert all(request.url.params["events_status"] == "active" for request in requests)
    assert all(request.url.params["keep_closed_markets"] == "0" for request in requests)
    assert all(request.method == "GET" for request in requests)


@pytest.mark.parametrize(
    ("asset", "name"),
    [("SOL", "Solana"), ("XRP", "XRP")],
)
def test_discovery_supports_each_additional_daily_asset(
    asset: str,
    name: str,
) -> None:
    queries: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        queries.append(str(request.url.params["q"]))
        return httpx.Response(200, json={"events": []})

    client = GammaClobReadClient(
        Settings(_env_file=None),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    assert client.discover_markets(asset, 4) == []
    assert set(queries) == {f"{name} above", f"{name} below"}


def test_market_book_and_fee_calls_are_get_only() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.startswith("/markets/"):
            return httpx.Response(200, json=make_market_payload())
        if request.url.path == "/book":
            return httpx.Response(200, json={"bids": [], "asks": []})
        return httpx.Response(200, json={"fd": {"r": 0.07, "e": 1, "to": True}})

    transport = httpx.MockTransport(handler)
    settings = Settings(_env_file=None)
    client = GammaClobReadClient(settings, client=httpx.Client(transport=transport))
    client.get_market("market-1")
    client.get_order_book("yes-token")
    client.get_market_info("condition-1")
    assert [request.method for request in requests] == ["GET", "GET", "GET"]
    assert requests[-1].url.path == "/clob-markets/condition-1"


def test_event_context_resolves_event_without_mutating_market_payload() -> None:
    market = make_market_payload()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"events": [{"id": "event-1", "markets": [market]}]},
        )

    transport = httpx.MockTransport(handler)
    settings = Settings(_env_file=None)
    client = GammaClobReadClient(settings, client=httpx.Client(transport=transport))
    context = client.get_market_event_context("market-1", "condition-1", market["question"])
    assert context.event_id == "event-1"
    assert context.raw_payload["events"][0]["id"] == "event-1"


def test_clob_server_time_health_read() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/time"
        return httpx.Response(200, json=1784700000)

    transport = httpx.MockTransport(handler)
    settings = Settings(_env_file=None)
    client = GammaClobReadClient(settings, client=httpx.Client(transport=transport))
    assert client.get_server_time() == 1784700000


def test_updown_discovery_uses_exact_tags_and_returns_all_fourteen() -> None:
    requests: list[httpx.Request] = []
    start = datetime(2026, 7, 25, 12, 0, tzinfo=UTC)
    end = start + timedelta(minutes=16)
    assets = ("btc", "eth", "sol", "xrp", "doge", "bnb", "hype")

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        interval = str(request.url.params["tag_slug"]).lower()
        events = []
        for asset in assets:
            slug = f"{asset}-up-or-down-{interval}"
            events.append(
                {
                    "id": f"event-{asset}-{interval}",
                    "seriesSlug": slug,
                    "recurrence": "daily",
                    "series": [{"slug": slug, "recurrence": "daily"}],
                    "markets": [
                        {
                            "id": f"market-{asset}-{interval}",
                            "conditionId": f"condition-{asset}-{interval}",
                            "question": f"{asset} Up or Down?",
                        }
                    ],
                }
            )
        return httpx.Response(200, json=events)

    client = GammaClobReadClient(
        Settings(_env_file=None),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    markets = client.discover_updown_markets(
        ("5m", "15m"),
        start=start,
        end=end,
        limit=14,
    )

    assert len(markets) == 14
    assert {request.url.params["tag_slug"] for request in requests} == {"5M", "15M"}
    assert all(request.method == "GET" for request in requests)
    assert all(request.url.path == "/events" for request in requests)
    assert all("events" in market for market in markets)


@pytest.mark.parametrize(
    ("interval", "variant"),
    (("5m", "fiveminute"), ("15m", "fifteen")),
)
def test_crypto_window_price_uses_public_site_endpoint_and_preserves_decimal(
    interval: str,
    variant: str,
) -> None:
    requests: list[httpx.Request] = []
    start = datetime(2026, 7, 27, 9, 0, tzinfo=UTC)
    end = start + timedelta(minutes=5 if interval == "5m" else 15)

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            content=(
                b'{"openPrice":1876.9833419425354,"closePrice":null,'
                b'"completed":false,"incomplete":true,"cached":true,'
                b'"timestamp":1785143404845}'
            ),
            headers={"content-type": "application/json"},
        )

    client = GammaClobReadClient(
        Settings(
            POLYMARKET_SITE_API_BASE="https://polymarket.test/api",
            _env_file=None,
        ),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    payload = client.get_crypto_window_price(
        "ETH",
        interval=interval,
        start=start,
        end=end,
    )

    assert payload["openPrice"] == Decimal("1876.9833419425354")
    assert len(requests) == 1
    request = requests[0]
    assert request.method == "GET"
    assert request.url == httpx.URL(
        "https://polymarket.test/api/crypto/crypto-price",
        params={
            "symbol": "ETH",
            "eventStartTime": "2026-07-27T09:00:00Z",
            "variant": variant,
            "endDate": end.isoformat().replace("+00:00", "Z"),
        },
    )
