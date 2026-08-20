from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable


BPS_SCALE = Decimal("10000")
DEFAULT_BUCKETS_BPS = (Decimal("5"), Decimal("10"), Decimal("25"))


def midpoint(bid: Decimal, ask: Decimal) -> Decimal:
    return (bid + ask) / Decimal("2")


def bps_difference(left: Decimal, right: Decimal, anchor: Decimal) -> Decimal:
    if anchor == 0:
        return Decimal("0")
    return ((left - right) / anchor) * BPS_SCALE


def median_decimal(values: Iterable[Decimal]) -> Decimal | None:
    ordered = sorted(values)
    if not ordered:
        return None
    middle = len(ordered) // 2
    if len(ordered) % 2 == 1:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / Decimal("2")


def format_bucket_bps(value: Decimal) -> str:
    if value == value.to_integral():
        text = str(int(value))
    else:
        normalized = value.normalize()
        text = format(normalized, "f").rstrip("0").rstrip(".")
    return f"{text or '0'}bps"


@dataclass(frozen=True)
class BookLevel:
    venue: str
    price: Decimal
    size: Decimal


@dataclass(frozen=True)
class VenueQuote:
    symbol: str
    venue: str
    bid: Decimal
    ask: Decimal
    bid_size: Decimal | None
    ask_size: Decimal | None
    exchange_ts_ms: int | None
    received_ts_ns: int

    @property
    def mid(self) -> Decimal:
        return midpoint(self.bid, self.ask)

    @property
    def spread_bps(self) -> Decimal:
        return bps_difference(self.ask, self.bid, self.mid)


@dataclass(frozen=True)
class VenueBook:
    symbol: str
    venue: str
    bids: tuple[BookLevel, ...]
    asks: tuple[BookLevel, ...]
    exchange_ts_ms: int | None
    received_ts_ns: int

    @property
    def best_bid(self) -> BookLevel | None:
        return self.bids[0] if self.bids else None

    @property
    def best_ask(self) -> BookLevel | None:
        return self.asks[0] if self.asks else None

    @property
    def mid(self) -> Decimal | None:
        if self.best_bid is None or self.best_ask is None:
            return None
        return midpoint(self.best_bid.price, self.best_ask.price)


@dataclass(frozen=True)
class DepthBucket:
    base_size: Decimal
    notional_usd: Decimal


@dataclass(frozen=True)
class DepthSummary:
    mid: Decimal | None
    bid_buckets: dict[str, DepthBucket]
    ask_buckets: dict[str, DepthBucket]


@dataclass(frozen=True)
class ExecutableRoute:
    source_venue: str
    hedge_venues: tuple[str, ...]
    top_gross_edge_bps: Decimal
    top_net_edge_bps: Decimal
    weighted_net_edge_bps: Decimal
    matched_base_size: Decimal
    matched_notional_usd: Decimal
    source_fill_notional_usd: Decimal
    hedge_fill_notional_usd: Decimal


def depth_summary(
    book: VenueBook,
    bucket_thresholds_bps: tuple[Decimal, ...] = DEFAULT_BUCKETS_BPS,
) -> DepthSummary:
    mid = book.mid
    bid_buckets: dict[str, DepthBucket] = {}
    ask_buckets: dict[str, DepthBucket] = {}
    if mid is None or mid == 0:
        for bucket in bucket_thresholds_bps:
            key = format_bucket_bps(bucket)
            bid_buckets[key] = DepthBucket(base_size=Decimal("0"), notional_usd=Decimal("0"))
            ask_buckets[key] = DepthBucket(base_size=Decimal("0"), notional_usd=Decimal("0"))
        return DepthSummary(mid=mid, bid_buckets=bid_buckets, ask_buckets=ask_buckets)

    for bucket in bucket_thresholds_bps:
        bid_limit = mid * (Decimal("1") - (bucket / BPS_SCALE))
        ask_limit = mid * (Decimal("1") + (bucket / BPS_SCALE))
        bid_size = Decimal("0")
        bid_notional = Decimal("0")
        ask_size = Decimal("0")
        ask_notional = Decimal("0")

        for level in book.bids:
            if level.price < bid_limit:
                break
            bid_size += level.size
            bid_notional += level.size * level.price

        for level in book.asks:
            if level.price > ask_limit:
                break
            ask_size += level.size
            ask_notional += level.size * level.price

        key = format_bucket_bps(bucket)
        bid_buckets[key] = DepthBucket(base_size=bid_size, notional_usd=bid_notional)
        ask_buckets[key] = DepthBucket(base_size=ask_size, notional_usd=ask_notional)

    return DepthSummary(mid=mid, bid_buckets=bid_buckets, ask_buckets=ask_buckets)


