#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import signal
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import websockets

from execution.unitree_lag_bot import (
    BookState,
    FillLogger,
    MarketDataLogger,
    PairConfig,
    TradeLogger,
    compute_signal,
    now_ms,
    utc_iso,
)


DEFAULT_HL_WS_URL = "wss://api.hyperliquid.xyz/ws"
DEFAULT_BINANCE_WS_BASE = "wss://fstream.binance.com/stream"


@dataclass
class ConnectionState:
    current_seq: int = 0

    def bump(self) -> int:
        self.current_seq += 1
        return self.current_seq


def hyperliquid_subscription_messages(target_coin: str) -> list[dict[str, Any]]:
    return [
        {"method": "subscribe", "subscription": {"type": "l2Book", "coin": target_coin}},
        {"method": "subscribe", "subscription": {"type": "activeAssetCtx", "coin": target_coin}},
    ]


def binance_stream_url(base_url: str, symbol: str) -> str:
    symbol_lower = symbol.lower()
    return f"{base_url}?streams={symbol_lower}@bookTicker/{symbol_lower}@markPrice@1s"


def apply_hyperliquid_message(message: dict[str, Any], target_coin: str, target_state: BookState) -> bool:
    channel = message.get("channel")
    data = message.get("data", {})
    if channel == "l2Book" and data.get("coin") == target_coin:
        levels = data.get("levels") or [[], []]
        target_state.bids = levels[0] or []
        target_state.asks = levels[1] or []
        target_state.book_time_ms = data.get("time")
        target_state.recv_time_ms = now_ms()
        return True
    if channel == "activeAssetCtx" and data.get("coin") == target_coin:
        target_state.ctx = data.get("ctx") or {}
        target_state.ctx_recv_time_ms = now_ms()
        return True
    return False


def apply_binance_message(message: dict[str, Any], reference_state: BookState) -> bool:
    data = message.get("data", {})
    event_type = data.get("e")
    if event_type == "bookTicker":
        bid_px = data.get("b")
        bid_sz = data.get("B")
        ask_px = data.get("a")
        ask_sz = data.get("A")
        if not all(value is not None for value in (bid_px, bid_sz, ask_px, ask_sz)):
            return False
        reference_state.bids = [{"px": str(bid_px), "sz": str(bid_sz)}]
        reference_state.asks = [{"px": str(ask_px), "sz": str(ask_sz)}]
        reference_state.book_time_ms = data.get("T") or data.get("E")
        reference_state.recv_time_ms = now_ms()
        return True
    if event_type == "markPriceUpdate":
        mark_px = data.get("p")
        oracle_px = data.get("i") or mark_px
        reference_state.ctx = {
            "markPx": "" if mark_px is None else str(mark_px),
            "oraclePx": "" if oracle_px is None else str(oracle_px),
            "midPx": "" if mark_px is None else str(mark_px),
        }
        reference_state.ctx_recv_time_ms = now_ms()
        return True
    return False


def signal_args(args: argparse.Namespace) -> argparse.Namespace:
    return argparse.Namespace(
        max_book_age_ms=args.max_book_age_ms,
        max_target_spread_bps=args.max_target_spread_bps,
        min_top_notional=args.min_top_notional,
        require_oracle_confirmation=args.require_oracle_confirmation,
        entry_oracle_gap_bps=args.entry_oracle_gap_bps,
    )


def log_error(
    logger: TradeLogger,
    pair: PairConfig,
    states: dict[str, BookState],
    reason: str,
    raw: dict[str, Any] | None = None,
) -> None:
    logger.write(
        event="error",
        reason=reason,
        pair=pair,
        signal=None,
        states=states,
        raw=raw,
    )


async def heartbeat(ws: websockets.WebSocketClientProtocol, interval_s: float) -> None:
    while True:
        await asyncio.sleep(interval_s)
        await ws.send(json.dumps({"method": "ping"}))


