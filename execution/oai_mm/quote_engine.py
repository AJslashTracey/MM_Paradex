from __future__ import annotations

from .config import MMConfig
from .inventory_manager import InventoryManager
from .models import QuoteIntent, QuotePlan, VenueState
from .utils import round_price, tick_size_from_decimals


class QuoteEngine:
    def __init__(self, config: MMConfig) -> None:
        self.config = config

    def build_plan(self, hl_state: VenueState, fair_px: float, inventory: InventoryManager, observed_ms: int) -> QuotePlan | None:
        book = hl_state.book
        if book is None:
            return None
        best_bid = book.best_bid()
        best_ask = book.best_ask()
        if best_bid is None or best_ask is None:
            return None
        decimals = book.price_decimals()
        tick = tick_size_from_decimals(decimals)
        skew_bps = inventory.inventory_skew_bps(self.config.soft_inventory_limit, self.config.max_inventory_skew_bps)
        center_px = fair_px * (1.0 + skew_bps / 10_000)
        distance_px = fair_px * self.config.quote_half_spread_bps / 10_000
        raw_bid = center_px - distance_px
        raw_ask = center_px + distance_px

        bid_px = min(max(raw_bid, best_bid.px), best_ask.px - tick)
        ask_px = max(min(raw_ask, best_ask.px), best_bid.px + tick)
        bid_px = round_price(bid_px, decimals, "bid")
        ask_px = round_price(ask_px, decimals, "ask")
        if bid_px <= 0 or ask_px <= 0 or bid_px >= ask_px:
            return None

        bid = QuoteIntent(
            quote_side="bid",
            is_buy=True,
            px=bid_px,
            size=min(self.config.order_size, self.config.max_order_size),
            clamped_to_book=bid_px != raw_bid,
        )
        ask = QuoteIntent(
            quote_side="ask",
            is_buy=False,
            px=ask_px,
            size=min(self.config.order_size, self.config.max_order_size),
            clamped_to_book=ask_px != raw_ask,
        )
        return QuotePlan(
            time_ms=observed_ms,
            fair_px=fair_px,
            base_half_spread_bps=self.config.quote_half_spread_bps,
            inventory_skew_bps=skew_bps,
            bid=bid,
            ask=ask,
        )

