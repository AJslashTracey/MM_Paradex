from __future__ import annotations

import json
import urllib.request
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any


EXCHANGE_INFO_URL = "https://fapi.binance.com/fapi/v1/exchangeInfo"
BOOK_TICKER_URL = "https://fapi.binance.com/fapi/v1/ticker/bookTicker"


def decimal_or_none(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


@dataclass(frozen=True)
class BinancePerp:
    symbol: str
    base_symbol: str
    bid: Decimal | None
    ask: Decimal | None

    @property
    def mid_price(self) -> Decimal | None:
        if self.bid is None or self.ask is None:
            return None
        return (self.bid + self.ask) / Decimal("2")


class BinanceFuturesClient:
    def __init__(
        self,
        exchange_info_url: str = EXCHANGE_INFO_URL,
        book_ticker_url: str = BOOK_TICKER_URL,
        timeout_s: float = 15.0,
    ) -> None:
        self.exchange_info_url = exchange_info_url
        self.book_ticker_url = book_ticker_url
        self.timeout_s = timeout_s

    def _get_json(self, url: str) -> Any:
        request = urllib.request.Request(
            url,
            headers={"Accept": "application/json", "User-Agent": "arb-xyz-scanner/0.1"},
        )
        with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
            return json.loads(response.read().decode("utf-8"))

    def usdt_perpetual_markets(self) -> dict[str, str]:
        payload = self._get_json(self.exchange_info_url)
        markets: dict[str, str] = {}
        for item in payload.get("symbols", []):
            if item.get("contractType") != "PERPETUAL":
                continue
            if item.get("status") != "TRADING":
                continue
            if item.get("quoteAsset") != "USDT":
                continue

            base_symbol = str(item.get("baseAsset", ""))
            symbol = str(item.get("symbol", ""))
            if base_symbol and symbol:
                markets[base_symbol] = symbol
        return markets

    def usdt_perp_mid_prices(self) -> dict[str, tuple[str, Decimal]]:
        usdt_markets = self.usdt_perpetual_markets()
        payload = self._get_json(self.book_ticker_url)
        prices: dict[str, tuple[str, Decimal]] = {}
        for item in payload:
            symbol = str(item.get("symbol", ""))
            bid = decimal_or_none(item.get("bidPrice"))
            ask = decimal_or_none(item.get("askPrice"))
            if symbol not in usdt_markets.values():
                continue

            perp = BinancePerp(
                symbol=symbol,
                base_symbol="",
                bid=bid,
                ask=ask,
            )
            mid_price = perp.mid_price
            if mid_price is None:
                continue

            for base_symbol, market_symbol in usdt_markets.items():
                if market_symbol == symbol:
                    prices[base_symbol] = (market_symbol, mid_price)
                    break
        return prices