async def run_hyperliquid_stream(
    args: argparse.Namespace,
    pair: PairConfig,
    states: dict[str, BookState],
    logger: TradeLogger,
    stop: asyncio.Event,
    connections: ConnectionState,
) -> None:
    target_state = states[pair.target_coin]
    while not stop.is_set():
        try:
            seq = connections.bump()
            async with websockets.connect(args.hl_ws_url, ping_interval=None, close_timeout=5) as ws:
                for payload in hyperliquid_subscription_messages(pair.target_coin):
                    await ws.send(json.dumps(payload))
                ping_task = asyncio.create_task(heartbeat(ws, args.ping_interval_s))
                print(f"[{utc_iso()}] hyperliquid connected connection_seq={seq}", flush=True)
                try:
                    while not stop.is_set():
                        raw = await asyncio.wait_for(ws.recv(), timeout=args.recv_timeout_s)
                        apply_hyperliquid_message(json.loads(raw), pair.target_coin, target_state)
                finally:
                    ping_task.cancel()
                    try:
                        await ping_task
                    except asyncio.CancelledError:
                        pass
        except asyncio.TimeoutError:
            log_error(logger, pair, states, "hyperliquid recv timeout", {"source": "hyperliquid"})
            print(f"[{utc_iso()}] hyperliquid timeout; reconnecting in {args.reconnect_delay_s}s", flush=True)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log_error(logger, pair, states, f"hyperliquid: {exc}", {"source": "hyperliquid", "error": str(exc)})
            print(
                f"[{utc_iso()}] hyperliquid error: {exc}; reconnecting in {args.reconnect_delay_s}s",
                flush=True,
            )
        if not stop.is_set():
            await asyncio.sleep(args.reconnect_delay_s)


async def run_binance_stream(
    args: argparse.Namespace,
    pair: PairConfig,
    states: dict[str, BookState],
    logger: TradeLogger,
    stop: asyncio.Event,
    connections: ConnectionState,
) -> None:
    reference_state = states[pair.reference_coin]
    ws_url = binance_stream_url(args.binance_ws_base, args.binance_symbol)
    while not stop.is_set():
        try:
            seq = connections.bump()
            async with websockets.connect(ws_url, ping_interval=20, close_timeout=5) as ws:
                print(f"[{utc_iso()}] binance connected connection_seq={seq}", flush=True)
                while not stop.is_set():
                    raw = await asyncio.wait_for(ws.recv(), timeout=args.recv_timeout_s)
                    apply_binance_message(json.loads(raw), reference_state)
        except asyncio.TimeoutError:
            log_error(logger, pair, states, "binance recv timeout", {"source": "binance"})
            print(f"[{utc_iso()}] binance timeout; reconnecting in {args.reconnect_delay_s}s", flush=True)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log_error(logger, pair, states, f"binance: {exc}", {"source": "binance", "error": str(exc)})
            print(f"[{utc_iso()}] binance error: {exc}; reconnecting in {args.reconnect_delay_s}s", flush=True)
        if not stop.is_set():
            await asyncio.sleep(args.reconnect_delay_s)


async def run_snapshot_loop(
    args: argparse.Namespace,
    pair: PairConfig,
    states: dict[str, BookState],
    logger: MarketDataLogger,
    stop: asyncio.Event,
    connections: ConnectionState,
) -> None:
    last_market_log_ms = 0
    compute_args = signal_args(args)
    while not stop.is_set():
        await asyncio.sleep(0.05)
        if args.market_log_interval_ms <= 0:
            continue
        current_ms = now_ms()
        if current_ms - last_market_log_ms < args.market_log_interval_ms:
            continue
        signal_value = compute_signal(pair, states, compute_args)
        logger.write(
            event="snapshot",
            pair=pair,
            signal=signal_value,
            states=states,
            position=None,
            max_book_age_ms=args.max_book_age_ms,
            max_cross_recv_skew_ms=args.max_cross_recv_skew_ms,
            connection_seq=connections.current_seq,
        )
        last_market_log_ms = current_ms


