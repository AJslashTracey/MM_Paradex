from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any


VARIATIONAL_STATS_URL = "https://omni-client-api.prod.ap-northeast-1.variational.io/metadata/stats"
OSTIUM_PRICES_URL = "https://builder.prod.bedrock.ostium.io/v1/prices"


STOCK_WATCHLIST = {
    "SNDK",
    "NVDA",
    "AAPL",
    "MSFT",
    "TSLA",
    "AMD",
    "MU",
    "TSM",
    "COIN",
    "PLTR",
    "HOOD",
    "CRCL",
    "SPCX",
    "MSTR",
}


@dataclass(frozen=True)
class VenueInfo:
    name: str
    structure: str
    tradable_now: bool
    note: str


@dataclass(frozen=True)
class VenueQuote:
    venue: str
    symbol: str
    structure: str
    bid: Decimal | None
    ask: Decimal | None
    mid: Decimal | None
    day_volume_usd: Decimal | None
    open_interest_usd: Decimal | None
    tradable_now: bool
    market_open: bool | None
    note: str
    raw: dict[str, Any]


VENUES = {
    "tradexyz": VenueInfo(
        name="Trade[XYZ] / Hyperliquid HIP-3",
        structure="CLOB, HyperCore, 24/7",
        tradable_now=True,
        note="Primary venue ranked by 24h notional volume.",
    ),
    "variational": VenueInfo(
        name="Variational Omni",
        structure="RFQ, not an order book",
        tradable_now=True,
        note="Indicative RFQ quotes; firm price is only known after requesting a quote.",
    ),
    "ostium": VenueInfo(
        name="Ostium",
        structure="Oracle/execution-network model",
        tradable_now=True,
        note="Builder API exposes bid/mid/ask and market-session state.",
    ),
    "paradex": VenueInfo(
        name="Paradex",
        structure="Roadmap",
        tradable_now=False,
        note="Equity Perps are listed as upcoming, so no current stock-perp venue leg.",
    ),
}


def decimal_or_none(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def open_json(url: str, timeout_s: float = 15.0) -> Any:
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": "Mozilla/5.0"},
    )
    with urllib.request.urlopen(request, timeout=timeout_s) as response:
        return json.loads(response.read().decode("utf-8"))


def variational_quotes(timeout_s: float = 15.0) -> dict[str, VenueQuote]:
    try:
        payload = open_json(VARIATIONAL_STATS_URL, timeout_s=timeout_s)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError):
        return {}

    quotes: dict[str, VenueQuote] = {}
    for listing in payload.get("listings", []):
        symbol = str(listing.get("ticker", "")).upper()
        quote = listing.get("quotes", {}).get("size_100k") or listing.get("quotes", {}).get("base", {})
        long_oi = decimal_or_none(listing.get("open_interest", {}).get("long_open_interest"))
        short_oi = decimal_or_none(listing.get("open_interest", {}).get("short_open_interest"))
        bid = decimal_or_none(quote.get("bid"))
        ask = decimal_or_none(quote.get("ask"))
        mid = decimal_or_none(listing.get("mark_price"))
        quotes[symbol] = VenueQuote(
            venue=VENUES["variational"].name,
            symbol=symbol,
            structure=VENUES["variational"].structure,
            bid=bid,
            ask=ask,
            mid=mid,
            day_volume_usd=decimal_or_none(listing.get("volume_24h")),
            open_interest_usd=(long_oi or Decimal("0")) + (short_oi or Decimal("0")),
            tradable_now=True,
            market_open=None,
            note=VENUES["variational"].note,
            raw=listing,
        )
    return quotes


def ostium_quotes(timeout_s: float = 15.0) -> dict[str, VenueQuote]:
    try:
        payload = open_json(OSTIUM_PRICES_URL, timeout_s=timeout_s)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError):
        return {}

    quotes: dict[str, VenueQuote] = {}
    for price in payload.get("prices", []):
        symbol = str(price.get("from", "")).upper()
        quotes[symbol] = VenueQuote(
            venue=VENUES["ostium"].name,
            symbol=symbol,
            structure=VENUES["ostium"].structure,
            bid=decimal_or_none(price.get("bid")),
            ask=decimal_or_none(price.get("ask")),
            mid=decimal_or_none(price.get("mid")),
            day_volume_usd=None,
            open_interest_usd=None,
            tradable_now=True,
            market_open=bool(price.get("isMarketOpen")),
            note=VENUES["ostium"].note,
            raw=price,
        )
    return quotes


def paradex_placeholder(symbol: str) -> VenueQuote:
    return VenueQuote(
        venue=VENUES["paradex"].name,
        symbol=symbol,
        structure=VENUES["paradex"].structure,
        bid=None,
        ask=None,
        mid=None,
        day_volume_usd=None,
        open_interest_usd=None,
        tradable_now=False,
        market_open=None,
        note=VENUES["paradex"].note,
        raw={},
    )

