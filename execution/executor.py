"""Small Hyperliquid execution wrapper for capped live tests.

This module intentionally does nothing at import time. Instantiate
``HyperliquidExecutor`` from a strategy and call explicit order methods.
"""

from __future__ import annotations

import http.client
import json
import os
import socket
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from json import JSONDecodeError
from typing import Any, Literal

from dotenv import load_dotenv
from eth_account import Account


TimeInForce = Literal["Ioc", "Gtc", "Alo"]


@dataclass(frozen=True)
class Position:
    coin: str
    size: float
    entry_px: float | None
    unrealized_pnl: float | None
    liquidation_px: float | None
    raw: dict[str, Any]


class HyperliquidExecutor:
    """Thin adapter around the official Hyperliquid Python SDK.

    The strategy should do its own signal/risk checks and pass already-rounded
    prices/sizes into this class.
    """

    def __init__(
        self,
        *,
        testnet: bool = True,
        target_coin: str | None = None,
        timeout_s: float | None = None,
        private_key_env: str = "PK",
        address_env: str = "ADDRESS",
        vault_address: str | None = None,
    ) -> None:
        load_dotenv()
        self.private_key = os.getenv(private_key_env)
        self.address = os.getenv(address_env)
        self.vault_address = vault_address

        if not self.private_key or not self.address:
            raise ValueError(f"Missing {private_key_env} or {address_env} in environment/.env")

        self.target_coin = target_coin
        self.default_perp_dex = _coin_dex(target_coin)
        try:
            from hyperliquid.api import API
            from hyperliquid.exchange import Exchange
            from hyperliquid.utils import constants
            from hyperliquid.utils.error import ClientError, ServerError
        except ModuleNotFoundError as exc:
            raise ModuleNotFoundError(
                "Official hyperliquid-python-sdk is not importable. Install the SDK in this Python env "
                "or run this executor on the server environment where it is installed."
            ) from exc

        _install_hyperliquid_post_fallback(API, ClientError, ServerError)
        base_url = constants.TESTNET_API_URL if testnet else constants.MAINNET_API_URL
        self.wallet = Account.from_key(self.private_key)
        exchange_kwargs: dict[str, Any] = {
            "wallet": self.wallet,
            "base_url": base_url,
            "account_address": self.address,
            "vault_address": vault_address,
            "timeout": timeout_s,
        }
        if self.default_perp_dex is not None:
            exchange_kwargs["perp_dexs"] = [self.default_perp_dex]
        self.exchange = Exchange(
            **exchange_kwargs,
        )
        self.info = self.exchange.info

    def get_positions(self, coin: str | None = None) -> dict[str, Position]:
        """Return nonzero perp positions keyed by coin."""
        state = self.info.user_state(self.address, dex=self._resolve_dex(coin))
        positions: dict[str, Position] = {}
        for item in state.get("assetPositions", []):
            raw_position = item.get("position", {})
            coin = raw_position.get("coin")
            size = _float(raw_position.get("szi")) or 0.0
            if not coin or size == 0:
                continue
            positions[coin] = Position(
                coin=coin,
                size=size,
                entry_px=_float(raw_position.get("entryPx")),
                unrealized_pnl=_float(raw_position.get("unrealizedPnl")),
                liquidation_px=_float(raw_position.get("liquidationPx")),
                raw=raw_position,
            )
        return positions

    def get_position(self, coin: str) -> Position | None:
        return self.get_positions(coin).get(coin)

    def get_open_orders(self, coin: str | None = None) -> list[dict[str, Any]]:
        orders = self.info.open_orders(self.address, dex=self._resolve_dex(coin))
        if coin is None:
            return orders
        return [order for order in orders if order.get("coin") == coin]

    def get_recent_fills(
        self,
        *,
        coin: str | None = None,
        start_time_ms: int | None = None,
        aggregate_by_time: bool = False,
    ) -> list[dict[str, Any]]:
        if start_time_ms is None:
            fills = self.info.user_fills(self.address)
        else:
            fills = self.info.user_fills_by_time(
                self.address,
                start_time_ms,
                aggregate_by_time=aggregate_by_time,
            )
        if coin is None:
            return fills
        return [fill for fill in fills if fill.get("coin") == coin]

    def place_limit_order(
        self,
        *,
        coin: str,
        is_buy: bool,
        size: float,
        limit_px: float,
        tif: TimeInForce = "Ioc",
        reduce_only: bool = False,
        cloid: Any | None = None,
    ) -> dict[str, Any]:
        """Place a protected limit order.

        Use ``tif="Ioc"`` for taker entries/exits with price protection.
        Use ``reduce_only=True`` for exits.
        """
        order_type = {"limit": {"tif": tif}}
        return self.exchange.order(
            name=coin,
            is_buy=is_buy,
            sz=size,
            limit_px=limit_px,
            order_type=order_type,
            reduce_only=reduce_only,
            cloid=cloid,
        )

    def enter_ioc(
        self,
        *,
        coin: str,
        is_buy: bool,
        size: float,
        limit_px: float,
        cloid: Any | None = None,
    ) -> dict[str, Any]:
        return self.place_limit_order(
            coin=coin,
            is_buy=is_buy,
            size=size,
            limit_px=limit_px,
            tif="Ioc",
            reduce_only=False,
            cloid=cloid,
        )

    def exit_reduce_only_ioc(
        self,
        *,
        coin: str,
        is_buy: bool,
        size: float,
        limit_px: float,
        cloid: Any | None = None,
    ) -> dict[str, Any]:
        return self.place_limit_order(
            coin=coin,
            is_buy=is_buy,
            size=size,
            limit_px=limit_px,
            tif="Ioc",
            reduce_only=True,
            cloid=cloid,
        )

    def cancel_order(self, *, coin: str, oid: int) -> dict[str, Any]:
        return self.exchange.cancel(coin, oid)

    def cancel_order_by_cloid(self, *, coin: str, cloid: Any) -> dict[str, Any]:
        return self.exchange.cancel_by_cloid(coin, cloid)

    def cancel_all_for_coin(self, coin: str) -> list[dict[str, Any]]:
        results = []
        for order in self.get_open_orders(coin):
            oid = order.get("oid")
            if oid is not None:
                results.append(self.cancel_order(coin=coin, oid=int(oid)))
        return results

    def query_order_status(self, oid_or_cloid: int | str) -> dict[str, Any]:
        return self.info.query_order_by_oid(self.address, oid_or_cloid)

    def schedule_cancel_all(self, *, ms_from_now: int = 5_000) -> dict[str, Any]:
        """Set Hyperliquid's dead-man switch cancel time."""
        cancel_time = int(time.time() * 1000) + ms_from_now
        return self.exchange.schedule_cancel(cancel_time)

    def clear_scheduled_cancel(self) -> dict[str, Any]:
        return self.exchange.schedule_cancel(None)

    def set_leverage(self, *, coin: str, leverage: int, cross: bool = False) -> dict[str, Any]:
        return self.exchange.update_leverage(leverage, coin, is_cross=cross)

    def _resolve_dex(self, coin: str | None = None) -> str:
        dex = _coin_dex(coin) or self.default_perp_dex
        return "" if dex is None else dex


