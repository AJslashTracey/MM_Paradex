from __future__ import annotations

from .config import MMConfig
from .models import FairValueSnapshot, RiskDecision, VenueState


class RiskManager:
    def __init__(self, config: MMConfig) -> None:
        self.config = config
        self._unhealthy_reason: str | None = None
        self._unhealthy_since_ms: int | None = None

    def evaluate(
        self,
        hl_state: VenueState,
        binance_state: VenueState,
        fair_value: FairValueSnapshot | None,
        inventory: float,
        open_notional: float,
        observed_ms: int,
    ) -> RiskDecision:
        reason = self._pause_or_cancel_reason(hl_state, binance_state, fair_value, open_notional, observed_ms)
        if reason is not None:
            return self._risk_decision_for_reason(reason, observed_ms)

        self._unhealthy_reason = None
        self._unhealthy_since_ms = None
        block_bid = inventory >= self.config.hard_inventory_limit
        block_ask = inventory <= -self.config.hard_inventory_limit
        return RiskDecision(True, False, None, block_bid=block_bid, block_ask=block_ask)

    def _pause_or_cancel_reason(
        self,
        hl_state: VenueState,
        binance_state: VenueState,
        fair_value: FairValueSnapshot | None,
        open_notional: float,
        observed_ms: int,
    ) -> str | None:
        if not hl_state.connected or not binance_state.connected:
            return "feed_disconnected"
        if not hl_state.is_fresh(observed_ms, self.config.max_data_age_ms):
            return "hyperliquid_stale"
        if not binance_state.is_fresh(observed_ms, self.config.max_data_age_ms):
            return "binance_stale"
        if hl_state.book is None or binance_state.book is None:
            return "missing_book"
        if hl_state.book.recv_time_ms is None or binance_state.book.recv_time_ms is None:
            return "missing_recv_time"
        if abs(hl_state.book.recv_time_ms - binance_state.book.recv_time_ms) > self.config.max_cross_recv_skew_ms:
            return "cross_recv_skew"
        if fair_value is None or fair_value.fair_px is None:
            return "fair_unavailable"
        if fair_value.io_deviation_bps is not None and abs(fair_value.io_deviation_bps) > self.config.max_fair_deviation_bps:
            return "fair_deviation"
        if open_notional > self.config.max_open_notional:
            return "open_notional_limit"
        return None

    def _risk_decision_for_reason(self, reason: str, observed_ms: int) -> RiskDecision:
        if reason in {"fair_deviation", "open_notional_limit"}:
            return RiskDecision(False, True, reason)

        if self._unhealthy_reason != reason:
            self._unhealthy_reason = reason
            self._unhealthy_since_ms = observed_ms
        since_ms = observed_ms if self._unhealthy_since_ms is None else self._unhealthy_since_ms
        should_cancel = observed_ms - since_ms >= self.config.feed_unhealthy_cancel_grace_ms
        return RiskDecision(False, should_cancel, reason)
