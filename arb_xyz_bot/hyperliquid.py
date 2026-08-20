from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any


INFO_URL = "https://api.hyperliquid.xyz/info"


class HyperliquidError(RuntimeError):
    pass


def normalize_xyz_symbol(symbol: str) -> str:
    if symbol.startswith("xyz:"):
        return symbol.split(":", 1)[1]
    return symbol


def decimal_or_none(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


@dataclass(frozen=True)
class Market:
    symbol: str
    coin: str
    mark_px: Decimal | None
    mid_px: Decimal | None
    oracle_px: Decimal | None
    day_ntl_vlm: Decimal
    funding: Decimal | None
    open_interest: Decimal | None
    raw_meta: dict[str, Any]
    raw_ctx: dict[str, Any]

    @property
    def best_price(self) -> Decimal | None:
        return self.mid_px or self.mark_px or self.oracle_px

    @property
    def open_interest_usd(self) -> Decimal | None:
        if self.open_interest is None or self.best_price is None:
            return None
        return self.open_interest * self.best_price


class HyperliquidClient:
    def __init__(self, info_url: str = INFO_URL, timeout_s: float = 15.0) -> None:
        self.info_url = info_url
        self.timeout_s = timeout_s

    def post_info(self, payload: dict[str, Any]) -> Any:
        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            self.info_url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise HyperliquidError(f"Hyperliquid HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise HyperliquidError(f"Hyperliquid request failed: {exc}") from exc

    def xyz_markets(self) -> list[Market]:
        meta, ctxs = self.post_info({"type": "metaAndAssetCtxs", "dex": "xyz"})
        universe = meta.get("universe", [])
        markets: list[Market] = []
        for asset, ctx in zip(universe, ctxs, strict=False):
            coin = str(asset["name"])
            symbol = normalize_xyz_symbol(coin)
            markets.append(
                Market(
                    symbol=symbol,
                    coin=coin,
                    mark_px=decimal_or_none(ctx.get("markPx")),
                    mid_px=decimal_or_none(ctx.get("midPx")),
                    oracle_px=decimal_or_none(ctx.get("oraclePx")),
                    day_ntl_vlm=decimal_or_none(ctx.get("dayNtlVlm")) or Decimal("0"),
                    funding=decimal_or_none(ctx.get("funding")),
                    open_interest=decimal_or_none(ctx.get("openInterest")),
                    raw_meta=asset,
                    raw_ctx=ctx,
                )
            )
        return markets

    def native_perp_prices(self) -> dict[str, Decimal]:
        meta, ctxs = self.post_info({"type": "metaAndAssetCtxs"})
        prices: dict[str, Decimal] = {}
        for asset, ctx in zip(meta.get("universe", []), ctxs, strict=False):
            price = decimal_or_none(ctx.get("midPx")) or decimal_or_none(ctx.get("markPx"))
            if price is not None:
                prices[str(asset["name"])] = price
        return prices
