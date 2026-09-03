from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from secrets import token_hex
from typing import Any


INFO_URL = "https://api.hyperliquid.xyz/info"


def now_ms() -> int:
    return time.time_ns() // 1_000_000


def utc_iso(timestamp_ms: int | None = None) -> str:
    if timestamp_ms is None:
        timestamp_ms = now_ms()
    return datetime.fromtimestamp(timestamp_ms / 1000, tz=UTC).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def to_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def safe_json(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), default=str)


def decimal_places(raw_px: str | None) -> int:
    if not raw_px or "." not in raw_px:
        return 0
    return len(raw_px.rstrip("0").split(".", 1)[1])


def decimal_places_for_float(value: float) -> int:
    normalized = format(value, "f")
    if "." not in normalized:
        return 0
    return len(normalized.rstrip("0").split(".", 1)[1])


def price_decimals_from_values(*raw_prices: str) -> int:
    decimals = [decimal_places(raw) for raw in raw_prices if raw]
    return max(decimals, default=0)


def tick_size_from_decimals(decimals: int) -> float:
    quant = Decimal("1") if decimals <= 0 else Decimal("1").scaleb(-decimals)
    return float(quant)


def round_down(value: float, decimals: int) -> float:
    quant = Decimal("1") if decimals <= 0 else Decimal("1").scaleb(-decimals)
    return float(Decimal(str(value)).quantize(quant, rounding=ROUND_DOWN))


def round_up(value: float, decimals: int) -> float:
    quant = Decimal("1") if decimals <= 0 else Decimal("1").scaleb(-decimals)
    return float(Decimal(str(value)).quantize(quant, rounding=ROUND_UP))


def round_price(value: float, decimals: int, side: str) -> float:
    if side == "bid":
        return round_down(value, decimals)
    return round_up(value, decimals)


def bps_diff(lhs: float | None, rhs: float | None) -> float | None:
    if lhs is None or rhs is None or rhs == 0:
        return None
    return (lhs - rhs) / rhs * 10_000


def bps_ratio(lhs: float | None, rhs: float | None) -> float | None:
    if lhs is None or rhs is None or rhs == 0:
        return None
    return (lhs / rhs - 1.0) * 10_000


def signed_edge_bps(fill_px: float | None, reference_px: float | None, is_buy: bool) -> float | None:
    if fill_px is None or reference_px is None or fill_px == 0:
        return None
    if is_buy:
        return (reference_px - fill_px) / fill_px * 10_000
    return (fill_px - reference_px) / fill_px * 10_000


def side_to_is_buy(side: str | None) -> bool | None:
    if side is None:
        return None
    normalized = side.strip().lower()
    if normalized in {"b", "buy", "bid", "long"}:
        return True
    if normalized in {"a", "ask", "sell", "short"}:
        return False
    return None


def cloid_from_str(raw: str) -> Any:
    try:
        from hyperliquid.utils.types import Cloid
    except ModuleNotFoundError:
        return raw
    return Cloid.from_str(raw)


def generate_cloid() -> Any:
    return cloid_from_str(f"0x{token_hex(16)}")


def post_info(payload: dict[str, Any], timeout: float) -> Any:
    request = urllib.request.Request(
        INFO_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": "oai-mm/1.0"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Hyperliquid info HTTP {exc.code}: {body}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"could not reach Hyperliquid info API: {exc.reason}") from exc


def load_size_decimals(coin: str, timeout: float) -> int:
    dex = coin.split(":", 1)[0] if ":" in coin else None
    payload: dict[str, Any] = {"type": "metaAndAssetCtxs"}
    if dex:
        payload["dex"] = dex
    meta, _ctxs = post_info(payload, timeout)
    for asset in meta.get("universe", []):
        if asset.get("name") == coin and not asset.get("isDelisted"):
            return int(asset.get("szDecimals", 0))
    raise RuntimeError(f"{coin} is missing or delisted in metaAndAssetCtxs")
