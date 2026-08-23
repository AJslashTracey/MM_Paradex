#!/usr/bin/env python3
"""Watch HIP-3 markets for cross-market/oracle lag and record books on trigger.

The default universe is the RWA lag shortlist:

* para:UNITREE vs xyz:UNITREE
* io:SNDK vs xyz:SNDK
* mkts:US500 vs xyz:SP500
* para:AAOI/AVGO/CRWD/IREN/RDDT/NET vs xyz:same

When a configured deviation crosses threshold, the script starts appending the
latest target/reference order-book snapshots to CSV.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import csv
import json
import signal
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import websockets


INFO_URL = "https://api.hyperliquid.xyz/info"
WS_URL = "wss://api.hyperliquid.xyz/ws"
DEFAULT_OUTPUT = Path("data/hip3_lag_order_books.csv")


@dataclass(frozen=True)
class PairSpec:
    name: str
    target: str
    reference: str
    reference_scale: float = 1.0
    note: str = ""


@dataclass
class MarketState:
    coin: str
    book_time_ms: int | None = None
    recv_time_ms: int | None = None
    bids: list[dict[str, Any]] | None = None
    asks: list[dict[str, Any]] | None = None
    ctx: dict[str, Any] | None = None
    ctx_recv_time_ms: int | None = None


@dataclass
class TriggerState:
    active: bool = False
    last_write_ms: int = 0
    last_trigger_ms: int = 0
    trigger_id: str = ""


DEFAULT_PAIRS = [
    PairSpec("para_UNITREE_vs_xyz_UNITREE", "para:UNITREE", "xyz:UNITREE"),
    PairSpec("io_SNDK_vs_xyz_SNDK", "io:SNDK", "xyz:SNDK"),
    PairSpec("mkts_US500_vs_xyz_SP500", "mkts:US500", "xyz:SP500", 0.1),
    PairSpec("para_AAOI_vs_xyz_AAOI", "para:AAOI", "xyz:AAOI"),
    PairSpec("para_AVGO_vs_xyz_AVGO", "para:AVGO", "xyz:AVGO"),
    PairSpec("para_CRWD_vs_xyz_CRWD", "para:CRWD", "xyz:CRWD"),
    PairSpec("para_IREN_vs_xyz_IREN", "para:IREN", "xyz:IREN"),
    PairSpec("para_RDDT_vs_xyz_RDDT", "para:RDDT", "xyz:RDDT"),
    PairSpec("para_NET_vs_xyz_NET", "para:NET", "xyz:NET"),
]


DEFAULT_WATCH_ONLY = [
    "xyz:SP500",
    "xyz:XYZ100",
    "xyz:SKHX",
    "xyz:SPCX",
    "xyz:EUR",
    "xyz:SNDK",
    "xyz:NVDA",
    "xyz:MU",
    "xyz:BRENTOIL",
    "xyz:SILVER",
    "xyz:MSTR",
    "xyz:SKHY",
    "xyz:CL",
    "xyz:DRAM",
    "xyz:GOLD",
    "xyz:CRCL",
    "xyz:GOOGL",
    "xyz:COIN",
    "xyz:META",
    "xyz:BABA",
]


SNAPSHOT_FIELDS = [
    "snapshot_time_utc",
    "recv_time_ms",
    "event",
    "trigger_id",
    "pair",
    "role",
    "coin",
    "reference_scale",
    "deviation_bps",
    "long_edge_bps",
    "short_edge_bps",
    "target_oracle_vs_fair_bps",
    "coin_oracle_vs_mid_bps",
    "book_time_ms",
    "book_age_ms",
    "bid_px",
    "bid_sz",
    "ask_px",
    "ask_sz",
    "mid_px",
    "ctx_mid_px",
    "ctx_mark_px",
    "ctx_oracle_px",
    "ctx_funding",
    "bid_levels_json",
    "ask_levels_json",
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


def f(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def post_info(payload: dict[str, Any], timeout: float) -> Any:
    request = urllib.request.Request(
        INFO_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "User-Agent": "hip3-lag-watcher/1.0",
        },
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


def dex_from_coin(coin: str) -> str | None:
    return coin.split(":", 1)[0] if ":" in coin else None


def load_live_coins(coins: set[str], timeout: float) -> set[str]:
    """Return coins that appear in current meta and are not marked delisted."""
    by_dex: dict[str | None, set[str]] = {}
    for coin in coins:
        by_dex.setdefault(dex_from_coin(coin), set()).add(coin)

    live: set[str] = set()
    for dex, wanted in sorted(by_dex.items(), key=lambda item: item[0] or ""):
        payload = {"type": "metaAndAssetCtxs"}
        if dex is not None:
            payload["dex"] = dex
        meta, _ctxs = post_info(payload, timeout)
        for asset in meta.get("universe", []):
            name = asset.get("name")
            if name in wanted and not asset.get("isDelisted"):
                live.add(name)
    return live


def parse_pair(raw: str) -> PairSpec:
    """Parse target=reference[:scale] or target,reference[,scale]."""
    raw = raw.strip()
    if not raw:
        raise ValueError("empty pair")
    if "=" in raw:
        target, rhs = raw.split("=", 1)
    elif "," in raw:
        parts = raw.split(",")
        if len(parts) not in {2, 3}:
            raise ValueError(f"bad pair {raw!r}; expected target,reference[,scale]")
        target, reference = parts[0], parts[1]
        scale = float(parts[2]) if len(parts) == 3 else 1.0
        return PairSpec(f"{target}_vs_{reference}".replace(":", "_"), target, reference, scale)
    else:
        raise ValueError(f"bad pair {raw!r}; expected target=reference[:scale]")

    rhs_parts = rhs.rsplit(":", 1)
    if len(rhs_parts) == 2:
        reference_candidate, scale_candidate = rhs_parts
        try:
            scale = float(scale_candidate)
            reference = reference_candidate
        except ValueError:
            reference = rhs
            scale = 1.0
    else:
        reference = rhs
        scale = 1.0
    return PairSpec(f"{target}_vs_{reference}".replace(":", "_"), target, reference, scale)


def best_bid(state: MarketState) -> dict[str, Any] | None:
    return state.bids[0] if state.bids else None


def best_ask(state: MarketState) -> dict[str, Any] | None:
    return state.asks[0] if state.asks else None


def book_mid(state: MarketState) -> float | None:
    bid = f(best_bid(state).get("px")) if best_bid(state) else None
    ask = f(best_ask(state).get("px")) if best_ask(state) else None
    if bid is not None and ask is not None and bid > 0 and ask > 0:
        return (bid + ask) / 2
    ctx = state.ctx or {}
    return f(ctx.get("midPx")) or f(ctx.get("markPx"))


def scaled(value: float | None, scale: float) -> float | None:
    return None if value is None else value * scale


def bps(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or denominator is None or denominator == 0:
        return None
    return numerator / denominator * 10_000


def pair_metrics(pair: PairSpec, states: dict[str, MarketState]) -> dict[str, float | None]:
    target = states[pair.target]
    reference = states[pair.reference]

    target_mid = book_mid(target)
    reference_mid = scaled(book_mid(reference), pair.reference_scale)
    target_bid = f(best_bid(target).get("px")) if best_bid(target) else None
    target_ask = f(best_ask(target).get("px")) if best_ask(target) else None
    reference_bid = scaled(f(best_bid(reference).get("px")) if best_bid(reference) else None, pair.reference_scale)
    reference_ask = scaled(f(best_ask(reference).get("px")) if best_ask(reference) else None, pair.reference_scale)

    target_ctx = target.ctx or {}
    target_oracle = f(target_ctx.get("oraclePx"))

    return {
        "deviation_bps": bps((target_mid - reference_mid) if target_mid is not None and reference_mid is not None else None, reference_mid),
        "long_edge_bps": bps((reference_bid - target_ask) if reference_bid is not None and target_ask is not None else None, reference_mid),
        "short_edge_bps": bps((target_bid - reference_ask) if target_bid is not None and reference_ask is not None else None, reference_mid),
        "target_oracle_vs_fair_bps": bps((target_oracle - reference_mid) if target_oracle is not None and reference_mid is not None else None, reference_mid),
    }


def coin_oracle_vs_mid_bps(state: MarketState) -> float | None:
    ctx = state.ctx or {}
    oracle = f(ctx.get("oraclePx"))
    mid = book_mid(state)
    return bps((oracle - mid) if oracle is not None and mid is not None else None, mid)


def should_trigger_pair(metrics: dict[str, float | None], args: argparse.Namespace) -> tuple[bool, str]:
    reasons: list[str] = []
    deviation = metrics["deviation_bps"]
    long_edge = metrics["long_edge_bps"]
    short_edge = metrics["short_edge_bps"]
    oracle_vs_fair = metrics["target_oracle_vs_fair_bps"]

    if deviation is not None and abs(deviation) >= args.mid_deviation_bps:
        reasons.append(f"mid_deviation={deviation:.2f}bps")
    if long_edge is not None and long_edge >= args.edge_bps:
        reasons.append(f"long_edge={long_edge:.2f}bps")
    if short_edge is not None and short_edge >= args.edge_bps:
        reasons.append(f"short_edge={short_edge:.2f}bps")
    if oracle_vs_fair is not None and abs(oracle_vs_fair) >= args.oracle_deviation_bps:
        reasons.append(f"target_oracle_vs_fair={oracle_vs_fair:.2f}bps")
    return bool(reasons), ";".join(reasons)


def should_trigger_coin(state: MarketState, args: argparse.Namespace) -> tuple[bool, str]:
    oracle_gap = coin_oracle_vs_mid_bps(state)
    if oracle_gap is not None and abs(oracle_gap) >= args.oracle_deviation_bps:
        return True, f"coin_oracle_vs_mid={oracle_gap:.2f}bps"
    return False, ""


class SnapshotWriter:
    def __init__(self, output_path: Path) -> None:
        self.output_path = output_path
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        needs_header = not self.output_path.exists() or self.output_path.stat().st_size == 0
        self.output_file = self.output_path.open("a", newline="", encoding="utf-8")
        self.writer = csv.DictWriter(self.output_file, fieldnames=SNAPSHOT_FIELDS)
        if needs_header:
            self.writer.writeheader()
            self.output_file.flush()

    def close(self) -> None:
        self.output_file.close()

    def write_snapshot(
        self,
        *,
        event: str,
        trigger_id: str,
        pair: str,
        role: str,
        coin: str,
        reference_scale: float,
        state: MarketState,
        metrics: dict[str, float | None],
        recv_ms: int,
    ) -> None:
        bid = best_bid(state) or {}
        ask = best_ask(state) or {}
        ctx = state.ctx or {}
        mid = book_mid(state)
        book_age_ms = None if state.book_time_ms is None else recv_ms - state.book_time_ms
        self.writer.writerow(
            {
                "snapshot_time_utc": utc_iso(recv_ms),
                "recv_time_ms": recv_ms,
                "event": event,
                "trigger_id": trigger_id,
                "pair": pair,
                "role": role,
                "coin": coin,
                "reference_scale": reference_scale,
                "deviation_bps": fmt(metrics.get("deviation_bps")),
                "long_edge_bps": fmt(metrics.get("long_edge_bps")),
                "short_edge_bps": fmt(metrics.get("short_edge_bps")),
                "target_oracle_vs_fair_bps": fmt(metrics.get("target_oracle_vs_fair_bps")),
                "coin_oracle_vs_mid_bps": fmt(coin_oracle_vs_mid_bps(state)),
                "book_time_ms": state.book_time_ms or "",
                "book_age_ms": "" if book_age_ms is None else book_age_ms,
                "bid_px": bid.get("px", ""),
                "bid_sz": bid.get("sz", ""),
                "ask_px": ask.get("px", ""),
                "ask_sz": ask.get("sz", ""),
                "mid_px": fmt(mid),
                "ctx_mid_px": ctx.get("midPx", ""),
                "ctx_mark_px": ctx.get("markPx", ""),
                "ctx_oracle_px": ctx.get("oraclePx", ""),
                "ctx_funding": ctx.get("funding", ""),
                "bid_levels_json": json.dumps(state.bids or [], separators=(",", ":")),
                "ask_levels_json": json.dumps(state.asks or [], separators=(",", ":")),
            }
        )

    def flush(self) -> None:
        self.output_file.flush()


def fmt(value: float | None) -> str:
    return "" if value is None else f"{value:.8f}"


def event_id(name: str, recv_ms: int) -> str:
    return f"{datetime.fromtimestamp(recv_ms / 1000, tz=timezone.utc).strftime('%Y%m%dT%H%M%S')}_{name}"


async def heartbeat(ws: websockets.WebSocketClientProtocol, interval_s: float) -> None:
    while True:
        await asyncio.sleep(interval_s)
        await ws.send(json.dumps({"method": "ping"}))


async def subscribe(ws: websockets.WebSocketClientProtocol, coins: list[str]) -> None:
    for coin in coins:
        await ws.send(json.dumps({"method": "subscribe", "subscription": {"type": "l2Book", "coin": coin}}))
        await ws.send(json.dumps({"method": "subscribe", "subscription": {"type": "activeAssetCtx", "coin": coin}}))


def maybe_write_pair(
    *,
    writer: SnapshotWriter,
    pair: PairSpec,
    states: dict[str, MarketState],
    triggers: dict[str, TriggerState],
    args: argparse.Namespace,
    recv_ms: int,
) -> None:
    if states[pair.target].bids is None or states[pair.reference].bids is None:
        return

    metrics = pair_metrics(pair, states)
    triggered, reason = should_trigger_pair(metrics, args)
    state = triggers[pair.name]
    if triggered:
        if not state.active:
            state.trigger_id = event_id(pair.name, recv_ms)
            print(f"[{utc_iso(recv_ms)}] START {pair.name} {reason}", flush=True)
            event = "start"
        else:
            event = "active"
        state.active = True
        state.last_trigger_ms = recv_ms
    elif state.active and recv_ms - state.last_trigger_ms <= args.cooldown_ms:
        event = "cooldown"
    elif state.active:
        print(f"[{utc_iso(recv_ms)}] END {pair.name}", flush=True)
        state.active = False
        state.last_write_ms = 0
        state.trigger_id = ""
        return
    else:
        return

    if recv_ms - state.last_write_ms < args.snapshot_interval_ms:
        return

    writer.write_snapshot(
        event=event,
        trigger_id=state.trigger_id,
        pair=pair.name,
        role="target",
        coin=pair.target,
        reference_scale=pair.reference_scale,
        state=states[pair.target],
        metrics=metrics,
        recv_ms=recv_ms,
    )
    writer.write_snapshot(
        event=event,
        trigger_id=state.trigger_id,
        pair=pair.name,
        role="reference",
        coin=pair.reference,
        reference_scale=pair.reference_scale,
        state=states[pair.reference],
        metrics=metrics,
        recv_ms=recv_ms,
    )
    writer.flush()
    state.last_write_ms = recv_ms


def maybe_write_coin(
    *,
    writer: SnapshotWriter,
    coin: str,
    states: dict[str, MarketState],
    triggers: dict[str, TriggerState],
    args: argparse.Namespace,
    recv_ms: int,
) -> None:
    state = states[coin]
    if state.bids is None:
        return

    triggered, reason = should_trigger_coin(state, args)
    trigger = triggers[coin]
    if triggered:
        if not trigger.active:
            trigger.trigger_id = event_id(coin.replace(":", "_"), recv_ms)
            print(f"[{utc_iso(recv_ms)}] START {coin} {reason}", flush=True)
            event = "start"
        else:
            event = "active"
        trigger.active = True
        trigger.last_trigger_ms = recv_ms
    elif trigger.active and recv_ms - trigger.last_trigger_ms <= args.cooldown_ms:
        event = "cooldown"
    elif trigger.active:
        print(f"[{utc_iso(recv_ms)}] END {coin}", flush=True)
        trigger.active = False
        trigger.last_write_ms = 0
        trigger.trigger_id = ""
        return
    else:
        return

    if recv_ms - trigger.last_write_ms < args.snapshot_interval_ms:
        return

    metrics = {
        "deviation_bps": None,
        "long_edge_bps": None,
        "short_edge_bps": None,
        "target_oracle_vs_fair_bps": None,
    }
    writer.write_snapshot(
        event=event,
        trigger_id=trigger.trigger_id,
        pair=coin,
        role="watch_only",
        coin=coin,
        reference_scale=1.0,
        state=state,
        metrics=metrics,
        recv_ms=recv_ms,
    )
    writer.flush()
    trigger.last_write_ms = recv_ms


async def watch(args: argparse.Namespace) -> int:
    pairs = list(DEFAULT_PAIRS if args.default_pairs else [])
    pairs.extend(parse_pair(raw) for raw in args.pair)

    watch_only = set(DEFAULT_WATCH_ONLY if args.default_watch_only else [])
    watch_only.update(args.watch_only)

    all_coins = {coin for pair in pairs for coin in (pair.target, pair.reference)}
    all_coins.update(watch_only)
    live = load_live_coins(all_coins, args.http_timeout)
    missing = sorted(all_coins - live)
    if missing and not args.allow_missing:
        print("error: these configured coins are not live on Hyperliquid:", ", ".join(missing), file=sys.stderr)
        print("rerun with --allow-missing to skip them", file=sys.stderr)
        return 2
    if missing:
        print("Skipping missing/delisted coins:", ", ".join(missing), flush=True)

    pairs = [pair for pair in pairs if pair.target in live and pair.reference in live]
    watch_only = {coin for coin in watch_only if coin in live}
    coins = sorted({coin for pair in pairs for coin in (pair.target, pair.reference)} | watch_only)

    if not coins:
        print("error: no live coins to watch", file=sys.stderr)
        return 2

    print(f"Watching {len(pairs)} pairs and {len(watch_only)} watch-only coins")
    print(f"Output: {args.output}")
    print(f"Thresholds: edge={args.edge_bps}bps mid={args.mid_deviation_bps}bps oracle={args.oracle_deviation_bps}bps")
    print("Coins:", ", ".join(coins), flush=True)

    states = {coin: MarketState(coin) for coin in coins}
    pair_triggers = {pair.name: TriggerState() for pair in pairs}
    coin_triggers = {coin: TriggerState() for coin in watch_only}
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

    writer = SnapshotWriter(args.output)
    try:
        while not stop.is_set():
            try:
                async with websockets.connect(args.ws_url, ping_interval=None, close_timeout=5) as ws:
                    await subscribe(ws, coins)
                    ping_task = asyncio.create_task(heartbeat(ws, args.ping_interval_s))
                    print(f"[{utc_iso()}] connected", flush=True)
                    try:
                        while not stop.is_set():
                            raw = await asyncio.wait_for(ws.recv(), timeout=args.recv_timeout_s)
                            recv_ms = now_ms()
                            message = json.loads(raw)
                            channel = message.get("channel")

                            if channel == "l2Book":
                                data = message.get("data", {})
                                coin = data.get("coin")
                                if coin not in states:
                                    continue
                                state = states[coin]
                                state.book_time_ms = data.get("time")
                                state.recv_time_ms = recv_ms
                                levels = data.get("levels") or [[], []]
                                state.bids = levels[0] or []
                                state.asks = levels[1] or []
                            elif channel == "activeAssetCtx":
                                data = message.get("data", {})
                                coin = data.get("coin")
                                if coin not in states:
                                    continue
                                states[coin].ctx = data.get("ctx") or {}
                                states[coin].ctx_recv_time_ms = recv_ms
                            elif channel in {"subscriptionResponse", "pong"}:
                                continue
                            else:
                                continue

                            touched_pairs = [pair for pair in pairs if coin in {pair.target, pair.reference}]
                            for pair in touched_pairs:
                                maybe_write_pair(
                                    writer=writer,
                                    pair=pair,
                                    states=states,
                                    triggers=pair_triggers,
                                    args=args,
                                    recv_ms=recv_ms,
                                )
                            if coin in watch_only:
                                maybe_write_coin(
                                    writer=writer,
                                    coin=coin,
                                    states=states,
                                    triggers=coin_triggers,
                                    args=args,
                                    recv_ms=recv_ms,
                                )
                    finally:
                        ping_task.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await ping_task
            except asyncio.TimeoutError:
                print(f"[{utc_iso()}] websocket receive timeout; reconnecting", flush=True)
            except Exception as exc:
                print(f"[{utc_iso()}] websocket error: {exc}; reconnecting in {args.reconnect_delay_s}s", file=sys.stderr, flush=True)
                await asyncio.sleep(args.reconnect_delay_s)
    finally:
        writer.close()

    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Watch HIP-3 lag candidates and write L2 book snapshots to CSV after deviations trigger.",
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help=f"CSV output path, default: {DEFAULT_OUTPUT}")
    parser.add_argument("--edge-bps", type=float, default=25.0, help="Tradable top-of-book edge trigger, default: 25 bps")
    parser.add_argument("--mid-deviation-bps", type=float, default=100.0, help="Signed target-vs-reference mid trigger, default: 100 bps")
    parser.add_argument("--oracle-deviation-bps", type=float, default=100.0, help="Oracle-vs-fair/mid trigger, default: 100 bps")
    parser.add_argument("--snapshot-interval-ms", type=int, default=1_000, help="Minimum time between CSV snapshots per trigger")
    parser.add_argument("--cooldown-ms", type=int, default=10_000, help="Keep writing briefly after trigger clears")
    parser.add_argument("--recv-timeout-s", type=float, default=75.0, help="Reconnect if no websocket messages arrive")
    parser.add_argument("--duration-s", type=float, default=None, help="Optional run duration for smoke tests or timed captures")
    parser.add_argument("--ping-interval-s", type=float, default=30.0, help="Heartbeat ping interval")
    parser.add_argument("--reconnect-delay-s", type=float, default=3.0, help="Delay after websocket errors")
    parser.add_argument("--http-timeout", type=float, default=15.0, help="HTTP validation timeout")
    parser.add_argument("--ws-url", default=WS_URL, help=f"WebSocket URL, default: {WS_URL}")
    parser.add_argument(
        "--pair",
        action="append",
        default=[],
        help="Extra pair as target=reference[:scale] or target,reference[,scale]. Example: para:UNITREE=xyz:UNITREE",
    )
    parser.add_argument("--watch-only", action="append", default=[], help="Extra single coin to record on oracle-vs-mid deviations")
    parser.add_argument("--no-default-pairs", dest="default_pairs", action="store_false", help="Disable built-in pair list")
    parser.add_argument("--no-default-watch-only", dest="default_watch_only", action="store_false", help="Disable built-in xyz watch-only list")
    parser.add_argument("--allow-missing", action="store_true", help="Skip missing/delisted configured coins instead of exiting")
    parser.set_defaults(default_pairs=True, default_watch_only=True)
    return parser.parse_args()


def main() -> int:
    return asyncio.run(watch(parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
