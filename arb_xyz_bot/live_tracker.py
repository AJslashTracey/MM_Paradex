from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import signal
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import websockets
from websockets.asyncio.client import ClientConnection

from .fairprice_engine import (
    BookLevel,
    DepthSummary,
    ExecutableRoute,
    VenueBook,
    VenueQuote,
    bps_difference,
    depth_summary,
    median_decimal,
    simulate_sell_route,
)
from .hyperliquid import HyperliquidClient
from .paradex import ParadexClient


LOGGER = logging.getLogger(__name__)

HYPERLIQUID_WS_URL = "wss://api.hyperliquid.xyz/ws"
PARADEX_WS_URL = "wss://ws.api.prod.paradex.trade/v1"
VENUE_HYPERLIQUID_XYZ = "hyperliquid_xyz"
VENUE_PARADEX = "paradex"


def decimal_from_text(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def decimal_to_float(value: Decimal | None) -> float | None:
    if value is None:
        return None
    return float(value)


@dataclass(frozen=True)
class SymbolConfig:
    symbol: str
    hyperliquid_coin: str
    paradex_market: str
    day_volume_usd: Decimal


@dataclass(frozen=True)
class TrackerConfig:
    base_dir: Path
    min_quote_deviation_bps: Decimal = Decimal("1")
    min_executable_edge_bps: Decimal = Decimal("5")
    exit_edge_bps: Decimal = Decimal("0.5")
    min_matched_notional_usd: Decimal = Decimal("250")
    max_quote_age_ms: int = 750
    max_spread_bps: Decimal = Decimal("100")
    flush_interval_s: float = 5.0
    depth_snapshot_interval_ms: int = 250
    maintenance_interval_s: float = 1.0
    top_symbols: int = 20
    symbols: tuple[str, ...] = ()
    fee_bps_by_venue: dict[str, Decimal] = field(
        default_factory=lambda: {
            VENUE_HYPERLIQUID_XYZ: Decimal("0"),
            VENUE_PARADEX: Decimal("0"),
        }
    )

    @classmethod
    def from_env(cls) -> "TrackerConfig":
        base_dir = Path(os.environ.get("ARB_FAIRPRICE_BASE_DIR", "data/fairprice"))
        symbols_value = os.environ.get("ARB_FAIRPRICE_SYMBOLS", "")
        symbols = tuple(symbol.strip().upper() for symbol in symbols_value.split(",") if symbol.strip())
        return cls(
            base_dir=base_dir,
            min_quote_deviation_bps=Decimal(os.environ.get("ARB_FAIRPRICE_MIN_QUOTE_DEVIATION_BPS", "1")),
            min_executable_edge_bps=Decimal(os.environ.get("ARB_FAIRPRICE_MIN_EXECUTABLE_EDGE_BPS", "5")),
            exit_edge_bps=Decimal(os.environ.get("ARB_FAIRPRICE_EXIT_EDGE_BPS", "0.5")),
            min_matched_notional_usd=Decimal(os.environ.get("ARB_FAIRPRICE_MIN_MATCHED_NOTIONAL_USD", "250")),
            max_quote_age_ms=int(os.environ.get("ARB_FAIRPRICE_MAX_QUOTE_AGE_MS", "750")),
            max_spread_bps=Decimal(os.environ.get("ARB_FAIRPRICE_MAX_SPREAD_BPS", "100")),
            flush_interval_s=float(os.environ.get("ARB_FAIRPRICE_FLUSH_INTERVAL_S", "5")),
            depth_snapshot_interval_ms=int(
                os.environ.get("ARB_FAIRPRICE_DEPTH_SNAPSHOT_INTERVAL_MS", "250")
            ),
            maintenance_interval_s=float(os.environ.get("ARB_FAIRPRICE_MAINTENANCE_INTERVAL_S", "1")),
            top_symbols=int(os.environ.get("ARB_FAIRPRICE_TOP_SYMBOLS", "20")),
            symbols=symbols,
            fee_bps_by_venue={
                VENUE_HYPERLIQUID_XYZ: Decimal(
                    os.environ.get("ARB_FAIRPRICE_FEE_BPS_HYPERLIQUID_XYZ", "0")
                ),
                VENUE_PARADEX: Decimal(os.environ.get("ARB_FAIRPRICE_FEE_BPS_PARADEX", "0")),
            },
        )


@dataclass
class ActiveEvent:
    event_id: str
    symbol: str
    source_venue: str
    started_ts_ns: int
    last_seen_ts_ns: int
    last_depth_snapshot_ts_ns: int
    fair_mid_at_start: Decimal
    peak_quote_deviation_bps: Decimal
    peak_top_net_edge_bps: Decimal
    peak_weighted_net_edge_bps: Decimal
    peak_matched_notional_usd: Decimal
    current_route: ExecutableRoute
    was_exploitable: bool
    samples: int = 1


class ParquetBatchWriter:
    def __init__(self, base_dir: Path) -> None:
        self.base_dir = base_dir
        self.buffers: dict[str, list[dict[str, Any]]] = {
            "quotes": [],
            "depth": [],
            "deviations": [],
        }
        self.sequence = 0
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def append(self, dataset: str, record: dict[str, Any]) -> None:
        self.buffers[dataset].append(record)

    def flush(self) -> None:
        timestamp = datetime.now(tz=UTC)
        date_part = timestamp.strftime("%Y-%m-%d")
        hour_part = timestamp.strftime("%H")
        for dataset, rows in self.buffers.items():
            if not rows:
                continue
            self.sequence += 1
            destination = self.base_dir / dataset / f"date={date_part}" / f"hour={hour_part}"
            destination.mkdir(parents=True, exist_ok=True)
            filename = (
                f"part-{timestamp.strftime('%Y%m%dT%H%M%S')}-{self.sequence:06d}.parquet"
            )
            table = pa.Table.from_pylist(rows)
            pq.write_table(table, destination / filename, compression="zstd")
            rows.clear()


class FairPriceTracker:
    def __init__(self, config: TrackerConfig, symbols: list[SymbolConfig]) -> None:
        self.config = config
        self.symbols = {item.symbol: item for item in symbols}
        self.symbol_by_hyperliquid_coin = {
            item.hyperliquid_coin: item.symbol for item in symbols
        }
        self.symbol_by_paradex_market = {
            item.paradex_market: item.symbol for item in symbols
        }
        self.writer = ParquetBatchWriter(config.base_dir)
        self.quotes: dict[tuple[str, str], VenueQuote] = {}
        self.books: dict[tuple[str, str], VenueBook] = {}
        self.active_events: dict[tuple[str, str], ActiveEvent] = {}
        self.lock = asyncio.Lock()

    @classmethod
    def discover_symbols(cls, config: TrackerConfig) -> list[SymbolConfig]:
        hyperliquid = HyperliquidClient()
        paradex = ParadexClient()
        paradex_symbols = set(paradex.perp_mid_prices())
        xyz_markets = sorted(hyperliquid.xyz_markets(), key=lambda market: market.day_ntl_vlm, reverse=True)
        discovered: list[SymbolConfig] = []
        for market in xyz_markets:
            if market.symbol not in paradex_symbols:
                continue
            if config.symbols and market.symbol not in config.symbols:
                continue
            discovered.append(
                SymbolConfig(
                    symbol=market.symbol,
                    hyperliquid_coin=market.coin,
                    paradex_market=f"{market.symbol}-USD-PERP",
                    day_volume_usd=market.day_ntl_vlm,
                )
            )
            if len(discovered) >= config.top_symbols and not config.symbols:
                break
        return discovered

    async def on_quote(self, quote: VenueQuote) -> None:
        now_ns = time.time_ns()
        async with self.lock:
            self.quotes[(quote.symbol, quote.venue)] = quote
            fair_mid, valid_quotes = self._fair_mid(quote.symbol, now_ns)
            deviation_bps = None
            if fair_mid is not None:
                deviation_bps = bps_difference(quote.mid, fair_mid, fair_mid)
            self.writer.append(
                "quotes",
                {
                    "ts_local_ns": quote.received_ts_ns,
                    "ts_exchange_ms": quote.exchange_ts_ms,
                    "symbol": quote.symbol,
                    "venue": quote.venue,
                    "bid": decimal_to_float(quote.bid),
                    "ask": decimal_to_float(quote.ask),
                    "bid_size": decimal_to_float(quote.bid_size),
                    "ask_size": decimal_to_float(quote.ask_size),
                    "mid": decimal_to_float(quote.mid),
                    "spread_bps": decimal_to_float(quote.spread_bps),
                    "fair_mid": decimal_to_float(fair_mid),
                    "quote_deviation_bps": decimal_to_float(deviation_bps),
                    "valid_venue_count": len(valid_quotes),
                },
            )
            self._evaluate_symbol(quote.symbol, now_ns, fair_mid, valid_quotes)

    async def on_book(self, book: VenueBook) -> None:
        now_ns = time.time_ns()
        async with self.lock:
            self.books[(book.symbol, book.venue)] = book
            fair_mid, valid_quotes = self._fair_mid(book.symbol, now_ns)
            self._evaluate_symbol(book.symbol, now_ns, fair_mid, valid_quotes)

    async def maintenance_loop(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=self.config.maintenance_interval_s)
            except TimeoutError:
                pass
            async with self.lock:
                now_ns = time.time_ns()
                for symbol in self.symbols:
                    fair_mid, valid_quotes = self._fair_mid(symbol, now_ns)
                    self._evaluate_symbol(symbol, now_ns, fair_mid, valid_quotes)

    async def flush_loop(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=self.config.flush_interval_s)
            except TimeoutError:
                pass
            async with self.lock:
                self.writer.flush()

    async def flush_once(self) -> None:
        async with self.lock:
            self.writer.flush()

    async def shutdown(self) -> None:
        async with self.lock:
            now_ns = time.time_ns()
            for key in list(self.active_events):
                fair_mid, _ = self._fair_mid(key[0], now_ns)
                self._close_event(key, now_ns, fair_mid)
            self.writer.flush()

    def _fair_mid(self, symbol: str, now_ns: int) -> tuple[Decimal | None, list[VenueQuote]]:
        valid_quotes: list[VenueQuote] = []
        max_age_ns = self.config.max_quote_age_ms * 1_000_000
        for venue in (VENUE_HYPERLIQUID_XYZ, VENUE_PARADEX):
            quote = self.quotes.get((symbol, venue))
            if quote is None:
                continue
            if now_ns - quote.received_ts_ns > max_age_ns:
                continue
            if quote.bid <= 0 or quote.ask <= 0 or quote.bid >= quote.ask:
                continue
            if quote.spread_bps > self.config.max_spread_bps:
                continue
            valid_quotes.append(quote)
        fair_mid = median_decimal(quote.mid for quote in valid_quotes)
        return fair_mid, valid_quotes

    def _evaluate_symbol(
        self,
        symbol: str,
        now_ns: int,
        fair_mid: Decimal | None,
        valid_quotes: list[VenueQuote],
    ) -> None:
        qualifying_keys: set[tuple[str, str]] = set()
        if fair_mid is not None and len(valid_quotes) >= 2:
            for quote in valid_quotes:
                quote_deviation_bps = bps_difference(quote.mid, fair_mid, fair_mid)
                key = (symbol, quote.venue)
                existing = self.active_events.get(key)
                min_quote_deviation = self.config.exit_edge_bps if existing is not None else self.config.min_quote_deviation_bps
                if quote_deviation_bps < min_quote_deviation:
                    continue
                source_book = self.books.get((symbol, quote.venue))
                if source_book is None:
                    continue
                hedge_books = [
                    book
                    for (book_symbol, book_venue), book in self.books.items()
                    if book_symbol == symbol and book_venue != quote.venue and self._book_is_fresh(book, now_ns)
                ]
                route = simulate_sell_route(
                    source_venue=quote.venue,
                    source_book=source_book,
                    hedge_books=hedge_books,
                    fair_mid=fair_mid,
                    fee_bps_by_venue=self.config.fee_bps_by_venue,
                    min_fill_edge_bps=self.config.min_executable_edge_bps,
                )
                if route is None:
                    continue

                qualifying_keys.add(key)
                self._open_or_update_event(
                    symbol=symbol,
                    source_venue=quote.venue,
                    now_ns=now_ns,
                    fair_mid=fair_mid,
                    quote_deviation_bps=quote_deviation_bps,
                    route=route,
                )

        for key in list(self.active_events):
            if key[0] != symbol:
                continue
            event = self.active_events[key]
            if key in qualifying_keys:
                if now_ns - event.last_depth_snapshot_ts_ns >= self.config.depth_snapshot_interval_ms * 1_000_000:
                    self._append_depth_snapshots(event, now_ns)
                continue
            if now_ns - event.last_seen_ts_ns >= self.config.maintenance_interval_s * 1_000_000_000:
                self._close_event(key, now_ns, fair_mid)

    def _book_is_fresh(self, book: VenueBook, now_ns: int) -> bool:
        return now_ns - book.received_ts_ns <= self.config.max_quote_age_ms * 1_000_000

    def _open_or_update_event(
        self,
        symbol: str,
        source_venue: str,
        now_ns: int,
        fair_mid: Decimal,
        quote_deviation_bps: Decimal,
        route: ExecutableRoute,
    ) -> None:
        key = (symbol, source_venue)
        existing = self.active_events.get(key)
        if existing is None:
            event = ActiveEvent(
                event_id=uuid.uuid4().hex,
                symbol=symbol,
                source_venue=source_venue,
                started_ts_ns=now_ns,
                last_seen_ts_ns=now_ns,
                last_depth_snapshot_ts_ns=0,
                fair_mid_at_start=fair_mid,
                peak_quote_deviation_bps=quote_deviation_bps,
                peak_top_net_edge_bps=route.top_net_edge_bps,
                peak_weighted_net_edge_bps=route.weighted_net_edge_bps,
                peak_matched_notional_usd=route.matched_notional_usd,
                current_route=route,
                was_exploitable=self._route_is_exploitable(route),
            )
            self.active_events[key] = event
            self._append_depth_snapshots(event, now_ns)
            LOGGER.info(
                "opened event symbol=%s source=%s top_net_edge_bps=%s matched_notional=%s hedge=%s",
                symbol,
                source_venue,
                route.top_net_edge_bps,
                route.matched_notional_usd,
                ",".join(route.hedge_venues),
            )
            return

        existing.last_seen_ts_ns = now_ns
        existing.peak_quote_deviation_bps = max(existing.peak_quote_deviation_bps, quote_deviation_bps)
        existing.peak_top_net_edge_bps = max(existing.peak_top_net_edge_bps, route.top_net_edge_bps)
        existing.peak_weighted_net_edge_bps = max(
            existing.peak_weighted_net_edge_bps,
            route.weighted_net_edge_bps,
        )
        existing.peak_matched_notional_usd = max(
            existing.peak_matched_notional_usd,
            route.matched_notional_usd,
        )
        existing.current_route = route
        existing.was_exploitable = existing.was_exploitable or self._route_is_exploitable(route)
        existing.samples += 1

    def _append_depth_snapshots(self, event: ActiveEvent, now_ns: int) -> None:
        for venue in (event.source_venue, *event.current_route.hedge_venues):
            book = self.books.get((event.symbol, venue))
            if book is None:
                continue
            summary = depth_summary(book)
            self.writer.append(
                "depth",
                self._depth_record(
                    event=event,
                    venue=venue,
                    book=book,
                    summary=summary,
                    ts_local_ns=now_ns,
                ),
            )
        event.last_depth_snapshot_ts_ns = now_ns

    def _depth_record(
        self,
        event: ActiveEvent,
        venue: str,
        book: VenueBook,
        summary: DepthSummary,
        ts_local_ns: int,
    ) -> dict[str, Any]:
        top_bids = [[str(level.price), str(level.size)] for level in book.bids[:10]]
        top_asks = [[str(level.price), str(level.size)] for level in book.asks[:10]]
        record: dict[str, Any] = {
            "ts_local_ns": ts_local_ns,
            "ts_exchange_ms": book.exchange_ts_ms,
            "event_id": event.event_id,
            "symbol": event.symbol,
            "event_source_venue": event.source_venue,
            "venue": venue,
            "book_mid": decimal_to_float(summary.mid),
            "best_bid": decimal_to_float(book.best_bid.price if book.best_bid else None),
            "best_ask": decimal_to_float(book.best_ask.price if book.best_ask else None),
            "top_bids_json": json.dumps(top_bids, separators=(",", ":")),
            "top_asks_json": json.dumps(top_asks, separators=(",", ":")),
        }
        for side, buckets in (("bid", summary.bid_buckets), ("ask", summary.ask_buckets)):
            for bucket, metrics in buckets.items():
                record[f"{side}_base_size_{bucket}"] = decimal_to_float(metrics.base_size)
                record[f"{side}_notional_usd_{bucket}"] = decimal_to_float(metrics.notional_usd)
        return record

    def _close_event(self, key: tuple[str, str], now_ns: int, fair_mid: Decimal | None) -> None:
        event = self.active_events.pop(key)
        duration_ms = (now_ns - event.started_ts_ns) / 1_000_000
        self.writer.append(
            "deviations",
            {
                "event_id": event.event_id,
                "symbol": event.symbol,
                "source_venue": event.source_venue,
                "hedge_venues": ",".join(event.current_route.hedge_venues),
                "started_ts_ns": event.started_ts_ns,
                "ended_ts_ns": now_ns,
                "duration_ms": duration_ms,
                "samples": event.samples,
                "fair_mid_at_start": decimal_to_float(event.fair_mid_at_start),
                "fair_mid_at_end": decimal_to_float(fair_mid),
                "peak_quote_deviation_bps": decimal_to_float(event.peak_quote_deviation_bps),
                "peak_top_net_edge_bps": decimal_to_float(event.peak_top_net_edge_bps),
                "peak_weighted_net_edge_bps": decimal_to_float(event.peak_weighted_net_edge_bps),
                "peak_matched_notional_usd": decimal_to_float(event.peak_matched_notional_usd),
                "final_weighted_net_edge_bps": decimal_to_float(
                    event.current_route.weighted_net_edge_bps
                ),
                "final_matched_notional_usd": decimal_to_float(
                    event.current_route.matched_notional_usd
                ),
                "was_exploitable": event.was_exploitable,
            },
        )
        LOGGER.info(
            "closed event symbol=%s source=%s duration_ms=%.1f peak_edge_bps=%s peak_notional=%s",
            event.symbol,
            event.source_venue,
            duration_ms,
            event.peak_top_net_edge_bps,
            event.peak_matched_notional_usd,
        )

    def _route_is_exploitable(self, route: ExecutableRoute) -> bool:
        return (
            route.top_net_edge_bps >= self.config.min_executable_edge_bps
            and route.matched_notional_usd >= self.config.min_matched_notional_usd
        )


class ParadexOrderBookCache:
    def __init__(self) -> None:
        self.books: dict[str, dict[str, dict[Decimal, Decimal]]] = {}

    def apply(self, market: str, payload: dict[str, Any]) -> VenueBook:
        state = self.books.setdefault(market, {"BUY": {}, "SELL": {}})
        if payload.get("update_type") == "s":
            state["BUY"].clear()
            state["SELL"].clear()

        for row in payload.get("inserts", []):
            self._upsert_level(state, row)
        for row in payload.get("updates", []):
            self._upsert_level(state, row)
        for row in payload.get("deletes", []):
            self._delete_level(state, row)

        bids = tuple(
            BookLevel(venue=VENUE_PARADEX, price=price, size=size)
            for price, size in sorted(state["BUY"].items(), key=lambda item: item[0], reverse=True)
        )
        asks = tuple(
            BookLevel(venue=VENUE_PARADEX, price=price, size=size)
            for price, size in sorted(state["SELL"].items(), key=lambda item: item[0])
        )
        symbol = market.removesuffix("-USD-PERP")
        return VenueBook(
            symbol=symbol,
            venue=VENUE_PARADEX,
            bids=bids,
            asks=asks,
            exchange_ts_ms=int(payload.get("last_updated_at")) if payload.get("last_updated_at") else None,
            received_ts_ns=time.time_ns(),
        )

    def _upsert_level(self, state: dict[str, dict[Decimal, Decimal]], row: dict[str, Any]) -> None:
        side = str(row["side"])
        price = decimal_from_text(row["price"])
        size = decimal_from_text(row["size"])
        if price is None or size is None:
            return
        if size == 0:
            state[side].pop(price, None)
            return
        state[side][price] = size

    def _delete_level(self, state: dict[str, dict[Decimal, Decimal]], row: dict[str, Any]) -> None:
        side = str(row["side"])
        price = decimal_from_text(row["price"])
        if price is None:
            return
        state[side].pop(price, None)


async def run_hyperliquid_feed(
    tracker: FairPriceTracker,
    stop_event: asyncio.Event,
) -> None:
    symbol_configs = list(tracker.symbols.values())
    while not stop_event.is_set():
        try:
            async with websockets.connect(HYPERLIQUID_WS_URL, ping_interval=20, ping_timeout=20) as websocket:
                await _subscribe_hyperliquid(websocket, symbol_configs)
                LOGGER.info("connected hyperliquid feed symbols=%d", len(symbol_configs))
                async for raw_message in websocket:
                    if stop_event.is_set():
                        break
                    message = json.loads(raw_message)
                    channel = message.get("channel")
                    if channel == "subscriptionResponse":
                        continue
                    if channel == "bbo":
                        await _handle_hyperliquid_bbo(tracker, message)
                    elif channel == "l2Book":
                        await _handle_hyperliquid_l2book(tracker, message)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            LOGGER.warning("hyperliquid feed error: %s", exc, exc_info=True)
            await asyncio.sleep(2)


async def _subscribe_hyperliquid(websocket: ClientConnection, symbols: list[SymbolConfig]) -> None:
    for item in symbols:
        await websocket.send(
            json.dumps({"method": "subscribe", "subscription": {"type": "bbo", "coin": item.hyperliquid_coin}})
        )
        await websocket.send(
            json.dumps(
                {"method": "subscribe", "subscription": {"type": "l2Book", "coin": item.hyperliquid_coin}}
            )
        )


async def _handle_hyperliquid_bbo(tracker: FairPriceTracker, message: dict[str, Any]) -> None:
    data = message.get("data", {})
    coin = str(data.get("coin", ""))
    symbol = tracker.symbol_by_hyperliquid_coin.get(coin)
    bbo = data.get("bbo") or []
    if symbol is None or len(bbo) < 2:
        return
    bid = decimal_from_text(bbo[0].get("px"))
    ask = decimal_from_text(bbo[1].get("px"))
    bid_size = decimal_from_text(bbo[0].get("sz"))
    ask_size = decimal_from_text(bbo[1].get("sz"))
    if bid is None or ask is None:
        return
    await tracker.on_quote(
        VenueQuote(
            symbol=symbol,
            venue=VENUE_HYPERLIQUID_XYZ,
            bid=bid,
            ask=ask,
            bid_size=bid_size,
            ask_size=ask_size,
            exchange_ts_ms=int(data.get("time")) if data.get("time") else None,
            received_ts_ns=time.time_ns(),
        )
    )


async def _handle_hyperliquid_l2book(tracker: FairPriceTracker, message: dict[str, Any]) -> None:
    data = message.get("data", {})
    coin = str(data.get("coin", ""))
    symbol = tracker.symbol_by_hyperliquid_coin.get(coin)
    levels = data.get("levels") or []
    if symbol is None or len(levels) < 2:
        return

    bids = tuple(
        BookLevel(
            venue=VENUE_HYPERLIQUID_XYZ,
            price=decimal_from_text(level["px"]) or Decimal("0"),
            size=decimal_from_text(level["sz"]) or Decimal("0"),
        )
        for level in levels[0]
    )
    asks = tuple(
        BookLevel(
            venue=VENUE_HYPERLIQUID_XYZ,
            price=decimal_from_text(level["px"]) or Decimal("0"),
            size=decimal_from_text(level["sz"]) or Decimal("0"),
        )
        for level in levels[1]
    )
    await tracker.on_book(
        VenueBook(
            symbol=symbol,
            venue=VENUE_HYPERLIQUID_XYZ,
            bids=tuple(level for level in bids if level.price > 0 and level.size > 0),
            asks=tuple(level for level in asks if level.price > 0 and level.size > 0),
            exchange_ts_ms=int(data.get("time")) if data.get("time") else None,
            received_ts_ns=time.time_ns(),
        )
    )


async def run_paradex_feed(
    tracker: FairPriceTracker,
    stop_event: asyncio.Event,
) -> None:
    symbol_configs = list(tracker.symbols.values())
    book_cache = ParadexOrderBookCache()
    while not stop_event.is_set():
        try:
            async with websockets.connect(PARADEX_WS_URL, ping_interval=20, ping_timeout=20) as websocket:
                await _subscribe_paradex(websocket, symbol_configs)
                LOGGER.info("connected paradex feed symbols=%d", len(symbol_configs))
                async for raw_message in websocket:
                    if stop_event.is_set():
                        break
                    message = json.loads(raw_message)
                    if message.get("method") != "subscription":
                        continue
                    params = message.get("params", {})
                    channel = str(params.get("channel", ""))
                    data = params.get("data", {})
                    if channel.startswith("markets_summary."):
                        await _handle_paradex_summary(tracker, channel, data)
                    elif channel.startswith("order_book."):
                        await _handle_paradex_order_book(tracker, channel, data, book_cache)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            LOGGER.warning("paradex feed error: %s", exc, exc_info=True)
            await asyncio.sleep(2)


async def _subscribe_paradex(websocket: ClientConnection, symbols: list[SymbolConfig]) -> None:
    request_id = 1
    for item in symbols:
        await websocket.send(
            json.dumps(
                {
                    "id": request_id,
                    "jsonrpc": "2.0",
                    "method": "subscribe",
                    "params": {"channel": f"markets_summary.{item.paradex_market}"},
                }
            )
        )
        request_id += 1
        await websocket.send(
            json.dumps(
                {
                    "id": request_id,
                    "jsonrpc": "2.0",
                    "method": "subscribe",
                    "params": {"channel": f"order_book.{item.paradex_market}.snapshot@15@100ms"},
                }
            )
        )
        request_id += 1


async def _handle_paradex_summary(
    tracker: FairPriceTracker,
    channel: str,
    data: dict[str, Any],
) -> None:
    market = channel.split(".", 1)[1]
    symbol = tracker.symbol_by_paradex_market.get(market)
    if symbol is None:
        return
    bid = decimal_from_text(data.get("bid"))
    ask = decimal_from_text(data.get("ask"))
    bid_size = decimal_from_text(data.get("bid_size"))
    ask_size = decimal_from_text(data.get("ask_size"))
    if bid is None or ask is None:
        return
    await tracker.on_quote(
        VenueQuote(
            symbol=symbol,
            venue=VENUE_PARADEX,
            bid=bid,
            ask=ask,
            bid_size=bid_size,
            ask_size=ask_size,
            exchange_ts_ms=int(data.get("created_at")) if data.get("created_at") else None,
            received_ts_ns=time.time_ns(),
        )
    )


async def _handle_paradex_order_book(
    tracker: FairPriceTracker,
    channel: str,
    data: dict[str, Any],
    cache: ParadexOrderBookCache,
) -> None:
    parts = channel.split(".")
    if len(parts) < 2:
        return
    market = parts[1]
    symbol = tracker.symbol_by_paradex_market.get(market)
    if symbol is None:
        return
    book = cache.apply(market, data)
    await tracker.on_book(book)


async def run_tracker(config: TrackerConfig) -> None:
    symbols = FairPriceTracker.discover_symbols(config)
    if not symbols:
        raise RuntimeError("No live overlap symbols discovered for fair-price tracking.")

    tracker = FairPriceTracker(config=config, symbols=symbols)
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signame in ("SIGINT", "SIGTERM"):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(getattr(signal, signame), stop_event.set)

    LOGGER.info(
        "starting fair-price tracker base_dir=%s symbols=%s",
        config.base_dir,
        ",".join(item.symbol for item in symbols),
    )
    tasks: list[asyncio.Task[Any]] = []
    try:
        tasks = [
            asyncio.create_task(run_hyperliquid_feed(tracker, stop_event), name="hyperliquid-feed"),
            asyncio.create_task(run_paradex_feed(tracker, stop_event), name="paradex-feed"),
            asyncio.create_task(tracker.flush_loop(stop_event), name="flush-loop"),
            asyncio.create_task(tracker.maintenance_loop(stop_event), name="maintenance-loop"),
        ]
        await stop_event.wait()
    finally:
        stop_event.set()
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await tracker.shutdown()
