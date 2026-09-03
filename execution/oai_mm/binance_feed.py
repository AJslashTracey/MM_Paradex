from __future__ import annotations

import asyncio
import json
from typing import Any

import websockets

from .config import MMConfig
from .models import Level, TradeTick, VenueBook, VenueState
from .utils import now_ms, to_float, utc_iso


def binance_stream_url(base_url: str, symbol: str, depth_stream: str) -> str:
    symbol_lower = symbol.lower()
    return (
        f"{base_url}?streams="
        f"{symbol_lower}@bookTicker/{symbol_lower}@markPrice@1s/{symbol_lower}@aggTrade/{symbol_lower}@{depth_stream}"
    )


def _levels_from_pairs(levels: list[list[str]]) -> list[Level]:
    parsed: list[Level] = []
    for raw_px, raw_sz, *_rest in levels:
        px = to_float(raw_px)
        sz = to_float(raw_sz)
        if px is None or sz is None:
            continue
        parsed.append(Level(px=px, sz=sz, raw_px=str(raw_px), raw_sz=str(raw_sz)))
    return parsed


def apply_binance_message(message: dict[str, Any], state: VenueState) -> str | None:
    data = message.get("data", {})
    event_type = data.get("e")
    recv_ms = now_ms()
    state.last_message_ms = recv_ms

    if event_type == "bookTicker":
        bid_px = data.get("b")
        bid_sz = data.get("B")
        ask_px = data.get("a")
        ask_sz = data.get("A")
        if not all(value is not None for value in (bid_px, bid_sz, ask_px, ask_sz)):
            return None
        bids = [Level(px=float(bid_px), sz=float(bid_sz), raw_px=str(bid_px), raw_sz=str(bid_sz))]
        asks = [Level(px=float(ask_px), sz=float(ask_sz), raw_px=str(ask_px), raw_sz=str(ask_sz))]
        if state.book is not None and len(state.book.bids) > 1:
            bids.extend(state.book.bids[1:])
        if state.book is not None and len(state.book.asks) > 1:
            asks.extend(state.book.asks[1:])
        state.book = VenueBook(
            bids=bids,
            asks=asks,
            exchange_time_ms=data.get("T") or data.get("E"),
            recv_time_ms=recv_ms,
            source="bookTicker",
        )
        return "binance_book"

    if event_type == "depthUpdate":
        bids = _levels_from_pairs(data.get("b", []))
        asks = _levels_from_pairs(data.get("a", []))
        if not bids or not asks:
            return None
        state.book = VenueBook(
            bids=bids,
            asks=asks,
            exchange_time_ms=data.get("T") or data.get("E"),
            recv_time_ms=recv_ms,
            source="depth",
        )
        return "binance_depth"

    if event_type == "markPriceUpdate":
        state.mark_px = to_float(data.get("p"))
        state.oracle_px = to_float(data.get("i")) or state.mark_px
        return "binance_mark"

    if event_type == "aggTrade":
        px = to_float(data.get("p"))
        sz = to_float(data.get("q"))
        if px is None or sz is None:
            return None
        state.last_trade = TradeTick(
            venue="binance",
            symbol=state.symbol,
            side="sell" if data.get("m") else "buy",
            px=px,
            sz=sz,
            exchange_time_ms=data.get("T") or data.get("E"),
            recv_time_ms=recv_ms,
            trade_id=str(data.get("a", "")),
            raw=data,
        )
        return "binance_trade"

    return None


class BinanceFeed:
    def __init__(self, config: MMConfig, state: VenueState, logger: Any, trigger: asyncio.Event) -> None:
        self.config = config
        self.state = state
        self.logger = logger
        self.trigger = trigger

    async def run(self, stop: asyncio.Event) -> None:
        ws_url = binance_stream_url(self.config.binance_ws_base, self.config.binance_symbol, self.config.binance_depth_stream)
        while not stop.is_set():
            try:
                self.state.connection_seq += 1
                async with websockets.connect(ws_url, ping_interval=20, close_timeout=5) as ws:
                    self.state.connected = True
                    self.state.last_disconnect_reason = None
                    self.logger.log_event("binance_connected")
                    print(f"[{utc_iso()}] binance connected connection_seq={self.state.connection_seq}", flush=True)
                    while not stop.is_set():
                        raw = await asyncio.wait_for(ws.recv(), timeout=self.config.recv_timeout_s)
                        event_name = apply_binance_message(json.loads(raw), self.state)
                        if event_name is not None:
                            self.logger.log_event(event_name)
                            self.trigger.set()
            except asyncio.TimeoutError:
                self.state.connected = False
                self.state.last_disconnect_reason = "recv_timeout"
                self.logger.log_event("binance_timeout")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.state.connected = False
                self.state.last_disconnect_reason = str(exc)
                self.logger.log_event("binance_error", reason=str(exc), raw={"error": str(exc)})
                print(f"[{utc_iso()}] binance error: {exc}; reconnecting in {self.config.reconnect_delay_s}s", flush=True)
            finally:
                if self.state.connected and not stop.is_set():
                    self.logger.log_event("binance_disconnected", reason=self.state.last_disconnect_reason)
                self.state.connected = False
                self.trigger.set()
            if not stop.is_set():
                await asyncio.sleep(self.config.reconnect_delay_s)
