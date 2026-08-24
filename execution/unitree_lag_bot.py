#!/usr/bin/env python3
"""Tiny unhedged HIP-3 lag execution bot for para:UNITREE vs xyz:UNITREE.

Default mode is dry-run. Pass ``--live`` to send real Hyperliquid orders.
The bot uses IOC limit entries and reduce-only IOC limit exits.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import math
import signal
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from typing import Any, Literal

import websockets

try:
    from .executor import HyperliquidExecutor
except ImportError:
    from executor import HyperliquidExecutor


INFO_URL = "https://api.hyperliquid.xyz/info"
WS_URL = "wss://api.hyperliquid.xyz/ws"
TARGET_COIN = "para:UNITREE"
REFERENCE_COIN = "xyz:UNITREE"
DEFAULT_LOG = Path("execution/unitree_lag_bot_trades.csv")


Side = Literal["long", "short"]


@dataclass
class BookState:
    coin: str
    book_time_ms: int | None = None
    recv_time_ms: int | None = None
    bids: list[dict[str, str]] | None = None
    asks: list[dict[str, str]] | None = None
    ctx: dict[str, Any] | None = None
    ctx_recv_time_ms: int | None = None


@dataclass
class BotPosition:
    side: Side
    size: float
    entry_px: float
    entry_time_ms: int
    entry_signal_id: str
    live_position: bool


@dataclass
class Signal:
    side: Side
    gross_edge_bps: float
    oracle_gap_bps: float | None
    entry_px: float
    exit_px_now: float
    top_notional: float
    target_spread_bps: float
    target_book_age_ms: int
    reference_book_age_ms: int


FIELDS = [
    "time_utc",
    "event",
    "mode",
    "side",
    "coin",
    "size",
    "price",
    "order_notional",
    "gross_edge_bps",
    "oracle_gap_bps",
    "pnl_bps",
    "reason",
    "target_bid",
    "target_ask",
    "target_mid",
    "reference_bid",
    "reference_ask",
    "reference_mid",
    "target_oracle",
    "target_spread_bps",
    "target_book_age_ms",
    "reference_book_age_ms",
    "raw_json",
]


def now_ms() -> int:
    return int(time.time() * 1000)


def utc_iso(ms: int | None = None) -> str:
    value = now_ms() if ms is None else ms
    return (
        datetime.fromtimestamp(value / 1000, tz=timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def best_bid(state: BookState) -> dict[str, str] | None:
    return state.bids[0] if state.bids else None


def best_ask(state: BookState) -> dict[str, str] | None:
    return state.asks[0] if state.asks else None


def mid(state: BookState) -> float | None:
    bid = to_float(best_bid(state).get("px")) if best_bid(state) else None
    ask = to_float(best_ask(state).get("px")) if best_ask(state) else None
    if bid is not None and ask is not None and bid > 0 and ask > 0:
        return (bid + ask) / 2
    ctx = state.ctx or {}
    return to_float(ctx.get("midPx")) or to_float(ctx.get("markPx"))


def bps(diff: float | None, denom: float | None) -> float | None:
    if diff is None or denom is None or denom == 0:
        return None
    return diff / denom * 10_000


def decimal_places(raw_px: str | None) -> int:
    if not raw_px or "." not in raw_px:
        return 0
    return len(raw_px.rstrip("0").split(".", 1)[1])


def round_down(value: float, decimals: int) -> float:
    quant = Decimal("1") if decimals <= 0 else Decimal("1").scaleb(-decimals)
    return float(Decimal(str(value)).quantize(quant, rounding=ROUND_DOWN))


def protected_price(raw_top_px: str, is_buy: bool, protection_bps: float) -> float:
    top = Decimal(raw_top_px)
    if protection_bps <= 0:
        return float(top)
    multiplier = Decimal("1") + (Decimal(str(protection_bps)) / Decimal("10000"))
    if not is_buy:
        multiplier = Decimal("1") - (Decimal(str(protection_bps)) / Decimal("10000"))
    decimals = decimal_places(raw_top_px)
    quant = Decimal("1") if decimals <= 0 else Decimal("1").scaleb(-decimals)
    rounding = ROUND_DOWN
    return float((top * multiplier).quantize(quant, rounding=rounding))


def post_info(payload: dict[str, Any], timeout: float) -> Any:
    request = urllib.request.Request(
        INFO_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": "unitree-lag-bot/1.0"},
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


class TradeLogger:
    def __init__(self, path: Path, dry_run: bool) -> None:
        self.path = path
        self.dry_run = dry_run
        self.path.parent.mkdir(parents=True, exist_ok=True)
        needs_header = not self.path.exists() or self.path.stat().st_size == 0
        self.file = self.path.open("a", newline="", encoding="utf-8")
        self.writer = csv.DictWriter(self.file, fieldnames=FIELDS)
        if needs_header:
            self.writer.writeheader()
            self.file.flush()

    def close(self) -> None:
        self.file.close()

    def write(
        self,
        *,
        event: str,
        reason: str,
        signal: Signal | None,
        states: dict[str, BookState],
        side: str = "",
        size: float | None = None,
        price: float | None = None,
        pnl_bps_value: float | None = None,
        raw: Any | None = None,
    ) -> None:
        target = states[TARGET_COIN]
        reference = states[REFERENCE_COIN]
        self.writer.writerow(
            {
                "time_utc": utc_iso(),
                "event": event,
                "mode": "dry_run" if self.dry_run else "live",
                "side": side or (signal.side if signal else ""),
                "coin": TARGET_COIN,
                "size": "" if size is None else f"{size:.10f}",
                "price": "" if price is None else f"{price:.10f}",
                "order_notional": "" if size is None or price is None else f"{size * price:.6f}",
                "gross_edge_bps": "" if signal is None else f"{signal.gross_edge_bps:.6f}",
                "oracle_gap_bps": "" if signal is None or signal.oracle_gap_bps is None else f"{signal.oracle_gap_bps:.6f}",
                "pnl_bps": "" if pnl_bps_value is None else f"{pnl_bps_value:.6f}",
                "reason": reason,
                "target_bid": px(best_bid(target)),
                "target_ask": px(best_ask(target)),
                "target_mid": fmt(mid(target)),
                "reference_bid": px(best_bid(reference)),
                "reference_ask": px(best_ask(reference)),
                "reference_mid": fmt(mid(reference)),
                "target_oracle": (target.ctx or {}).get("oraclePx", ""),
                "target_spread_bps": "" if signal is None else f"{signal.target_spread_bps:.6f}",
                "target_book_age_ms": "" if signal is None else signal.target_book_age_ms,
                "reference_book_age_ms": "" if signal is None else signal.reference_book_age_ms,
                "raw_json": json.dumps(raw, separators=(",", ":"), default=str) if raw is not None else "",
            }
        )
        self.file.flush()


def px(level: dict[str, str] | None) -> str:
    return "" if not level else str(level.get("px", ""))


def fmt(value: float | None) -> str:
    return "" if value is None else f"{value:.10f}"


def compute_signal(states: dict[str, BookState], args: argparse.Namespace) -> Signal | None:
    target = states[TARGET_COIN]
    reference = states[REFERENCE_COIN]
    target_bid = best_bid(target)
    target_ask = best_ask(target)
    reference_bid = best_bid(reference)
    reference_ask = best_ask(reference)
    target_mid = mid(target)
    reference_mid = mid(reference)
    received_ms = now_ms()

    if not all([target_bid, target_ask, reference_bid, reference_ask, target_mid, reference_mid]):
        return None
    if target.book_time_ms is None or reference.book_time_ms is None:
        return None

    target_bid_px = float(target_bid["px"])
    target_ask_px = float(target_ask["px"])
    reference_bid_px = float(reference_bid["px"])
    reference_ask_px = float(reference_ask["px"])
    target_book_age_ms = received_ms - target.book_time_ms
    reference_book_age_ms = received_ms - reference.book_time_ms
    target_spread_bps = (target_ask_px - target_bid_px) / target_mid * 10_000
    target_oracle = to_float((target.ctx or {}).get("oraclePx"))
    oracle_gap = bps(target_oracle - reference_mid if target_oracle is not None else None, reference_mid)

    long_edge = (reference_bid_px - target_ask_px) / reference_mid * 10_000
    short_edge = (target_bid_px - reference_ask_px) / reference_mid * 10_000
    long_notional = min(target_ask_px * float(target_ask["sz"]), reference_bid_px * float(reference_bid["sz"]))
    short_notional = min(target_bid_px * float(target_bid["sz"]), reference_ask_px * float(reference_ask["sz"]))

    common_filters_ok = (
        target_book_age_ms <= args.max_book_age_ms
        and reference_book_age_ms <= args.max_book_age_ms
        and target_spread_bps <= args.max_target_spread_bps
    )
    if not common_filters_ok:
        return None

    if (
        long_edge >= args.entry_edge_bps
        and long_notional >= args.min_top_notional
        and (not args.require_oracle_confirmation or (oracle_gap is not None and oracle_gap <= -args.entry_oracle_gap_bps))
    ):
        return Signal(
            side="long",
            gross_edge_bps=long_edge,
            oracle_gap_bps=oracle_gap,
            entry_px=target_ask_px,
            exit_px_now=target_bid_px,
            top_notional=long_notional,
            target_spread_bps=target_spread_bps,
            target_book_age_ms=target_book_age_ms,
            reference_book_age_ms=reference_book_age_ms,
        )

    if (
        short_edge >= args.entry_edge_bps
        and short_notional >= args.min_top_notional
        and (not args.require_oracle_confirmation or (oracle_gap is not None and oracle_gap >= args.entry_oracle_gap_bps))
    ):
        return Signal(
            side="short",
            gross_edge_bps=short_edge,
            oracle_gap_bps=oracle_gap,
            entry_px=target_bid_px,
            exit_px_now=target_ask_px,
            top_notional=short_notional,
            target_spread_bps=target_spread_bps,
            target_book_age_ms=target_book_age_ms,
            reference_book_age_ms=reference_book_age_ms,
        )

    return None


def position_pnl_bps(position: BotPosition, states: dict[str, BookState]) -> float | None:
    target = states[TARGET_COIN]
    if position.side == "long":
        exit_level = best_bid(target)
        if not exit_level:
            return None
        return (float(exit_level["px"]) - position.entry_px) / position.entry_px * 10_000
    exit_level = best_ask(target)
    if not exit_level:
        return None
    return (position.entry_px - float(exit_level["px"])) / position.entry_px * 10_000


def exit_reason(position: BotPosition, signal: Signal | None, states: dict[str, BookState], args: argparse.Namespace) -> str | None:
    pnl = position_pnl_bps(position, states)
    age_s = (now_ms() - position.entry_time_ms) / 1000
    if pnl is not None and pnl >= args.take_profit_bps:
        return "take_profit"
    if pnl is not None and pnl <= -args.stop_loss_bps:
        return "stop_loss"
    if age_s >= args.max_hold_s:
        return "max_hold"
    if signal is None:
        return "edge_closed"
    if signal.side != position.side:
        return "signal_reversed"
    if signal.gross_edge_bps <= args.exit_edge_bps:
        return "edge_below_exit"
    return None


def order_size(entry_px: float, size_decimals: int, args: argparse.Namespace) -> float:
    raw_size = args.order_notional / entry_px
    rounded = round_down(raw_size, size_decimals)
    if rounded <= 0:
        raise RuntimeError(f"order size rounds to zero: raw={raw_size}, decimals={size_decimals}")
    return rounded


async def subscribe(ws: websockets.WebSocketClientProtocol) -> None:
    for coin in [TARGET_COIN, REFERENCE_COIN]:
        await ws.send(json.dumps({"method": "subscribe", "subscription": {"type": "l2Book", "coin": coin}}))
        await ws.send(json.dumps({"method": "subscribe", "subscription": {"type": "activeAssetCtx", "coin": coin}}))


async def heartbeat(ws: websockets.WebSocketClientProtocol, interval_s: float) -> None:
    while True:
        await asyncio.sleep(interval_s)
        await ws.send(json.dumps({"method": "ping"}))


async def run_bot(args: argparse.Namespace) -> int:
    live = bool(args.live)
    dry_run = not live
    size_decimals = load_size_decimals(TARGET_COIN, args.http_timeout)
    executor = None if dry_run else HyperliquidExecutor(testnet=args.testnet)
    logger = TradeLogger(args.log, dry_run=dry_run)

    states = {
        TARGET_COIN: BookState(TARGET_COIN),
        REFERENCE_COIN: BookState(REFERENCE_COIN),
    }
    simulated_position: BotPosition | None = None
    live_tracked_position: BotPosition | None = None
    last_trade_ms = 0
    last_deadman_ms = 0
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
        f"mode={'live' if live else 'dry-run'} target={TARGET_COIN} reference={REFERENCE_COIN} "
        f"order_notional={args.order_notional} entry_edge={args.entry_edge_bps}bps",
        flush=True,
    )
    if live:
        print("LIVE MODE: orders will be sent through execution/executor.py", flush=True)

    try:
        while not stop.is_set():
            try:
                async with websockets.connect(args.ws_url, ping_interval=None, close_timeout=5) as ws:
                    await subscribe(ws)
                    ping_task = asyncio.create_task(heartbeat(ws, args.ping_interval_s))
                    print(f"[{utc_iso()}] connected", flush=True)
                    try:
                        while not stop.is_set():
                            raw = await asyncio.wait_for(ws.recv(), timeout=args.recv_timeout_s)
                            message = json.loads(raw)
                            channel = message.get("channel")
                            data = message.get("data", {})

                            if channel == "l2Book":
                                coin = data.get("coin")
                                if coin not in states:
                                    continue
                                levels = data.get("levels") or [[], []]
                                states[coin].bids = levels[0] or []
                                states[coin].asks = levels[1] or []
                                states[coin].book_time_ms = data.get("time")
                                states[coin].recv_time_ms = now_ms()
                            elif channel == "activeAssetCtx":
                                coin = data.get("coin")
                                if coin not in states:
                                    continue
                                states[coin].ctx = data.get("ctx") or {}
                                states[coin].ctx_recv_time_ms = now_ms()
                            elif channel in {"subscriptionResponse", "pong"}:
                                continue
                            else:
                                continue

                            if live and now_ms() - last_deadman_ms >= args.deadman_refresh_ms:
                                executor.schedule_cancel_all(ms_from_now=args.deadman_ms)
                                last_deadman_ms = now_ms()

                            if dry_run:
                                active_position = simulated_position
                            else:
                                observed_position = live_position(executor)
                                if observed_position is None:
                                    live_tracked_position = None
                                    active_position = None
                                elif (
                                    live_tracked_position is not None
                                    and live_tracked_position.side == observed_position.side
                                ):
                                    live_tracked_position.size = observed_position.size
                                    if not math.isnan(observed_position.entry_px):
                                        live_tracked_position.entry_px = observed_position.entry_px
                                    active_position = live_tracked_position
                                else:
                                    live_tracked_position = observed_position
                                    active_position = live_tracked_position
                            current_signal = compute_signal(states, args)

                            if active_position is not None:
                                reason = exit_reason(active_position, current_signal, states, args)
                                if reason:
                                    pnl = position_pnl_bps(active_position, states)
                                    exit_is_buy = active_position.side == "short"
                                    exit_level = best_ask(states[TARGET_COIN]) if exit_is_buy else best_bid(states[TARGET_COIN])
                                    if not exit_level:
                                        continue
                                    limit_px = protected_price(exit_level["px"], exit_is_buy, args.exit_price_protection_bps)
                                    size = abs(active_position.size)
                                    raw_result = None
                                    if live:
                                        raw_result = executor.exit_reduce_only_ioc(
                                            coin=TARGET_COIN,
                                            is_buy=exit_is_buy,
                                            size=size,
                                            limit_px=limit_px,
                                        )
                                    else:
                                        simulated_position = None
                                    logger.write(
                                        event="exit_order",
                                        reason=reason,
                                        signal=current_signal,
                                        states=states,
                                        side=active_position.side,
                                        size=size,
                                        price=limit_px,
                                        pnl_bps_value=pnl,
                                        raw=raw_result,
                                    )
                                    print(f"[{utc_iso()}] exit {active_position.side} {size} @ {limit_px} reason={reason} pnl_bps={pnl}", flush=True)
                                    last_trade_ms = now_ms()
                                continue

                            if current_signal is None:
                                continue
                            if now_ms() - last_trade_ms < args.cooldown_ms:
                                continue
                            if live and executor.get_open_orders(TARGET_COIN):
                                logger.write(event="skip", reason="open_order_exists", signal=current_signal, states=states)
                                continue
                            if live and executor.get_position(TARGET_COIN) is not None:
                                logger.write(event="skip", reason="existing_position", signal=current_signal, states=states)
                                continue

                            is_buy = current_signal.side == "long"
                            limit_px = protected_price(
                                str(current_signal.entry_px),
                                is_buy,
                                args.entry_price_protection_bps,
                            )
                            size = order_size(limit_px, size_decimals, args)
                            if size * limit_px > args.max_order_notional:
                                size = round_down(args.max_order_notional / limit_px, size_decimals)
                            if size * limit_px < args.min_order_notional:
                                logger.write(event="skip", reason="size_below_min_order_notional", signal=current_signal, states=states, size=size, price=limit_px)
                                continue

                            raw_result = None
                            if live:
                                raw_result = executor.enter_ioc(
                                    coin=TARGET_COIN,
                                    is_buy=is_buy,
                                    size=size,
                                    limit_px=limit_px,
                                )
                                await asyncio.sleep(args.fill_check_delay_s)
                                active_after = executor.get_position(TARGET_COIN)
                                if active_after is None:
                                    logger.write(event="entry_no_fill", reason="ioc_not_filled", signal=current_signal, states=states, size=size, price=limit_px, raw=raw_result)
                                    print(f"[{utc_iso()}] entry no fill {current_signal.side} {size} @ {limit_px}", flush=True)
                                    last_trade_ms = now_ms()
                                    continue
                                live_tracked_position = BotPosition(
                                    side="long" if active_after.size > 0 else "short",
                                    size=active_after.size,
                                    entry_px=active_after.entry_px or limit_px,
                                    entry_time_ms=now_ms(),
                                    entry_signal_id=f"{now_ms()}_{current_signal.side}",
                                    live_position=True,
                                )
                            else:
                                simulated_position = BotPosition(
                                    side=current_signal.side,
                                    size=size if current_signal.side == "long" else -size,
                                    entry_px=limit_px,
                                    entry_time_ms=now_ms(),
                                    entry_signal_id=f"{now_ms()}_{current_signal.side}",
                                    live_position=False,
                                )

                            logger.write(
                                event="entry_order",
                                reason="signal",
                                signal=current_signal,
                                states=states,
                                side=current_signal.side,
                                size=size,
                                price=limit_px,
                                raw=raw_result,
                            )
                            print(
                                f"[{utc_iso()}] entry {current_signal.side} {size} @ {limit_px} "
                                f"edge={current_signal.gross_edge_bps:.2f} oracle={current_signal.oracle_gap_bps}",
                                flush=True,
                            )
                            last_trade_ms = now_ms()
                    finally:
                        ping_task.cancel()
                        try:
                            await ping_task
                        except asyncio.CancelledError:
                            pass
            except asyncio.TimeoutError:
                print(f"[{utc_iso()}] websocket timeout; reconnecting", flush=True)
            except Exception as exc:
                logger.write(event="error", reason=str(exc), signal=None, states=states, raw={"error": str(exc)})
                print(f"[{utc_iso()}] error: {exc}; reconnecting in {args.reconnect_delay_s}s", flush=True)
                await asyncio.sleep(args.reconnect_delay_s)
    finally:
        if live and executor is not None:
            try:
                executor.cancel_all_for_coin(TARGET_COIN)
            except Exception as exc:
                print(f"[{utc_iso()}] cancel_all_for_coin failed during shutdown: {exc}", flush=True)
        logger.close()
    return 0


def live_position(executor: HyperliquidExecutor) -> BotPosition | None:
    position = executor.get_position(TARGET_COIN)
    if position is None:
        return None
    side: Side = "long" if position.size > 0 else "short"
    return BotPosition(
        side=side,
        size=position.size,
        entry_px=position.entry_px or math.nan,
        entry_time_ms=now_ms(),
        entry_signal_id="live_position",
        live_position=True,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Execute tiny unhedged para:UNITREE lag test orders.")
    parser.add_argument("--live", action="store_true", help="Send real orders. Default is dry-run.")
    parser.add_argument("--testnet", action="store_true", help="Use Hyperliquid testnet executor base URL")
    parser.add_argument("--log", type=Path, default=DEFAULT_LOG, help=f"CSV event log path, default: {DEFAULT_LOG}")
    parser.add_argument("--order-notional", type=float, default=20.0, help="Desired order notional in USDC")
    parser.add_argument("--max-order-notional", type=float, default=20.0, help="Hard cap per order")
    parser.add_argument("--min-order-notional", type=float, default=5.0, help="Skip if rounded order falls below this")
    parser.add_argument("--entry-edge-bps", type=float, default=50.0, help="Minimum gross edge to enter")
    parser.add_argument("--exit-edge-bps", type=float, default=5.0, help="Exit if same-direction edge compresses below this")
    parser.add_argument("--entry-oracle-gap-bps", type=float, default=75.0, help="Required oracle confirmation size")
    parser.add_argument("--no-oracle-confirmation", dest="require_oracle_confirmation", action="store_false", help="Do not require oracle gap direction confirmation")
    parser.add_argument("--min-top-notional", type=float, default=80.0, help="Minimum executable top-book notional on both legs")
    parser.add_argument("--max-book-age-ms", type=int, default=750, help="Max age for target/reference books")
    parser.add_argument("--max-target-spread-bps", type=float, default=25.0, help="Max para top-of-book spread")
    parser.add_argument("--take-profit-bps", type=float, default=50.0, help="Reduce-only exit profit target")
    parser.add_argument("--stop-loss-bps", type=float, default=75.0, help="Reduce-only exit stop loss")
    parser.add_argument("--max-hold-s", type=float, default=180.0, help="Max seconds to hold a position")
    parser.add_argument("--cooldown-ms", type=int, default=15_000, help="Minimum ms after entry/exit before next entry")
    parser.add_argument("--entry-price-protection-bps", type=float, default=0.0, help="Extra IOC limit protection beyond displayed entry price")
    parser.add_argument("--exit-price-protection-bps", type=float, default=5.0, help="Extra IOC limit protection beyond displayed exit price")
    parser.add_argument("--fill-check-delay-s", type=float, default=0.5, help="Delay before checking live IOC fill")
    parser.add_argument("--duration-s", type=float, default=None, help="Optional run duration")
    parser.add_argument("--http-timeout", type=float, default=15.0)
    parser.add_argument("--ws-url", default=WS_URL)
    parser.add_argument("--recv-timeout-s", type=float, default=75.0)
    parser.add_argument("--ping-interval-s", type=float, default=30.0)
    parser.add_argument("--reconnect-delay-s", type=float, default=3.0)
    parser.add_argument("--deadman-ms", type=int, default=15_000)
    parser.add_argument("--deadman-refresh-ms", type=int, default=5_000)
    parser.set_defaults(require_oracle_confirmation=True)
    return parser.parse_args()


def main() -> int:
    return asyncio.run(run_bot(parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
