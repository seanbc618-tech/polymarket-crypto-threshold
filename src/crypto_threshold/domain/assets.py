"""Canonical asset identities and contract-family capabilities."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AssetContract:
    symbol: str
    display_name: str
    aliases: tuple[str, ...]
    chainlink_pair: str
    daily_threshold: bool = False
    short_updown: bool = False
    binance_pair: str | None = None
    binance_symbol: str | None = None
    coinbase_symbol: str | None = None


ASSET_CONTRACTS: dict[str, AssetContract] = {
    "BTC": AssetContract(
        symbol="BTC",
        display_name="Bitcoin",
        aliases=("bitcoin", "btc"),
        chainlink_pair="BTC/USD",
        daily_threshold=True,
        short_updown=True,
        binance_pair="BTC/USDT",
        binance_symbol="BTCUSDT",
        coinbase_symbol="BTC-USD",
    ),
    "ETH": AssetContract(
        symbol="ETH",
        display_name="Ethereum",
        aliases=("ethereum", "ether", "eth"),
        chainlink_pair="ETH/USD",
        daily_threshold=True,
        short_updown=True,
        binance_pair="ETH/USDT",
        binance_symbol="ETHUSDT",
        coinbase_symbol="ETH-USD",
    ),
    "SOL": AssetContract(
        symbol="SOL",
        display_name="Solana",
        aliases=("solana", "sol"),
        chainlink_pair="SOL/USD",
        daily_threshold=True,
        short_updown=True,
        binance_pair="SOL/USDT",
        binance_symbol="SOLUSDT",
        coinbase_symbol="SOL-USD",
    ),
    "XRP": AssetContract(
        symbol="XRP",
        display_name="XRP",
        aliases=("xrp", "ripple"),
        chainlink_pair="XRP/USD",
        daily_threshold=True,
        short_updown=True,
        binance_pair="XRP/USDT",
        binance_symbol="XRPUSDT",
        coinbase_symbol="XRP-USD",
    ),
    "DOGE": AssetContract(
        symbol="DOGE",
        display_name="Dogecoin",
        aliases=("dogecoin", "doge"),
        chainlink_pair="DOGE/USD",
        short_updown=True,
    ),
    "BNB": AssetContract(
        symbol="BNB",
        display_name="BNB",
        aliases=("bnb", "binance coin"),
        chainlink_pair="BNB/USD",
        short_updown=True,
    ),
    "HYPE": AssetContract(
        symbol="HYPE",
        display_name="Hyperliquid",
        aliases=("hyperliquid", "hype"),
        chainlink_pair="HYPE/USD",
        short_updown=True,
    ),
}

DAILY_THRESHOLD_ASSETS = frozenset(
    asset for asset, contract in ASSET_CONTRACTS.items() if contract.daily_threshold
)
SHORT_UPDOWN_ASSETS = frozenset(
    asset for asset, contract in ASSET_CONTRACTS.items() if contract.short_updown
)
# Compatibility name for the original daily-threshold runtime.
SUPPORTED_ASSETS = DAILY_THRESHOLD_ASSETS
SUPPORTED_BINANCE_SYMBOLS = frozenset(
    contract.binance_symbol
    for contract in ASSET_CONTRACTS.values()
    if contract.daily_threshold and contract.binance_symbol is not None
)
SUPPORTED_CHAINLINK_PAIRS = frozenset(
    contract.chainlink_pair
    for contract in ASSET_CONTRACTS.values()
    if contract.short_updown
)


def asset_contract(asset: str) -> AssetContract:
    symbol = asset.upper()
    try:
        return ASSET_CONTRACTS[symbol]
    except KeyError as exc:
        raise ValueError(f"unsupported crypto asset: {asset}") from exc


def asset_for_binance_symbol(symbol: str) -> str | None:
    normalized = symbol.upper()
    for asset, contract in ASSET_CONTRACTS.items():
        if contract.binance_symbol == normalized:
            return asset
    return None


def asset_for_chainlink_pair(pair: str) -> str | None:
    normalized = pair.upper()
    for asset, contract in ASSET_CONTRACTS.items():
        if contract.chainlink_pair == normalized:
            return asset
    return None