def simulate_sell_route(
    source_venue: str,
    source_book: VenueBook,
    hedge_books: Iterable[VenueBook],
    fair_mid: Decimal,
    fee_bps_by_venue: dict[str, Decimal],
    min_fill_edge_bps: Decimal,
) -> ExecutableRoute | None:
    if fair_mid == 0 or source_book.best_bid is None:
        return None

    source_bids = [level for level in source_book.bids if level.size > 0]
    hedge_asks = sorted(
        [
            BookLevel(venue=level.venue, price=level.price, size=level.size)
            for book in hedge_books
            for level in book.asks
            if level.size > 0
        ],
        key=lambda level: level.price,
    )
    if not source_bids or not hedge_asks:
        return None

    top_sell = source_bids[0]
    top_buy = hedge_asks[0]
    top_gross = bps_difference(top_sell.price, top_buy.price, fair_mid)
    top_net = top_gross - fee_bps_by_venue.get(source_venue, Decimal("0")) - fee_bps_by_venue.get(
        top_buy.venue, Decimal("0")
    )

    bid_index = 0
    ask_index = 0
    bid_remaining = source_bids[0].size
    ask_remaining = hedge_asks[0].size
    matched_base = Decimal("0")
    matched_notional = Decimal("0")
    source_fill_notional = Decimal("0")
    hedge_fill_notional = Decimal("0")
    weighted_net_numerator = Decimal("0")
    hedge_venues: list[str] = []

    while bid_index < len(source_bids) and ask_index < len(hedge_asks):
        bid_level = source_bids[bid_index]
        ask_level = hedge_asks[ask_index]
        gross_edge = bps_difference(bid_level.price, ask_level.price, fair_mid)
        net_edge = gross_edge - fee_bps_by_venue.get(source_venue, Decimal("0")) - fee_bps_by_venue.get(
            ask_level.venue, Decimal("0")
        )
        if net_edge < min_fill_edge_bps:
            break

        trade_size = min(bid_remaining, ask_remaining)
        trade_notional = trade_size * ((bid_level.price + ask_level.price) / Decimal("2"))
        matched_base += trade_size
        matched_notional += trade_notional
        source_fill_notional += trade_size * bid_level.price
        hedge_fill_notional += trade_size * ask_level.price
        weighted_net_numerator += net_edge * trade_notional
        if ask_level.venue not in hedge_venues:
            hedge_venues.append(ask_level.venue)

        bid_remaining -= trade_size
        ask_remaining -= trade_size

        if bid_remaining == 0:
            bid_index += 1
            if bid_index < len(source_bids):
                bid_remaining = source_bids[bid_index].size
        if ask_remaining == 0:
            ask_index += 1
            if ask_index < len(hedge_asks):
                ask_remaining = hedge_asks[ask_index].size

    weighted_net_edge_bps = (
        weighted_net_numerator / matched_notional if matched_notional > 0 else Decimal("0")
    )

    return ExecutableRoute(
        source_venue=source_venue,
        hedge_venues=tuple(hedge_venues),
        top_gross_edge_bps=top_gross,
        top_net_edge_bps=top_net,
        weighted_net_edge_bps=weighted_net_edge_bps,
        matched_base_size=matched_base,
        matched_notional_usd=matched_notional,
        source_fill_notional_usd=source_fill_notional,
        hedge_fill_notional_usd=hedge_fill_notional,
    )
