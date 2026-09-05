from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from execution.executor import HyperliquidExecutor

from .config import MMConfig
from .inventory_manager import InventoryManager
from .models import ActiveOrder, FillRecord, QuoteIntent, QuotePlan, VenueState
from .utils import (
    cloid_from_str,
    generate_cloid,
    now_ms,
    round_down,
    round_price,
    safe_json,
    side_to_is_buy,
    signed_edge_bps,
    to_float,
)


@dataclass(frozen=True)
class ParsedOrderResult:
    status: str
    order_id: int | None = None
    error: str = ""


class OrderManager:
    def __init__(
        self,
        config: MMConfig,
        logger: Any,
        inventory: InventoryManager,
        executor: HyperliquidExecutor | None,
        size_decimals: int,
    ) -> None:
        self.config = config
        self.logger = logger
        self.inventory = inventory
        self.executor = executor
        self.size_decimals = size_decimals
        self.active_orders: dict[str, ActiveOrder] = {}
        self.seen_fill_keys: set[str] = set()
        self.last_deadman_refresh_ms = 0
        self.last_live_order_action_ms = 0
        self.live_order_blocked_until_ms = 0
        self.live_entries_halted_reason: str | None = None
        self.last_emergency_cancel_ms = 0
        self.last_flatten_attempt_ms = 0
        self.last_unresolved_position_alert_ms = 0
        self.cancel_all_in_flight = False
        self.flatten_in_flight = False
        self.pending_markouts: dict[str, dict[str, Any]] = {}

    def total_open_notional(self) -> float:
        return sum(order.open_notional for order in self.active_orders.values())

    def refresh_deadman(self, observed_ms: int) -> None:
        if not self.config.live or self.executor is None:
            return
        if self.config.deadman_ms <= 0:
            return
        if observed_ms - self.last_deadman_refresh_ms < self.config.deadman_refresh_ms:
            return
        raw = self.executor.schedule_cancel_all(ms_from_now=self.config.deadman_ms)
        self.last_deadman_refresh_ms = observed_ms
        self.logger.log_event("deadman_refresh", raw=raw)

    def cancel_all(self, reason: str, emergency: bool = False) -> None:
        if not self.active_orders and not (
            emergency
            and self.config.live
            and self.executor is not None
            and self.config.live_cancel_all_when_no_active_orders
        ):
            if emergency and self.config.live and self.executor is not None:
                self.logger.log_event("cancel_all_skip", reason="no_active_orders")
            return
        if self.config.live and self.executor is not None:
            if emergency:
                current_ms = now_ms()
                if self.cancel_all_in_flight:
                    self.logger.log_event("cancel_all_skip", reason="cancel_all_in_flight")
                    return
                if (
                    reason != "shutdown"
                    and current_ms - self.last_emergency_cancel_ms < self.config.live_cancel_all_min_interval_ms
                ):
                    self.logger.log_event("cancel_all_skip", reason="live_cancel_all_throttle")
                    return
                self.cancel_all_in_flight = True
                try:
                    self.last_emergency_cancel_ms = current_ms
                    self.last_live_order_action_ms = current_ms
                    raw = self.executor.cancel_all_for_coin(self.config.target_coin)
                    self.active_orders.clear()
                    self.logger.log_event("cancel_all", reason=reason, raw=raw)
                finally:
                    self.cancel_all_in_flight = False
                return
            for side in list(self.active_orders):
                self._cancel_one(side, reason, current_ms=now_ms())
            return
        for side in list(self.active_orders):
            order = self.active_orders.pop(side)
            self.logger.log_event("quote_cancel", reason=reason, raw={"side": side, "cloid": order.cloid_raw})

    def sync_quotes(self, plan: QuotePlan, observed_ms: int, force: bool, block_bid: bool, block_ask: bool) -> None:
        desired = {
            "bid": None if block_bid else plan.bid,
            "ask": None if block_ask else plan.ask,
        }
        if self.inventory.inventory > 0:
            side_order = ("ask", "bid")
        elif self.inventory.inventory < 0:
            side_order = ("bid", "ask")
        else:
            side_order = ("bid", "ask")
        for side in side_order:
            active = self.active_orders.get(side)
            target = desired[side]
            reduce_only = (side == "ask" and self.inventory.inventory > 0) or (
                side == "bid" and self.inventory.inventory < 0
            )
            if active is not None and target is None:
                self._cancel_one(side, "side_blocked", current_ms=observed_ms)
                continue
            if active is None and target is not None:
                self._place_one(target, observed_ms, reason="new_quote", reduce_only=reduce_only)
                continue
            if active is None or target is None:
                continue
            if self._needs_replace(active, target, observed_ms, force):
                if self._cancel_one(side, "requote", current_ms=observed_ms):
                    self._place_one(target, observed_ms, reason="requote", reduce_only=reduce_only)

    def maybe_paper_fill_cross(self, hl_bid: float | None, hl_ask: float | None, fair_value: Any, observed_ms: int) -> None:
        if not self.config.paper_fill_on_cross:
            return
        bid_order = self.active_orders.get("bid")
        if bid_order is not None and hl_ask is not None and hl_ask <= bid_order.price:
            self._register_paper_fill(bid_order, observed_ms, fair_value)
        ask_order = self.active_orders.get("ask")
        if ask_order is not None and hl_bid is not None and hl_bid >= ask_order.price:
            self._register_paper_fill(ask_order, observed_ms, fair_value)

    def _register_paper_fill(self, order: ActiveOrder, observed_ms: int, fair_value: Any) -> None:
        raw_fill = {
            "coin": self.config.target_coin,
            "px": order.price,
            "sz": order.remaining_size if order.remaining_size is not None else order.size,
            "side": "B" if order.is_buy else "A",
            "time": observed_ms,
            "startPosition": self.inventory.inventory,
            "dir": "Open Long" if order.is_buy else "Open Short",
            "closedPnl": "0",
            "hash": f"paper-{order.cloid_raw}",
            "oid": order.order_id or 0,
            "crossed": False,
            "fee": "0",
            "tid": observed_ms,
            "feeToken": "USDC",
        }
        self.active_orders.pop(order.quote_side, None)
        self.handle_fill(raw_fill, fair_value, observed_ms)

    def has_active_reducing_order(self, position_size: float) -> bool:
        if position_size > 0:
            order = self.active_orders.get("ask")
        elif position_size < 0:
            order = self.active_orders.get("bid")
        else:
            return False
        if order is None:
            return False
        remaining = order.remaining_size if order.remaining_size is not None else order.size
        return order.reduce_only and remaining >= abs(position_size) - self.config.position_reconcile_tolerance

    def cancel_non_reducing_orders(self, reason: str, observed_ms: int) -> None:
        for side, order in list(self.active_orders.items()):
            if order.reduce_only:
                continue
            self._cancel_one(side, reason, current_ms=observed_ms)

    def flatten_position_if_needed(self, position_size: float, hl_state: VenueState, observed_ms: int, reason: str) -> None:
        if abs(position_size) <= self.config.position_reconcile_tolerance:
            return
        if self.has_active_reducing_order(position_size):
            self._log_flatten_skip("reducing_limit_active", observed_ms, position_size)
            return
        if self.flatten_in_flight:
            self._log_flatten_skip("flatten_in_flight", observed_ms, position_size)
            return
        if (
            self.last_flatten_attempt_ms > 0
            and observed_ms - self.last_flatten_attempt_ms < self.config.flatten_cooldown_ms
        ):
            remaining_ms = self.config.flatten_cooldown_ms - (observed_ms - self.last_flatten_attempt_ms)
            self._log_flatten_skip(f"flatten_cooldown:{remaining_ms}ms", observed_ms, position_size)
            return
        if not hl_state.connected or not hl_state.is_fresh(observed_ms, self.config.max_data_age_ms):
            stale_reason = "hyperliquid_stale" if hl_state.connected else "hyperliquid_disconnected"
            self._alert_unresolved_position(stale_reason, observed_ms, position_size)
            return
        if hl_state.book is None:
            self._alert_unresolved_position("missing_hyperliquid_book", observed_ms, position_size)
            return

        is_buy = position_size < 0
        level = hl_state.book.best_ask() if is_buy else hl_state.book.best_bid()
        if level is None:
            self._alert_unresolved_position("missing_exit_level", observed_ms, position_size)
            return

        size = round_down(abs(position_size), self.size_decimals)
        if size <= 0:
            self._alert_unresolved_position("position_size_rounds_to_zero", observed_ms, position_size)
            return

        decimals = hl_state.book.price_decimals()
        price_multiplier = 1.0 + self.config.exit_ioc_price_protection_bps / 10_000 if is_buy else 1.0 - self.config.exit_ioc_price_protection_bps / 10_000
        limit_px = round_price(level.px * price_multiplier, decimals, "ask" if is_buy else "bid")
        cloid = generate_cloid()

        if not self.config.live or self.executor is None:
            self.last_flatten_attempt_ms = observed_ms
            self.logger.log_event(
                "flatten_submit",
                reason=f"dry_run:{reason}",
                raw={"is_buy": is_buy, "size": size, "limit_px": limit_px, "position_size": position_size},
            )
            return

        self.flatten_in_flight = True
        self.last_flatten_attempt_ms = observed_ms
        self.last_live_order_action_ms = observed_ms
        try:
            raw = self.executor.exit_reduce_only_ioc(
                coin=self.config.target_coin,
                is_buy=is_buy,
                size=size,
                limit_px=limit_px,
                cloid=cloid,
            )
        except Exception as exc:
            self.logger.log_event(
                "flatten_error",
                reason=str(exc),
                raw={"is_buy": is_buy, "size": size, "limit_px": limit_px, "position_size": position_size},
            )
            return
        finally:
            self.flatten_in_flight = False

        parsed = parse_order_result(raw)
        if parsed.status == "error":
            self._record_request_limit(parsed.error, observed_ms, source="flatten")
            self.logger.log_event("flatten_reject", reason=parsed.error, raw=raw)
            return
        if parsed.status == "resting":
            quote_side = "bid" if is_buy else "ask"
            self.active_orders[quote_side] = ActiveOrder(
                quote_side=quote_side,
                is_buy=is_buy,
                cloid_raw=str(cloid),
                price=limit_px,
                size=size,
                placed_time_ms=observed_ms,
                order_id=parsed.order_id,
                remaining_size=size,
                status="resting",
                reduce_only=True,
            )
        self.logger.log_event(
            "flatten_submit",
            reason=f"{reason}:{parsed.status}",
            raw={"is_buy": is_buy, "size": size, "limit_px": limit_px, "position_size": position_size, "result": raw},
        )

    def ingest_open_orders(self, orders: list[dict[str, Any]], observed_ms: int, reason: str) -> None:
        for order in orders:
            self._upsert_exchange_order(order, observed_ms)
        self.logger.log_event("open_orders_reconcile", reason=reason, raw={"orders": orders})

    def handle_order_update(self, payload: dict[str, Any], observed_ms: int) -> None:
        self.logger.log_event("order_update", raw=payload)
        order = payload.get("order") if isinstance(payload, dict) else None
        status = str(payload.get("status", "")) if isinstance(payload, dict) else ""
        if not isinstance(order, dict) or order.get("coin") != self.config.target_coin:
            return
        active = self._upsert_exchange_order(order, observed_ms)
        if active is not None:
            active.status = status or active.status
        if status in {"filled", "canceled", "rejected", "marginCanceled"}:
            self._remove_matching_order(order)

    def handle_fill(self, fill: dict[str, Any], fair_value: Any, observed_ms: int) -> FillRecord | None:
        fill_key = str(fill.get("tid") or fill.get("hash") or fill.get("oid") or safe_json(fill))
        if fill_key in self.seen_fill_keys:
            return None
        self.seen_fill_keys.add(fill_key)
        is_buy = side_to_is_buy(str(fill.get("side", "")))
        px = to_float(fill.get("px"))
        sz = to_float(fill.get("sz"))
        if is_buy is None or px is None or sz is None or sz <= 0:
            return None
        fee = to_float(fill.get("fee")) or 0.0
        realized_before = self.inventory.realized_pnl
        fill_time_ms = int(fill.get("time") or observed_ms)
        inventory_before, inventory_after = self.inventory.apply_fill(is_buy, sz, px, fee, fill_time_ms)
        order_id = int(fill["oid"]) if fill.get("oid") is not None else None
        if order_id is not None:
            for side, active in list(self.active_orders.items()):
                if active.order_id != order_id:
                    continue
                remaining = (active.remaining_size if active.remaining_size is not None else active.size) - sz
                if remaining <= 1e-12:
                    self.active_orders.pop(side, None)
                else:
                    active.remaining_size = remaining
                break
        after_move = False
        rapid_move_side = ""
        if fair_value is not None and fair_value.recent_rapid_move_time_ms is not None:
            after_move = observed_ms - fair_value.recent_rapid_move_time_ms <= self.config.recent_move_lookback_ms
            rapid_move_side = fair_value.rapid_move_side or ""
        fill_record = FillRecord(
            fill_key=fill_key,
            time_ms=fill_time_ms,
            side="buy" if is_buy else "sell",
            size=sz,
            price=px,
            inventory_before=inventory_before,
            inventory_after=inventory_after,
            realized_pnl_delta=self.inventory.realized_pnl - realized_before,
            total_realized_pnl=self.inventory.realized_pnl,
            fee=fee,
            fee_token=str(fill.get("feeToken", "")),
            order_id=order_id,
            trade_id=int(fill["tid"]) if fill.get("tid") is not None else None,
            tx_hash=str(fill.get("hash", "")),
            crossed=bool(fill.get("crossed", False)),
            dir_label=str(fill.get("dir", "")),
            after_binance_move=after_move,
            rapid_move_side=rapid_move_side,
            fair_px=None if fair_value is None else fair_value.fair_px,
            io_mid_px=None if fair_value is None else fair_value.baseline_io_mid_px,
            basis_ema=None if fair_value is None else fair_value.basis_ema,
            binance_ret_1s_bps=None if fair_value is None else fair_value.binance_ret_1s_bps,
            binance_ret_5s_bps=None if fair_value is None else fair_value.binance_ret_5s_bps,
            binance_ret_10s_bps=None if fair_value is None else fair_value.binance_ret_10s_bps,
            edge_vs_io_mid_bps=signed_edge_bps(px, None if fair_value is None else fair_value.baseline_io_mid_px, is_buy),
            edge_vs_fair_bps=signed_edge_bps(px, None if fair_value is None else fair_value.fair_px, is_buy),
            raw=fill,
        )
        self.pending_markouts[fill_key] = {
            "record": fill_record,
            "markouts": {},
        }
        self.logger.log_event("fill", raw=fill)
        self.logger.log_fill("fill_initial", fill_record, {}, False)
        return fill_record

    def resolve_markouts(self, observed_ms: int, current_mid: float | None) -> None:
        if current_mid is None:
            return
        completed: list[str] = []
        for fill_key, pending in self.pending_markouts.items():
            fill_record: FillRecord = pending["record"]
            markouts: dict[int, float | None] = pending["markouts"]
            for window_s in self.config.markout_windows_s:
                if window_s in markouts:
                    continue
                if observed_ms - fill_record.time_ms < window_s * 1000:
                    continue
                sign = 1.0 if fill_record.side == "buy" else -1.0
                markouts[window_s] = sign * (current_mid - fill_record.price) / fill_record.price * 10_000
            if len(markouts) == len(self.config.markout_windows_s):
                self.logger.log_fill("fill_markout_final", fill_record, markouts, True)
                completed.append(fill_key)
        for fill_key in completed:
            self.pending_markouts.pop(fill_key, None)

    def flush_pending_markouts(self) -> None:
        for fill_key, pending in list(self.pending_markouts.items()):
            self.logger.log_fill("fill_markout_partial", pending["record"], pending["markouts"], False)
            self.pending_markouts.pop(fill_key, None)

    def _needs_replace(self, active: ActiveOrder, target: QuoteIntent, observed_ms: int, force: bool) -> bool:
        if observed_ms - active.placed_time_ms < self.config.min_quote_lifetime_ms:
            return False
        if force:
            return True
        price_bps = abs(target.px - active.price) / target.px * 10_000
        if price_bps >= self.config.requote_threshold_bps:
            return True
        active_remaining = active.remaining_size if active.remaining_size is not None else active.size
        if abs(active_remaining - target.size) > 1e-12:
            return True
        return False

    def _cancel_one(self, side: str, reason: str, current_ms: int | None = None) -> bool:
        active = self.active_orders.get(side)
        if active is None:
            return True
        if self.config.live and self.executor is not None:
            current_ms = now_ms() if current_ms is None else current_ms
            throttle_reason = self._live_order_throttle_reason(current_ms, allow_reduce_only=active.reduce_only)
            if throttle_reason is not None:
                self.logger.log_event("quote_cancel_skip", reason=throttle_reason, raw={"cloid": active.cloid_raw})
                return False
            self.last_live_order_action_ms = current_ms
            try:
                if active.cloid_raw:
                    raw = self.executor.cancel_order_by_cloid(
                        coin=self.config.target_coin,
                        cloid=cloid_from_str(active.cloid_raw),
                    )
                elif active.order_id is not None:
                    raw = self.executor.cancel_order(coin=self.config.target_coin, oid=active.order_id)
                else:
                    raise RuntimeError("active order has neither cloid nor oid")
            except Exception as exc:
                self.logger.log_event("quote_cancel_error", reason=f"{reason}:{exc}", raw={"cloid": active.cloid_raw})
                return False
            self.logger.log_event("quote_cancel", reason=reason, raw=raw)
        else:
            self.logger.log_event("quote_cancel", reason=reason, raw={"side": side, "cloid": active.cloid_raw})
        self.active_orders.pop(side, None)
        return True

    def _place_one(self, target: QuoteIntent, observed_ms: int, reason: str, reduce_only: bool = False) -> None:
        rounded_size = round_down(min(target.size, self.config.max_order_size), self.size_decimals)
        if rounded_size <= 0:
            self.logger.log_event("quote_skip", reason="size_rounds_to_zero", raw={"quote_side": target.quote_side})
            return
        projected_open_notional = self.total_open_notional() + (rounded_size * target.px)
        if not reduce_only and projected_open_notional > self.config.max_open_notional:
            self.logger.log_event(
                "quote_skip",
                reason="max_open_notional",
                raw={"quote_side": target.quote_side, "projected_open_notional": projected_open_notional},
            )
            return
        cloid = generate_cloid()
        if self.config.live and self.executor is not None:
            throttle_reason = self._live_order_throttle_reason(observed_ms, allow_reduce_only=reduce_only)
            if throttle_reason is not None:
                self.logger.log_event("quote_skip", reason=throttle_reason, raw={"quote_side": target.quote_side})
                return
            self.last_live_order_action_ms = observed_ms
            raw = self.executor.place_limit_order(
                coin=self.config.target_coin,
                is_buy=target.is_buy,
                size=rounded_size,
                limit_px=target.px,
                tif="Alo",
                reduce_only=reduce_only,
                cloid=cloid,
            )
            parsed = parse_order_result(raw)
            if parsed.status != "resting":
                self._record_request_limit(parsed.error, observed_ms, source="entry")
                self.logger.log_event("quote_reject", reason=parsed.error or parsed.status, raw=raw)
                return
            order_id = parsed.order_id
            self.logger.log_event("quote_place", reason=reason, raw=raw)
        else:
            raw = {"paper": True, "cloid": str(cloid), "price": target.px, "size": target.size}
            order_id = None
            self.logger.log_event("quote_place", reason=reason, raw=raw)
        self.active_orders[target.quote_side] = ActiveOrder(
            quote_side=target.quote_side,
            is_buy=target.is_buy,
            cloid_raw=str(cloid),
            price=target.px,
            size=rounded_size,
            placed_time_ms=observed_ms,
            order_id=order_id,
            remaining_size=rounded_size,
            reduce_only=reduce_only,
        )

    def _live_order_throttle_reason(self, observed_ms: int, allow_reduce_only: bool = False) -> str | None:
        if not self.config.live:
            return None
        if not allow_reduce_only and self.live_entries_halted_reason is not None:
            return f"live_entry_halt:{self.live_entries_halted_reason}"
        if not allow_reduce_only and observed_ms < self.live_order_blocked_until_ms:
            remaining_ms = self.live_order_blocked_until_ms - observed_ms
            return f"live_reject_cooldown:{remaining_ms}ms"
        elapsed_ms = observed_ms - self.last_live_order_action_ms
        if not allow_reduce_only and elapsed_ms < self.config.live_order_action_min_interval_ms:
            return f"live_order_action_throttle:{self.config.live_order_action_min_interval_ms - elapsed_ms}ms"
        return None

    def _record_request_limit(self, error: str, observed_ms: int, source: str) -> None:
        if "Too many cumulative requests" not in error:
            return
        self.live_order_blocked_until_ms = max(
            self.live_order_blocked_until_ms,
            observed_ms + self.config.live_reject_cooldown_ms,
        )
        if self.config.halt_entries_on_request_limit:
            self.live_entries_halted_reason = f"request_limit:{source}"
            if self.logger is not None:
                self.logger.log_event("live_entry_halt", reason=self.live_entries_halted_reason, raw={"error": error})

    def _alert_unresolved_position(self, reason: str, observed_ms: int, position_size: float) -> None:
        if self.config.unresolved_position_alert_interval_ms <= 0:
            return
        if (
            self.last_unresolved_position_alert_ms > 0
            and observed_ms - self.last_unresolved_position_alert_ms < self.config.unresolved_position_alert_interval_ms
        ):
            return
        self.last_unresolved_position_alert_ms = observed_ms
        self.logger.log_event("unresolved_position_alert", reason=reason, raw={"position_size": position_size})

    def _log_flatten_skip(self, reason: str, observed_ms: int, position_size: float) -> None:
        self.logger.log_event("flatten_skip", reason=reason, raw={"position_size": position_size})

    def _upsert_exchange_order(self, order: dict[str, Any], observed_ms: int) -> ActiveOrder | None:
        quote_side = _quote_side_from_exchange_order(order)
        price = to_float(order.get("limitPx") or order.get("px"))
        size = to_float(order.get("sz") or order.get("origSz"))
        if quote_side is None or price is None or size is None:
            return None
        order_id = int(order["oid"]) if order.get("oid") is not None else None
        cloid_raw = str(order.get("cloid") or "")
        timestamp = int(order.get("timestamp") or observed_ms)
        reduce_only = _truthy(order.get("reduceOnly") if "reduceOnly" in order else order.get("reduce_only"))
        active = self._find_matching_order(order)
        if active is None:
            active = ActiveOrder(
                quote_side=quote_side,
                is_buy=quote_side == "bid",
                cloid_raw=cloid_raw,
                price=price,
                size=size,
                placed_time_ms=timestamp,
                order_id=order_id,
                remaining_size=size,
                reduce_only=reduce_only,
            )
            self.active_orders[quote_side] = active
            return active
        active.price = price
        active.size = max(active.size, size)
        active.remaining_size = size
        active.order_id = order_id if order_id is not None else active.order_id
        active.cloid_raw = cloid_raw or active.cloid_raw
        active.reduce_only = reduce_only
        return active

    def _find_matching_order(self, order: dict[str, Any]) -> ActiveOrder | None:
        oid = order.get("oid")
        cloid = order.get("cloid")
        for active in self.active_orders.values():
            if oid is not None and active.order_id == int(oid):
                return active
            if cloid and active.cloid_raw == str(cloid):
                return active
        quote_side = _quote_side_from_exchange_order(order)
        if quote_side is None:
            return None
        return self.active_orders.get(quote_side)

    def _remove_matching_order(self, order: dict[str, Any]) -> None:
        active = self._find_matching_order(order)
        if active is not None:
            self.active_orders.pop(active.quote_side, None)


