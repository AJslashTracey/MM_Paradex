from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from .utils import decimal_places


QuoteSide = Literal["bid", "ask"]
TradeSide = Literal["buy", "sell"]


@dataclass(frozen=True)
class Level:
    px: float
    sz: float
    raw_px: str
    raw_sz: str


@dataclass
class VenueBook:
    bids: list[Level] = field(default_factory=list)
    asks: list[Level] = field(default_factory=list)
    exchange_time_ms: int | None = None
    recv_time_ms: int | None = None
    source: str = ""

    def best_bid(self) -> Level | None:
        return self.bids[0] if self.bids else None

    def best_ask(self) -> Level | None:
        return self.asks[0] if self.asks else None

    def mid(self) -> float | None:
        bid = self.best_bid()
        ask = self.best_ask()
        if bid is None or ask is None:
            return None
        return (bid.px + ask.px) / 2.0

    def spread_bps(self) -> float | None:
        bid = self.best_bid()
        ask = self.best_ask()
        mid = self.mid()
        if bid is None or ask is None or mid in (None, 0):
            return None
        return (ask.px - bid.px) / mid * 10_000

    def price_decimals(self) -> int:
        bid = self.best_bid()
        ask = self.best_ask()
        return max(decimal_places("" if bid is None else bid.raw_px), decimal_places("" if ask is None else ask.raw_px))


@dataclass(frozen=True)
class TradeTick:
    venue: str
    symbol: str
    side: TradeSide
    px: float
    sz: float
    exchange_time_ms: int | None
    recv_time_ms: int
    trade_id: str = ""
    raw: dict[str, Any] | None = None


@dataclass
class VenueState:
    venue: str
    symbol: str
    connected: bool = False
    book: VenueBook | None = None
    mark_px: float | None = None
    oracle_px: float | None = None
    last_trade: TradeTick | None = None
    last_message_ms: int | None = None
    last_disconnect_reason: str | None = None
    connection_seq: int = 0

    def best_bid(self) -> Level | None:
        if self.book is None:
            return None
        return self.book.best_bid()

    def best_ask(self) -> Level | None:
        if self.book is None:
            return None
        return self.book.best_ask()

    def mid(self) -> float | None:
        if self.book is None:
            return None
        return self.book.mid()

    def spread_bps(self) -> float | None:
        if self.book is None:
            return None
        return self.book.spread_bps()

    def book_age_ms(self, observed_ms: int) -> int | None:
        if self.book is None or self.book.recv_time_ms is None:
            return None
        return max(0, observed_ms - self.book.recv_time_ms)

    def is_fresh(self, observed_ms: int, max_age_ms: int) -> bool:
        age = self.book_age_ms(observed_ms)
        return age is not None and age <= max_age_ms


@dataclass(frozen=True)
class FairValueSnapshot:
    time_ms: int
    strategy_mode: str
    basis_raw: float | None
    basis_ema: float | None
    fair_px: float | None
    baseline_io_mid_px: float | None
    binance_ret_1s_bps: float | None
    binance_ret_5s_bps: float | None
    binance_ret_10s_bps: float | None
    io_deviation_bps: float | None
    io_spread_bps: float | None
    rapid_move_side: str | None
    rapid_move_bps: float | None
    recent_rapid_move_time_ms: int | None


@dataclass(frozen=True)
class QuoteIntent:
    quote_side: QuoteSide
    is_buy: bool
    px: float
    size: float
    clamped_to_book: bool = False


@dataclass(frozen=True)
class QuotePlan:
    time_ms: int
    fair_px: float
    base_half_spread_bps: float
    inventory_skew_bps: float
    bid: QuoteIntent | None
    ask: QuoteIntent | None


@dataclass
class ActiveOrder:
    quote_side: QuoteSide
    is_buy: bool
    cloid_raw: str
    price: float
    size: float
    placed_time_ms: int
    order_id: int | None = None
    remaining_size: float | None = None
    status: str = "resting"

    @property
    def open_notional(self) -> float:
        remaining = self.size if self.remaining_size is None else self.remaining_size
        return abs(remaining) * self.price


@dataclass(frozen=True)
class RiskDecision:
    quoting_allowed: bool
    should_cancel_all: bool
    reason: str | None
    block_bid: bool = False
    block_ask: bool = False


@dataclass(frozen=True)
class InventorySnapshot:
    time_ms: int
    inventory: float
    avg_entry_px: float | None
    realized_pnl: float
    unrealized_pnl: float | None
    total_fees: float
    max_abs_inventory: float
    avg_abs_inventory: float


@dataclass(frozen=True)
class FillRecord:
    fill_key: str
    time_ms: int
    side: TradeSide
    size: float
    price: float
    inventory_before: float
    inventory_after: float
    realized_pnl_delta: float
    total_realized_pnl: float
    fee: float
    fee_token: str
    order_id: int | None
    trade_id: int | None
    tx_hash: str
    crossed: bool
    dir_label: str
    after_binance_move: bool
    rapid_move_side: str
    fair_px: float | None
    io_mid_px: float | None
    basis_ema: float | None
    binance_ret_1s_bps: float | None
    binance_ret_5s_bps: float | None
    binance_ret_10s_bps: float | None
    edge_vs_io_mid_bps: float | None
    edge_vs_fair_bps: float | None
    raw: dict[str, Any]


@dataclass
class BotState:
    hl: VenueState
    binance: VenueState
    fair_value: FairValueSnapshot | None = None
    quote_plan: QuotePlan | None = None
    risk: RiskDecision | None = None

