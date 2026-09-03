from __future__ import annotations

import asyncio
import json
from typing import Any

import websockets

from .config import MMConfig
from .models import Level, TradeTick, VenueBook, VenueState
from .utils import now_ms, to_float, utc_iso


def hyperliquid_subscription_messages(target_coin: str, user_address: str | None = None) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = [
        {"method": "subscribe", "subscription": {"type": "l2Book", "coin": target_coin}},
        {"method": "subscribe", "subscription": {"type": "activeAssetCtx", "coin": target_coin}},
        {"method": "subscribe", "subscription": {"type": "trades", "coin": target_coin}},
    ]
    if user_address:
        messages.append({"method": "subscribe", "subscription": {"type": "userFills", "user": user_address}})
        messages.append({"method": "subscribe", "subscription": {"type": "orderUpdates", "user": user_address}})
    return messages


def _levels_from_l2(levels: list[dict[str, Any]]) -> list[Level]:
    parsed: list[Level] = []
    for level in levels:
        px = to_float(level.get("px"))
        sz = to_float(level.get("sz"))
        if px is None or sz is None:
            continue
        parsed.append(Level(px=px, sz=sz, raw_px=str(level.get("px", "")), raw_sz=str(level.get("sz", ""))))
    return parsed


def apply_hyperliquid_message(message: dict[str, Any], state: VenueState) -> tuple[str | None, list[dict[str, Any]]]:
    channel = message.get("channel")
    data = message.get("data", {})
    recv_ms = now_ms()
    state.last_message_ms = recv_ms

    if channel == "l2Book" and data.get("coin") == state.symbol:
        levels = data.get("levels") or [[], []]
        state.book = VenueBook(
            bids=_levels_from_l2(levels[0]),
            asks=_levels_from_l2(levels[1]),
            exchange_time_ms=data.get("time"),
            recv_time_ms=recv_ms,
            source="l2Book",
        )
        return "hl_book", []

    if channel == "activeAssetCtx" and data.get("coin") == state.symbol:
        ctx = data.get("ctx") or {}
        state.mark_px = to_float(ctx.get("markPx"))
        state.oracle_px = to_float(ctx.get("oraclePx"))
        return "hl_ctx", []

    if channel == "trades":
        trades = data if isinstance(data, list) else []
        for trade in reversed(trades):
            if trade.get("coin") != state.symbol:
                continue
            px = to_float(trade.get("px"))
            sz = to_float(trade.get("sz"))
            if px is None or sz is None:
                continue
            trade_side = str(trade.get("side", "")).lower()
            side = "buy" if trade_side in {"b", "buy"} else "sell"
            state.last_trade = TradeTick(
                venue="hyperliquid",
                symbol=state.symbol,
                side=side,
                px=px,
                sz=sz,
                exchange_time_ms=trade.get("time"),
                recv_time_ms=recv_ms,
                trade_id=str(trade.get("hash", "")),
                raw=trade,
            )
            break
        return "hl_trade", []

    if channel == "userFills":
        fills = data.get("fills", []) if isinstance(data, dict) else []
        if isinstance(data, dict) and data.get("isSnapshot"):
            return "hl_user_fills_snapshot", []
        return "hl_user_fills", [fill for fill in fills if fill.get("coin") == state.symbol]

    if channel == "orderUpdates":
        return "hl_order_updates", data if isinstance(data, list) else []

    return None, []


class HyperliquidFeed:
    def __init__(
        self,
        config: MMConfig,
        state: VenueState,
        logger: Any,
        trigger: asyncio.Event,
        user_event_queue: asyncio.Queue[tuple[str, dict[str, Any]]],
        user_address: str | None = None,
    ) -> None:
        self.config = config
        self.state = state
        self.logger = logger
        self.trigger = trigger
        self.user_event_queue = user_event_queue
        self.user_address = user_address

    async def _heartbeat(self, ws: websockets.WebSocketClientProtocol) -> None:
        while True:
            await asyncio.sleep(self.config.ping_interval_s)
            await ws.send(json.dumps({"method": "ping"}))

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                self.state.connection_seq += 1
                async with websockets.connect(self.config.hl_ws_url, ping_interval=None, close_timeout=5) as ws:
                    for payload in hyperliquid_subscription_messages(self.config.target_coin, self.user_address):
                        await ws.send(json.dumps(payload))
                    ping_task = asyncio.create_task(self._heartbeat(ws))
                    self.state.connected = True
                    self.state.last_disconnect_reason = None
                    self.logger.log_event("hl_connected")
                    print(f"[{utc_iso()}] hyperliquid connected connection_seq={self.state.connection_seq}", flush=True)
                    try:
                        while not stop.is_set():
                            raw = await asyncio.wait_for(ws.recv(), timeout=self.config.recv_timeout_s)
                            event_name, queue_items = apply_hyperliquid_message(json.loads(raw), self.state)
                            if event_name is not None:
                                self.logger.log_event(event_name)
                                for item in queue_items:
                                    await self.user_event_queue.put((event_name, item))
                                self.trigger.set()
                    finally:
                        ping_task.cancel()
                        try:
                            await ping_task
                        except asyncio.CancelledError:
                            pass
            except asyncio.TimeoutError:
                self.state.connected = False
                self.state.last_disconnect_reason = "recv_timeout"
                self.logger.log_event("hl_timeout")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.state.connected = False
                self.state.last_disconnect_reason = str(exc)
                self.logger.log_event("hl_error", reason=str(exc), raw={"error": str(exc)})
                print(f"[{utc_iso()}] hyperliquid error: {exc}; reconnecting in {self.config.reconnect_delay_s}s", flush=True)
            finally:
                if self.state.connected and not stop.is_set():
                    self.logger.log_event("hl_disconnected", reason=self.state.last_disconnect_reason)
                self.state.connected = False
                self.trigger.set()
            if not stop.is_set():
                await asyncio.sleep(self.config.reconnect_delay_s)
