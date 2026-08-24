"""Small Hyperliquid execution wrapper for capped live tests.

This module intentionally does nothing at import time. Instantiate
``HyperliquidExecutor`` from a strategy and call explicit order methods.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
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

        try:
            from hyperliquid.exchange import Exchange
            from hyperliquid.utils import constants
        except ModuleNotFoundError as exc:
            raise ModuleNotFoundError(
                "Official hyperliquid-python-sdk is not importable. Install the SDK in this Python env "
                "or run this executor on the server environment where it is installed."
            ) from exc

        base_url = constants.TESTNET_API_URL if testnet else constants.MAINNET_API_URL
        self.wallet = Account.from_key(self.private_key)
        self.exchange = Exchange(
            wallet=self.wallet,
            base_url=base_url,
            account_address=self.address,
            vault_address=vault_address,
        )
        self.info = self.exchange.info

    def get_positions(self) -> dict[str, Position]:
        """Return nonzero perp positions keyed by coin."""
        state = self.info.user_state(self.address)
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
        return self.get_positions().get(coin)

    def get_open_orders(self, coin: str | None = None) -> list[dict[str, Any]]:
        orders = self.info.open_orders(self.address)
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
            fills = self.info.user_fills(self.address, aggregate_by_time=aggregate_by_time)
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


def _float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
