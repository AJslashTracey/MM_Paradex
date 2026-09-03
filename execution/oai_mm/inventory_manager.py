from __future__ import annotations

import math

from .models import InventorySnapshot


class InventoryManager:
    def __init__(self) -> None:
        self.inventory = 0.0
        self.avg_entry_px: float | None = None
        self.realized_pnl = 0.0
        self.total_fees = 0.0
        self.max_abs_inventory = 0.0
        self._started_ms: int | None = None
        self._last_update_ms: int | None = None
        self._abs_inventory_ms = 0.0

    def observe_time(self, observed_ms: int) -> None:
        if self._started_ms is None:
            self._started_ms = observed_ms
            self._last_update_ms = observed_ms
            return
        if self._last_update_ms is None:
            self._last_update_ms = observed_ms
            return
        delta_ms = max(0, observed_ms - self._last_update_ms)
        self._abs_inventory_ms += abs(self.inventory) * delta_ms
        self._last_update_ms = observed_ms

    def apply_fill(self, is_buy: bool, size: float, price: float, fee: float, observed_ms: int) -> tuple[float, float]:
        self.observe_time(observed_ms)
        before = self.inventory
        signed_qty = size if is_buy else -size
        realized_delta = 0.0

        if before == 0 or math.copysign(1.0, before) == math.copysign(1.0, signed_qty):
            new_inventory = before + signed_qty
            if self.avg_entry_px is None or before == 0:
                self.avg_entry_px = price
            else:
                total_size = abs(before) + abs(signed_qty)
                if total_size > 0:
                    self.avg_entry_px = ((abs(before) * self.avg_entry_px) + (abs(signed_qty) * price)) / total_size
            self.inventory = new_inventory
        else:
            close_qty = min(abs(before), abs(signed_qty))
            entry_px = self.avg_entry_px if self.avg_entry_px is not None else price
            if before > 0:
                realized_delta = close_qty * (price - entry_px)
            else:
                realized_delta = close_qty * (entry_px - price)
            self.realized_pnl += realized_delta
            self.inventory = before + signed_qty
            if self.inventory == 0:
                self.avg_entry_px = None
            elif math.copysign(1.0, self.inventory) != math.copysign(1.0, before):
                self.avg_entry_px = price

        self.total_fees += fee
        self.max_abs_inventory = max(self.max_abs_inventory, abs(self.inventory))
        return before, self.inventory

    def inventory_skew_bps(self, soft_limit: float, max_skew_bps: float) -> float:
        if soft_limit <= 0 or max_skew_bps <= 0:
            return 0.0
        scaled = max(-1.0, min(1.0, self.inventory / soft_limit))
        return -scaled * max_skew_bps

    def unrealized_pnl(self, mark_px: float | None) -> float | None:
        if mark_px is None or self.avg_entry_px is None or self.inventory == 0:
            return 0.0 if self.inventory == 0 else None
        return self.inventory * (mark_px - self.avg_entry_px)

    def snapshot(self, observed_ms: int, mark_px: float | None) -> InventorySnapshot:
        self.observe_time(observed_ms)
        elapsed_ms = 0 if self._started_ms is None else max(1, observed_ms - self._started_ms)
        avg_abs_inventory = self._abs_inventory_ms / elapsed_ms
        return InventorySnapshot(
            time_ms=observed_ms,
            inventory=self.inventory,
            avg_entry_px=self.avg_entry_px,
            realized_pnl=self.realized_pnl,
            unrealized_pnl=self.unrealized_pnl(mark_px),
            total_fees=self.total_fees,
            max_abs_inventory=self.max_abs_inventory,
            avg_abs_inventory=avg_abs_inventory,
        )

