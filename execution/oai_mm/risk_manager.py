from __future__ import annotations

from .config import MMConfig
from .models import FairValueSnapshot, RiskDecision, VenueState


class RiskManager:
    def __init__(self, config: MMConfig) -> None:
        self.config = config

    def evaluate(
        self,
        hl_state: VenueState,
        binance_state: VenueState,
        fair_value: FairValueSnapshot | None,
        inventory: float,
        open_notional: float,
        observed_ms: int,
    ) -> RiskDecision:
        if not hl_state.connected or not binance_state.connected:
            return RiskDecision(False, True, "feed_disconnected")
        if not hl_state.is_fresh(observed_ms, self.config.max_data_age_ms):
            return RiskDecision(False, True, "hyperliquid_stale")
        if not binance_state.is_fresh(observed_ms, self.config.max_data_age_ms):
            return RiskDecision(False, True, "binance_stale")
        if hl_state.book is None or binance_state.book is None:
            return RiskDecision(False, True, "missing_book")
        if hl_state.book.recv_time_ms is None or binance_state.book.recv_time_ms is None:
            return RiskDecision(False, True, "missing_recv_time")
        if abs(hl_state.book.recv_time_ms - binance_state.book.recv_time_ms) > self.config.max_cross_recv_skew_ms:
            return RiskDecision(False, True, "cross_recv_skew")
        if fair_value is None or fair_value.fair_px is None:
            return RiskDecision(False, True, "fair_unavailable")
        if fair_value.io_deviation_bps is not None and abs(fair_value.io_deviation_bps) > self.config.max_fair_deviation_bps:
            return RiskDecision(False, True, "fair_deviation")
        if open_notional > self.config.max_open_notional:
            return RiskDecision(False, True, "open_notional_limit")

        block_bid = inventory >= self.config.hard_inventory_limit
        block_ask = inventory <= -self.config.hard_inventory_limit
        return RiskDecision(True, False, None, block_bid=block_bid, block_ask=block_ask)