def _float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _coin_dex(coin: str | None) -> str | None:
    if not coin or ":" not in coin:
        return None
    dex, _symbol = coin.split(":", 1)
    return dex or None


def _install_hyperliquid_post_fallback(api_cls: type, client_error_cls: type[Exception], server_error_cls: type[Exception]) -> None:
    if getattr(api_cls, "_codex_urllib_fallback_installed", False):
        return

    def post_via_urllib(self: Any, url_path: str, payload: Any = None) -> Any:
        payload = payload or {}
        url = self.base_url + url_path
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "User-Agent": "arbitrage-on-xyz/1.0"},
            method="POST",
        )
        for attempt in range(1, 4):
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    text = response.read().decode("utf-8")
                    try:
                        return json.loads(text)
                    except ValueError:
                        return {"error": f"Could not parse JSON: {text}"}
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", errors="replace")
                if exc.code >= 500 and attempt < 3:
                    time.sleep(0.25 * attempt)
                    continue
                if 400 <= exc.code < 500:
                    try:
                        err = json.loads(body)
                    except JSONDecodeError as parse_exc:
                        raise client_error_cls(exc.code, None, body, None, exc.headers) from parse_exc
                    if err is None:
                        raise client_error_cls(exc.code, None, body, None, exc.headers)
                    error_data = err.get("data")
                    raise client_error_cls(exc.code, err.get("code"), err.get("msg", body), exc.headers, error_data)
                raise server_error_cls(exc.code, body)
            except (
                urllib.error.URLError,
                http.client.RemoteDisconnected,
                ConnectionResetError,
                TimeoutError,
                socket.timeout,
                ssl.SSLError,
            ) as exc:
                if attempt < 3:
                    time.sleep(0.25 * attempt)
                    continue
                reason = exc.reason if isinstance(exc, urllib.error.URLError) else str(exc)
                raise RuntimeError(f"Hyperliquid API request failed: {reason}") from exc

    api_cls.post = post_via_urllib
    api_cls._codex_urllib_fallback_installed = True
