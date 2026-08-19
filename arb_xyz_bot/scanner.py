from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal
from typing import Any

from .hyperliquid import HyperliquidClient, Market
from .venues import STOCK_WATCHLIST, VenueQuote, ostium_quotes, paradex_placeholder, variational_quotes


@dataclass(frozen=True)
class ArbCheck:
    venue_quote: VenueQuote
    edge_bps: Decimal | None
    direction: str
    executable: bool


@dataclass(frozen=True)
class ScanRow:
    market: Market
    checks: list[ArbCheck]


def bps(exit_price: Decimal, entry_price: Decimal) -> Decimal:
    return ((exit_price - entry_price) / entry_price) * Decimal("10000")


def quote_mid(market: Market) -> Decimal | None:
    return market.mid_px or market.mark_px or market.oracle_px


def best_cross_venue_edge(xyz: Market, quote: VenueQuote) -> tuple[Decimal | None, str, bool]:
    xyz_mid = quote_mid(xyz)
    if xyz_mid is None:
        return None, "no XYZ price", False

    if quote.bid is not None:
        long_xyz_edge = bps(quote.bid, xyz_mid)
    else:
        long_xyz_edge = None

    if quote.ask is not None:
        short_xyz_edge = bps(xyz_mid, quote.ask)
    else:
        short_xyz_edge = None

    candidates = [
        (long_xyz_edge, "long XYZ / sell venue"),
        (short_xyz_edge, "short XYZ / buy venue"),
    ]
    candidates = [(edge, direction) for edge, direction in candidates if edge is not None]
    if not candidates:
        return None, "listed, no comparable bid/ask", False

    edge, direction = max(candidates, key=lambda item: item[0])
    executable = edge > 0 and quote.tradable_now and quote.market_open is not False
    if edge <= 0:
        direction = f"no gross arb; best leg is {direction}"
    if "RFQ" in quote.structure:
        direction = f"{direction} (indicative RFQ)"
    return edge, direction, executable


def scan(
    top: int = 20,
    min_edge_bps: Decimal = Decimal("0"),
    symbols: set[str] | None = None,
) -> list[ScanRow]:
    client = HyperliquidClient()
    xyz_markets = sorted(client.xyz_markets(), key=lambda m: m.day_ntl_vlm, reverse=True)
    if symbols:
        wanted = {symbol.upper() for symbol in symbols}
        xyz_markets = [market for market in xyz_markets if market.symbol.upper() in wanted]

    selected = xyz_markets[:top]
    selected_symbols = {market.symbol.upper() for market in selected}
    venue_quotes = {
        "variational": variational_quotes(),
        "ostium": ostium_quotes(),
    }

    rows: list[ScanRow] = []
    for market in selected:
        checks: list[ArbCheck] = []
        symbol = market.symbol.upper()

        for venue_name in ("variational", "ostium"):
            quote = venue_quotes[venue_name].get(symbol)
            if quote is None:
                continue
            edge, direction, executable = best_cross_venue_edge(market, quote)
            if edge is not None and edge < min_edge_bps:
                continue
            checks.append(ArbCheck(quote, edge, direction, executable))

        if symbol in selected_symbols or symbol in STOCK_WATCHLIST:
            checks.append(
                ArbCheck(
                    paradex_placeholder(symbol),
                    edge_bps=None,
                    direction="not tradable yet",
                    executable=False,
                )
            )

        rows.append(ScanRow(market=market, checks=checks))

    return rows


def row_to_dict(row: ScanRow) -> dict[str, Any]:
    market = row.market
    return {
        "symbol": market.symbol,
        "coin": market.coin,
        "xyz_price": str(quote_mid(market)) if quote_mid(market) is not None else None,
        "day_volume_usd": str(market.day_ntl_vlm),
        "open_interest_usd": str(market.open_interest_usd)
        if market.open_interest_usd is not None
        else None,
        "funding": str(market.funding) if market.funding is not None else None,
        "venues": [
            {
                **asdict(check.venue_quote),
                "bid": str(check.venue_quote.bid) if check.venue_quote.bid is not None else None,
                "ask": str(check.venue_quote.ask) if check.venue_quote.ask is not None else None,
                "mid": str(check.venue_quote.mid) if check.venue_quote.mid is not None else None,
                "day_volume_usd": str(check.venue_quote.day_volume_usd)
                if check.venue_quote.day_volume_usd is not None
                else None,
                "open_interest_usd": str(check.venue_quote.open_interest_usd)
                if check.venue_quote.open_interest_usd is not None
                else None,
                "edge_bps": str(check.edge_bps.quantize(Decimal("0.01")))
                if check.edge_bps is not None
                else None,
                "direction": check.direction,
                "executable": check.executable,
            }
            for check in row.checks
        ],
    }
