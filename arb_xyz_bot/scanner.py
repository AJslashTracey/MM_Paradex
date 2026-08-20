from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal
from typing import Any

from .binance import BinanceFuturesClient
from .hyperliquid import HyperliquidClient, Market
from .paradex import ParadexClient
from .references import ReferenceMarket, references_for_symbol, yahoo_quotes


@dataclass(frozen=True)
class Comparison:
    reference: ReferenceMarket
    reference_price: Decimal
    edge_bps: Decimal
    direction: str


@dataclass(frozen=True)
class ScanRow:
    market: Market
    comparisons: list[Comparison]


def bps(xyz_price: Decimal, reference_price: Decimal) -> Decimal:
    return ((xyz_price - reference_price) / reference_price) * Decimal("10000")


def scan(
    top: int = 20,
    min_edge_bps: Decimal = Decimal("0"),
    symbols: set[str] | None = None,
) -> list[ScanRow]:
    hyperliquid = HyperliquidClient()
    xyz_markets = sorted(hyperliquid.xyz_markets(), key=lambda market: market.day_ntl_vlm, reverse=True)
    if symbols:
        wanted = {symbol.upper() for symbol in symbols}
        xyz_markets = [market for market in xyz_markets if market.symbol.upper() in wanted]

    selected = xyz_markets[:top]
    native_prices = hyperliquid.native_perp_prices()
    native_prices_by_symbol = {
        symbol.upper(): price for symbol, price in native_prices.items()
    }
    binance_prices = BinanceFuturesClient().usdt_perp_mid_prices()
    paradex_prices = ParadexClient().perp_mid_prices()
    binance_symbols = {
        base_symbol.upper(): market_symbol
        for base_symbol, (market_symbol, _) in binance_prices.items()
    }
    binance_prices_by_symbol = {
        market_symbol: mid_price for market_symbol, mid_price in binance_prices.values()
    }
    paradex_symbols = {
        base_symbol.upper(): market_symbol
        for base_symbol, (market_symbol, _) in paradex_prices.items()
    }
    paradex_prices_by_symbol = {
        market_symbol: mid_price for market_symbol, mid_price in paradex_prices.values()
    }

    ref_by_symbol: dict[str, list[ReferenceMarket]] = {
        market.symbol.upper(): references_for_symbol(
            market.symbol.upper(),
            set(native_prices_by_symbol),
            paradex_symbols,
            binance_symbols,
        )
        for market in selected
    }
    yahoo_symbols = [
        ref.symbol
        for refs in ref_by_symbol.values()
        for ref in refs
        if ref.venue == "Yahoo"
    ]
    yahoo_prices = yahoo_quotes(yahoo_symbols)

    rows: list[ScanRow] = []
    for market in selected:
        xyz_price = market.best_price
        if xyz_price is None:
            rows.append(ScanRow(market=market, comparisons=[]))
            continue

        symbol = market.symbol.upper()
        comparisons: list[Comparison] = []
        for ref in ref_by_symbol[symbol]:
            ref_price = None
            if ref.venue == "Yahoo":
                ref_price = yahoo_prices.get(ref.symbol)
            elif ref.venue == "Hyperliquid":
                ref_price = native_prices_by_symbol.get(ref.symbol.upper())
            elif ref.venue == "Paradex":
                ref_price = paradex_prices_by_symbol.get(ref.symbol)
            elif ref.venue == "Binance":
                ref_price = binance_prices_by_symbol.get(ref.symbol)

            if ref_price is None or ref_price == 0:
                continue

            edge = bps(xyz_price, ref_price)
            if abs(edge) < min_edge_bps:
                continue

            direction = (
                "short XYZ / long reference"
                if edge > 0
                else "long XYZ / short reference"
            )
            comparisons.append(Comparison(ref, ref_price, edge, direction))

        rows.append(ScanRow(market=market, comparisons=comparisons))

    return rows


def row_to_dict(row: ScanRow) -> dict[str, Any]:
    market = row.market
    return {
        "symbol": market.symbol,
        "coin": market.coin,
        "xyz_price": str(market.best_price) if market.best_price is not None else None,
        "day_volume_usd": str(market.day_ntl_vlm),
        "open_interest_usd": str(market.open_interest_usd)
        if market.open_interest_usd is not None
        else None,
        "funding": str(market.funding) if market.funding is not None else None,
        "comparisons": [
            {
                **asdict(comparison.reference),
                "reference_price": str(comparison.reference_price),
                "edge_bps": str(comparison.edge_bps.quantize(Decimal("0.01"))),
                "direction": comparison.direction,
            }
            for comparison in row.comparisons
        ],
    }
