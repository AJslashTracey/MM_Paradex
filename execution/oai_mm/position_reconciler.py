from __future__ import annotations

from typing import Any

from execution.executor import HyperliquidExecutor, Position

from .config import MMConfig
from .inventory_manager import InventoryManager
from .models import BotState


class PositionReconciler:
    def __init__(
        self,
        config: MMConfig,
        state: BotState,
        inventory: InventoryManager,
        executor: HyperliquidExecutor | None,
        logger: Any,
    ) -> None:
        self.config = config
        self.state = state
        self.inventory = inventory
        self.executor = executor
        self.logger = logger
        self._in_flight = False
        self._last_mismatch_alert_ms = 0

    def update_internal(self) -> None:
        self.state.position_reconciliation.update_internal(
            self.inventory.inventory,
            self.config.position_reconcile_tolerance,
        )

    def seed_exchange_position(self, position: Position | None, observed_ms: int, reason: str) -> None:
        self.update_internal()
        self._apply_exchange_position(position, observed_ms)
        self._log_reconcile(reason)

    def maybe_reconcile(self, observed_ms: int) -> None:
        self.update_internal()
        if not self.config.live or self.executor is None:
            return
        if self.config.position_reconcile_interval_ms <= 0:
            return
        reconciliation = self.state.position_reconciliation
        if (
            reconciliation.last_reconcile_ms is not None
            and observed_ms - reconciliation.last_reconcile_ms < self.config.position_reconcile_interval_ms
        ):
            self._maybe_log_mismatch_alert(observed_ms)
            return
        self.reconcile(observed_ms, reason="interval")

    def reconcile(self, observed_ms: int, reason: str) -> None:
        self.update_internal()
        if not self.config.live or self.executor is None or self._in_flight:
            return
        self._in_flight = True
        try:
            position = self.executor.get_position(self.config.target_coin)
            self._apply_exchange_position(position, observed_ms)
            self._log_reconcile(reason)
        except Exception as exc:
            self.state.position_reconciliation.mark_error(str(exc), observed_ms)
            self.logger.log_event("position_reconcile_error", reason=str(exc), raw={"error": str(exc)})
        finally:
            self._in_flight = False
        self._maybe_log_mismatch_alert(observed_ms)

    def position_to_flatten(self) -> float:
        self.update_internal()
        reconciliation = self.state.position_reconciliation
        exchange_position = reconciliation.exchange_position
        if exchange_position is not None and abs(exchange_position) > self.config.position_reconcile_tolerance:
            return exchange_position
        if abs(self.inventory.inventory) > self.config.position_reconcile_tolerance:
            return self.inventory.inventory
        return 0.0

    def entry_halt_reason(self) -> str | None:
        self.update_internal()
        reconciliation = self.state.position_reconciliation
        if self.config.halt_entries_on_position_mismatch and reconciliation.mismatch:
            return "position_mismatch"
        if abs(self.position_to_flatten()) > self.config.position_reconcile_tolerance:
            return "position_open"
        return None

    def _apply_exchange_position(self, position: Position | None, observed_ms: int) -> None:
        size = 0.0 if position is None else position.size
        self.state.position_reconciliation.update_exchange(
            position=size,
            entry_px=None if position is None else position.entry_px,
            unrealized_pnl=None if position is None else position.unrealized_pnl,
            observed_ms=observed_ms,
            tolerance=self.config.position_reconcile_tolerance,
        )

    def _log_reconcile(self, reason: str) -> None:
        reconciliation = self.state.position_reconciliation
        event_reason = "mismatch" if reconciliation.mismatch else reason
        self.logger.log_event(
            "position_reconcile",
            reason=event_reason,
            raw={
                "reason": reason,
                "exchange_position": reconciliation.exchange_position,
                "internal_inventory": reconciliation.internal_inventory,
                "diff": reconciliation.diff,
                "mismatch": reconciliation.mismatch,
                "entry_px": reconciliation.exchange_entry_px,
                "unrealized_pnl": reconciliation.exchange_unrealized_pnl,
            },
        )

    def _maybe_log_mismatch_alert(self, observed_ms: int) -> None:
        reconciliation = self.state.position_reconciliation
        if not reconciliation.mismatch:
            return
        if self.config.unresolved_position_alert_interval_ms <= 0:
            return
        if (
            self._last_mismatch_alert_ms > 0
            and observed_ms - self._last_mismatch_alert_ms < self.config.unresolved_position_alert_interval_ms
        ):
            return
        self._last_mismatch_alert_ms = observed_ms
        self.logger.log_event(
            "position_mismatch_alert",
            reason="entries_halted",
            raw={
                "exchange_position": reconciliation.exchange_position,
                "internal_inventory": reconciliation.internal_inventory,
                "diff": reconciliation.diff,
            },
        )
