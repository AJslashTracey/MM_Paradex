from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from hyperliquid.utils.types import Cloid

from execution.executor import HyperliquidExecutor

from .config import MMConfig
from .inventory_manager import InventoryManager
from .models import ActiveOrder, FillRecord, QuoteIntent, QuotePlan
from .utils import generate_cloid, round_down, safe_json, side_to_is_buy, signed_edge_bps, to_float


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
        self.pending_markouts: dict[str, dict[str, Any]] = {}

    def total_open_notional(self) -> float:
        return sum(order.open_notional for order in self.active_orders.values())

    def refresh_deadman(self, observed_ms: int) -> None:
        if not self.config.live or self.executor is None:
            return
        if observed_ms - self.last_deadman_refresh_ms < self.config.deadman_refresh_ms:
            return
        raw = self.executor.schedule_cancel_all(ms_from_now=self.config.deadman_ms)
        self.last_deadman_refresh_ms = observed_ms
        self.logger.log_event("deadman_refresh", raw=raw)

    def cancel_all(self, reason: str, emergency: bool = False) -> None:
        if not self.active_orders and not (emergency and self.config.live and self.executor is not None):
            return
        if self.config.live and self.executor is not None:
            if emergency:
                raw = self.executor.cancel_all_for_coin(self.config.target_coin)
                self.active_orders.clear()
                self.logger.log_event("cancel_all", reason=reason, raw=raw)
                return
            for side in list(self.active_orders):
                self._cancel_one(side, reason)
            return
        for side in list(self.active_orders):
            order = self.active_orders.pop(side)
            self.logger.log_event("quote_cancel", reason=reason, raw={"side": side, "cloid": order.cloid_raw})

    def sync_quotes(self, plan: QuotePlan, observed_ms: int, force: bool, block_bid: bool, block_ask: bool) -> None:
        desired = {
            "bid": None if block_bid else plan.bid,
            "ask": None if block_ask else plan.ask,
        }
        for side in ("bid", "ask"):
            active = self.active_orders.get(side)
            target = desired[side]
            if active is not None and target is None:
                self._cancel_one(side, "side_blocked")
                continue
            if active is None and target is not None:
                self._place_one(target, observed_ms, reason="new_quote")
                continue
            if active is None or target is None:
                continue
            if self._needs_replace(active, target, observed_ms, force):
                if self._cancel_one(side, "requote"):
                    self._place_one(target, observed_ms, reason="requote")

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
        if force:
            return True
        if observed_ms - active.placed_time_ms < self.config.min_quote_lifetime_ms:
            return False
        price_bps = abs(target.px - active.price) / target.px * 10_000
        if price_bps >= self.config.requote_threshold_bps:
            return True
        active_remaining = active.remaining_size if active.remaining_size is not None else active.size
        if abs(active_remaining - target.size) > 1e-12:
            return True
        return False

    def _cancel_one(self, side: str, reason: str) -> bool:
        active = self.active_orders.get(side)
        if active is None:
            return True
        if self.config.live and self.executor is not None:
            try:
                raw = self.executor.cancel_order_by_cloid(coin=self.config.target_coin, cloid=Cloid.from_str(active.cloid_raw))
            except Exception as exc:
                self.logger.log_event("quote_cancel_error", reason=f"{reason}:{exc}", raw={"cloid": active.cloid_raw})
                return False
            self.logger.log_event("quote_cancel", reason=reason, raw=raw)
        else:
            self.logger.log_event("quote_cancel", reason=reason, raw={"side": side, "cloid": active.cloid_raw})
        self.active_orders.pop(side, None)
        return True

    def _place_one(self, target: QuoteIntent, observed_ms: int, reason: str) -> None:
        rounded_size = round_down(min(target.size, self.config.max_order_size), self.size_decimals)
        if rounded_size <= 0:
            self.logger.log_event("quote_skip", reason="size_rounds_to_zero", raw={"quote_side": target.quote_side})
            return
        projected_open_notional = self.total_open_notional() + (rounded_size * target.px)
        if projected_open_notional > self.config.max_open_notional:
            self.logger.log_event(
                "quote_skip",
                reason="max_open_notional",
                raw={"quote_side": target.quote_side, "projected_open_notional": projected_open_notional},
            )
            return
        cloid = generate_cloid()
        if self.config.live and self.executor is not None:
            raw = self.executor.place_limit_order(
                coin=self.config.target_coin,
                is_buy=target.is_buy,
                size=rounded_size,
                limit_px=target.px,
                tif="Alo",
                reduce_only=False,
                cloid=cloid,
            )
            parsed = parse_order_result(raw)
            if parsed.status != "resting":
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
        )


def parse_order_result(raw: Any) -> ParsedOrderResult:
    if not isinstance(raw, dict):
        return ParsedOrderResult(status="unknown", error="non_dict_response")
    if raw.get("status") == "error":
        return ParsedOrderResult(status="error", error=str(raw.get("error", "unknown_error")))
    data = raw.get("response", {}).get("data", {})
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
        return ParsedOrderResult(status="filled")
    if "error" in first:
        return ParsedOrderResult(status="error", error=str(first["error"]))
    return ParsedOrderResult(status="unknown", error=safe_json(first))
