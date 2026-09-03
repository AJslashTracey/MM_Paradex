from __future__ import annotations

from collections import deque

from .basis_estimator import BasisEstimator
from .config import MMConfig
from .models import FairValueSnapshot, VenueState
from .utils import bps_ratio, now_ms


class FairValueModel:
    def __init__(self, config: MMConfig) -> None:
        self.config = config
        self.basis_estimator = BasisEstimator(config.basis_ema_period)
        self.binance_history: deque[tuple[int, float]] = deque()
        self.max_history_ms = max(config.markout_windows_s) * 1000 + 10_000
        self.last_binance_recv_ms: int | None = None
        self.last_rapid_move_source_ms: int | None = None
        self.last_rapid_move_time_ms: int | None = None
        self.last_rapid_move_side: str | None = None
        self.last_rapid_move_bps: float | None = None

    def update(self, hl_state: VenueState, binance_state: VenueState, observed_ms: int | None = None) -> FairValueSnapshot | None:
        if observed_ms is None:
            observed_ms = now_ms()
        io_mid = hl_state.mid()
        binance_mid = binance_state.mid()
        if io_mid is None or binance_mid is None:
            return None

        book = binance_state.book
        if book is not None and book.recv_time_ms is not None and book.recv_time_ms != self.last_binance_recv_ms:
            self.binance_history.append((book.recv_time_ms, binance_mid))
            self.last_binance_recv_ms = book.recv_time_ms
            self._trim_history(book.recv_time_ms)
            self._detect_rapid_move(book.recv_time_ms, binance_mid)

        sample_key = (
            None if hl_state.book is None else hl_state.book.recv_time_ms,
            None if binance_state.book is None else binance_state.book.recv_time_ms,
        )
        basis_raw, basis_ema = self.basis_estimator.update(io_mid, binance_mid, sample_key)
        fair_px = io_mid if self.config.strategy_mode == "io_mid" else binance_mid * (1.0 + basis_ema)
        io_deviation_bps = bps_ratio(io_mid, fair_px)
        io_spread_bps = hl_state.spread_bps()

        return FairValueSnapshot(
            time_ms=observed_ms,
            strategy_mode=self.config.strategy_mode,
            basis_raw=basis_raw,
            basis_ema=basis_ema,
            fair_px=fair_px,
            baseline_io_mid_px=io_mid,
            binance_ret_1s_bps=self._return_bps(observed_ms, 1_000, binance_mid),
            binance_ret_5s_bps=self._return_bps(observed_ms, 5_000, binance_mid),
            binance_ret_10s_bps=self._return_bps(observed_ms, 10_000, binance_mid),
            io_deviation_bps=io_deviation_bps,
            io_spread_bps=io_spread_bps,
            rapid_move_side=self.last_rapid_move_side,
            rapid_move_bps=self.last_rapid_move_bps,
            recent_rapid_move_time_ms=self.last_rapid_move_time_ms,
        )

    def _trim_history(self, current_ms: int) -> None:
        cutoff = current_ms - self.max_history_ms
        while self.binance_history and self.binance_history[0][0] < cutoff:
            self.binance_history.popleft()

    def _reference_price(self, observed_ms: int, window_ms: int) -> float | None:
        target = observed_ms - window_ms
        candidate: float | None = None
        for ts, px in self.binance_history:
            if ts <= target:
                candidate = px
            else:
                break
        return candidate

    def _return_bps(self, observed_ms: int, window_ms: int, current_px: float) -> float | None:
        reference = self._reference_price(observed_ms, window_ms)
        return bps_ratio(current_px, reference)

    def _detect_rapid_move(self, observed_ms: int, current_px: float) -> None:
        if observed_ms == self.last_rapid_move_source_ms:
            return
        move_bps = self._return_bps(observed_ms, int(self.config.rapid_move_window_s * 1000), current_px)
        if move_bps is None or abs(move_bps) < self.config.rapid_move_threshold_bps:
            return
        self.last_rapid_move_source_ms = observed_ms
        self.last_rapid_move_time_ms = observed_ms
        self.last_rapid_move_side = "up" if move_bps > 0 else "down"
        self.last_rapid_move_bps = move_bps