def _quote_side_from_exchange_order(order: dict[str, Any]) -> str | None:
    is_buy = side_to_is_buy(str(order.get("side", "")))
    if is_buy is None:
        return None
    return "bid" if is_buy else "ask"


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y"}
    return bool(value)


def parse_order_result(raw: Any) -> ParsedOrderResult:
    if not isinstance(raw, dict):
        return ParsedOrderResult(status="unknown", error="non_dict_response")
    if raw.get("status") in {"error", "err"}:
        return ParsedOrderResult(status="error", error=str(raw.get("error") or raw.get("response") or "unknown_error"))
    response = raw.get("response", {})
    if not isinstance(response, dict):
        return ParsedOrderResult(status="unknown", error=f"non_dict_response_field:{response}")
    data = response.get("data", {})
    if not isinstance(data, dict):
        return ParsedOrderResult(status="unknown", error=f"non_dict_data_field:{data}")
    statuses = data.get("statuses", [])
    if not statuses:
        if "error" in raw:
            return ParsedOrderResult(status="error", error=str(raw["error"]))
        return ParsedOrderResult(status="unknown", error="missing_statuses")
    first = statuses[0]
    if "resting" in first and isinstance(first["resting"], dict):
        oid = first["resting"].get("oid")
        return ParsedOrderResult(status="resting", order_id=None if oid is None else int(oid))
    if "filled" in first:
        filled = first["filled"]
        oid = filled.get("oid") if isinstance(filled, dict) else None
        return ParsedOrderResult(status="filled", order_id=None if oid is None else int(oid))
    if "error" in first:
        return ParsedOrderResult(status="error", error=str(first["error"]))
    return ParsedOrderResult(status="unknown", error=safe_json(first))
