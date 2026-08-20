from __future__ import annotations

import json
import urllib.parse
import urllib.request
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any


SUMMARY_URL = "https://api.prod.paradex.trade/v1/markets/summary"


def decimal_or_none(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


@dataclass(frozen=True)
class ParadexMarket:
    symbol: str
    base_symbol: str
    bid: Decimal | None
    ask: Decimal | None
    mark_price: Decimal | None

    @property
    def mid_price(self) -> Decimal | None:
        if self.bid is not None and self.ask is not None:
            return (self.bid + self.ask) / Decimal("2")
        return self.mark_price


class ParadexClient:
    def __init__(self, summary_url: str = SUMMARY_URL, timeout_s: float = 15.0) -> None:
        self.summary_url = summary_url
        self.timeout_s = timeout_s

    def markets_summary(self, market: str = "ALL") -> list[ParadexMarket]:
        query = urllib.parse.urlencode({"market": market})
        request = urllib.request.Request(
            f"{self.summary_url}?{query}",
            headers={"Accept": "application/json", "User-Agent": "arb-xyz-scanner/0.1"},
        )
        with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
            payload = json.loads(response.read().decode("utf-8"))

        markets: list[ParadexMarket] = []
        for item in payload.get("results", []):
            symbol = str(item.get("symbol", ""))
            if not symbol.endswith("-PERP"):
                continue

            base_symbol = symbol.split("-", 1)[0]
            markets.append(
                ParadexMarket(
                    symbol=symbol,
                    base_symbol=base_symbol,
                    bid=decimal_or_none(item.get("bid")),
                    ask=decimal_or_none(item.get("ask")),
                    mark_price=decimal_or_none(item.get("mark_price")),
                )
            )
        return markets

    def perp_mid_prices(self) -> dict[str, tuple[str, Decimal]]:
        prices: dict[str, tuple[str, Decimal]] = {}
        for market in self.markets_summary():
            mid_price = market.mid_price
            if mid_price is None:
                continue
            prices[market.base_symbol] = (market.symbol, mid_price)
        return prices
