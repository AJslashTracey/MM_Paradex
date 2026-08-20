from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any


YAHOO_QUOTE_URL = "https://query1.finance.yahoo.com/v7/finance/quote"


@dataclass(frozen=True)
class ReferenceMarket:
    venue: str
    symbol: str
    kind: str
    note: str = ""


STATIC_REFERENCES: dict[str, list[ReferenceMarket]] = {
    "XYZ100": [
        ReferenceMarket("Yahoo", "QQQ", "etf", "rough tech/growth ETF proxy"),
        ReferenceMarket("Yahoo", "^NDX", "index", "rough Nasdaq 100 proxy"),
    ],
    "SP500": [
        ReferenceMarket("Yahoo", "^GSPC", "index", "S&P 500 cash index"),
        ReferenceMarket("Yahoo", "SPY", "etf", "S&P 500 ETF proxy"),
        ReferenceMarket("Yahoo", "ES=F", "future", "E-mini S&P 500 future"),
    ],
    "CL": [ReferenceMarket("Yahoo", "CL=F", "future", "WTI crude future")],
    "BRENTOIL": [ReferenceMarket("Yahoo", "BZ=F", "future", "Brent crude future")],
    "NATGAS": [ReferenceMarket("Yahoo", "NG=F", "future", "Henry Hub natural gas future")],
    "GOLD": [ReferenceMarket("Yahoo", "GC=F", "future", "COMEX gold future")],
    "SILVER": [ReferenceMarket("Yahoo", "SI=F", "future", "COMEX silver future")],
    "COPPER": [ReferenceMarket("Yahoo", "HG=F", "future", "COMEX copper future")],
    "EURUSD": [ReferenceMarket("Yahoo", "EURUSD=X", "fx", "EUR/USD spot proxy")],
    "GBPUSD": [ReferenceMarket("Yahoo", "GBPUSD=X", "fx", "GBP/USD spot proxy")],
    "USDJPY": [ReferenceMarket("Yahoo", "JPY=X", "fx", "USD/JPY spot proxy")],
}


def decimal_or_none(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def references_for_symbol(
    symbol: str,
    native_perp_symbols: set[str],
    paradex_perp_symbols: dict[str, str] | None = None,
    binance_perp_symbols: dict[str, str] | None = None,
) -> list[ReferenceMarket]:
    refs = list(STATIC_REFERENCES.get(symbol, []))
    if symbol in native_perp_symbols:
        refs.append(ReferenceMarket("Hyperliquid", symbol, "perp", "native Hyperliquid perp"))
    if paradex_perp_symbols and symbol in paradex_perp_symbols:
        refs.append(
            ReferenceMarket(
                "Paradex",
                paradex_perp_symbols[symbol],
                "perp",
                "Paradex perp midpoint",
            )
        )
    if (
        binance_perp_symbols
        and symbol in native_perp_symbols
        and symbol in binance_perp_symbols
    ):
        refs.append(
            ReferenceMarket(
                "Binance",
                binance_perp_symbols[symbol],
                "perp",
                "Binance USD-M perp midpoint",
            )
        )

    if not refs and symbol.isalpha() and 1 <= len(symbol) <= 5:
        refs.append(ReferenceMarket("Yahoo", symbol, "equity", "same-symbol equity lookup"))

    return refs


def yahoo_quotes(symbols: list[str], timeout_s: float = 15.0) -> dict[str, Decimal]:
    if not symbols:
        return {}

    query = urllib.parse.urlencode({"symbols": ",".join(sorted(set(symbols)))})
    request = urllib.request.Request(
        f"{YAHOO_QUOTE_URL}?{query}",
        headers={"User-Agent": "arb-xyz-scanner/0.1"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.HTTPError, urllib.error.URLError):
        return {}

    prices: dict[str, Decimal] = {}
    for item in payload.get("quoteResponse", {}).get("result", []):
        symbol = item.get("symbol")
        price = (
            decimal_or_none(item.get("regularMarketPrice"))
            or decimal_or_none(item.get("postMarketPrice"))
            or decimal_or_none(item.get("preMarketPrice"))
            or decimal_or_none(item.get("bid"))
            or decimal_or_none(item.get("ask"))
        )
        if symbol and price is not None:
            prices[str(symbol)] = price
    return prices