async def run_collector(args: argparse.Namespace) -> int:
    pair = PairConfig(
        target_coin=args.target_coin,
        reference_coin=f"binance:{args.binance_symbol}",
        entry_edge_bps=args.entry_edge_bps,
        reference_scale=args.reference_scale,
    )
    states = {
        pair.target_coin: BookState(pair.target_coin),
        pair.reference_coin: BookState(pair.reference_coin),
    }
    connections = ConnectionState()
    trade_logger = TradeLogger(args.log, dry_run=True)
    market_logger = MarketDataLogger(args.market_log, dry_run=True)
    fill_logger = FillLogger(args.fill_log, dry_run=True, pair_by_target_coin={pair.target_coin: pair})
    stop = asyncio.Event()

    def request_stop() -> None:
        stop.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, request_stop)
        except NotImplementedError:
            pass
    if args.duration_s is not None:
        loop.call_later(args.duration_s, request_stop)

    print(
        f"mode=collect-only pair={pair.pair_id} entry_edge_bps={pair.entry_edge_bps} "
        f"max_book_age_ms={args.max_book_age_ms} max_cross_recv_skew_ms={args.max_cross_recv_skew_ms}",
        flush=True,
    )
    print(
        f"market_log={args.market_log} events_log={args.log} fill_log={args.fill_log}",
        flush=True,
    )

    tasks = [
        asyncio.create_task(run_hyperliquid_stream(args, pair, states, trade_logger, stop, connections)),
        asyncio.create_task(run_binance_stream(args, pair, states, trade_logger, stop, connections)),
        asyncio.create_task(run_snapshot_loop(args, pair, states, market_logger, stop, connections)),
    ]
    try:
        await stop.wait()
    finally:
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
        trade_logger.close()
        market_logger.close()
        fill_logger.close()
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Collect stale-protected divergence snapshots for one Hyperliquid/Binance futures pair."
    )
    parser.add_argument("--target-coin", default="io:OAI", help="Hyperliquid target coin, default: io:OAI")
    parser.add_argument("--binance-symbol", default="OPENAIUSDT", help="Binance futures symbol, default: OPENAIUSDT")
    parser.add_argument("--reference-scale", type=float, default=1.0, help="Optional multiplier for the Binance reference")
    parser.add_argument("--log", type=Path, required=True, help="CSV event log path")
    parser.add_argument("--market-log", type=Path, required=True, help="CSV market snapshot path")
    parser.add_argument("--fill-log", type=Path, required=True, help="CSV fill log path")
    parser.add_argument("--entry-edge-bps", type=float, default=50.0, help="Signal threshold used for gross_edge_bps labeling")
    parser.add_argument("--entry-oracle-gap-bps", type=float, default=75.0, help="Required oracle confirmation size")
    parser.add_argument(
        "--no-oracle-confirmation",
        dest="require_oracle_confirmation",
        action="store_false",
        help="Do not require oracle gap direction confirmation for gross_edge_bps labeling",
    )
    parser.add_argument("--min-top-notional", type=float, default=80.0, help="Minimum top-book notional on both legs")
    parser.add_argument("--max-book-age-ms", type=int, default=750, help="Max age for target/reference books")
    parser.add_argument(
        "--max-cross-recv-skew-ms",
        type=int,
        default=250,
        help="Max allowed cross-venue receive-time skew for pair synchronization labels",
    )
    parser.add_argument("--max-target-spread-bps", type=float, default=25.0, help="Max target top-of-book spread")
    parser.add_argument("--market-log-interval-ms", type=int, default=500, help="How often to persist market-state snapshots")
    parser.add_argument("--recv-timeout-s", type=float, default=75.0)
    parser.add_argument("--ping-interval-s", type=float, default=30.0)
    parser.add_argument("--reconnect-delay-s", type=float, default=3.0)
    parser.add_argument("--duration-s", type=float, default=None, help="Optional run duration")
    parser.add_argument("--hl-ws-url", default=DEFAULT_HL_WS_URL)
    parser.add_argument("--binance-ws-base", default=DEFAULT_BINANCE_WS_BASE)
    parser.set_defaults(require_oracle_confirmation=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    return asyncio.run(run_collector(args))


if __name__ == "__main__":
    raise SystemExit(main())
