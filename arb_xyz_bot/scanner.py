from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal
from typing import Any

from .hyperliquid import HyperliquidClient, Market
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


def scan(top: int = 20, min_edge_bps: Decimal = Decimal("0")) -> list[ScanRow]:
    client = HyperliquidClient()
    xyz_markets = sorted(client.xyz_markets(), key=lambda m: m.day_ntl_vlm, reverse=True)
    native_prices = client.native_perp_prices()
    selected = xyz_markets[:top]

    ref_by_symbol: dict[str, list[ReferenceMarket]] = {
        market.symbol: references_for_symbol(market.symbol, set(native_prices))
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

        comparisons: list[Comparison] = []
        for ref in ref_by_symbol[market.symbol]:
            ref_price = None
            if ref.venue == "Yahoo":
                ref_price = yahoo_prices.get(ref.symbol)
            elif ref.venue == "Hyperliquid":
                ref_price = native_prices.get(ref.symbol)

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

