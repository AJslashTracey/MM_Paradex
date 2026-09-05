from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

from .config import MMConfig
from .inventory_manager import InventoryManager
from .models import ActiveOrder, BotState, FillRecord, InventorySnapshot
from .utils import now_ms, safe_json, utc_iso


EVENT_FIELDS = [
    "time_utc",
    "time_ms",
    "mode",
    "strategy_mode",
    "event",
    "reason",
    "target_coin",
    "binance_symbol",
    "hl_connected",
    "binance_connected",
    "hl_bid",
    "hl_ask",
    "hl_mid",
    "hl_spread_bps",
    "hl_book_time_ms",
    "hl_recv_time_ms",
    "binance_bid",
    "binance_ask",
    "binance_mid",
    "binance_book_time_ms",
    "binance_recv_time_ms",
    "basis_raw",
    "basis_ema",
    "fair_px",
    "binance_ret_1s_bps",
    "binance_ret_5s_bps",
    "binance_ret_10s_bps",
    "io_deviation_bps",
    "rapid_move_side",
    "rapid_move_bps",
    "inventory",
    "avg_entry_px",
    "realized_pnl",
    "unrealized_pnl",
    "total_fees",
    "exchange_position",
    "position_diff",
    "position_mismatch",
    "position_last_reconcile_ms",
    "live_entry_halt_reason",
    "hl_book_age_ms",
    "binance_book_age_ms",
    "cross_recv_skew_ms",
    "active_bid_px",
    "active_bid_sz",
    "active_bid_oid",
    "active_bid_cloid",
    "active_bid_reduce_only",
    "active_ask_px",
    "active_ask_sz",
    "active_ask_oid",
    "active_ask_cloid",
    "active_ask_reduce_only",
    "desired_bid_px",
    "desired_bid_sz",
    "desired_ask_px",
    "desired_ask_sz",
    "block_bid",
    "block_ask",
    "quoting_allowed",
    "raw_json",
]

MARKET_FIELDS = EVENT_FIELDS

FILL_FIELDS = [
    "record_type",
    "time_utc",
    "time_ms",
    "mode",
    "strategy_mode",
    "target_coin",
    "binance_symbol",
    "fill_key",
    "fill_time_utc",
    "fill_time_ms",
    "side",
    "size",
    "price",
    "inventory_before",
    "inventory_after",
    "realized_pnl_delta",
    "total_realized_pnl",
    "fee",
    "fee_token",
    "order_id",
    "trade_id",
    "hash",
    "crossed",
    "dir_label",
    "after_binance_move",
    "rapid_move_side",
    "fair_px",
    "io_mid_px",
    "basis_ema",
    "binance_ret_1s_bps",
    "binance_ret_5s_bps",
    "binance_ret_10s_bps",
    "edge_vs_io_mid_bps",
    "edge_vs_fair_bps",
    "markout_1s_bps",
    "markout_5s_bps",
    "markout_10s_bps",
    "markout_30s_bps",
    "markout_60s_bps",
    "markouts_complete",
    "raw_json",
]


class MMLogger:
    def __init__(self, config: MMConfig, state: BotState, inventory: InventoryManager, order_manager: Any) -> None:
        self.config = config
        self.state = state
        self.inventory = inventory
        self.order_manager = order_manager
        self._last_console_status_ms = 0
        self._last_console_skip_by_key: dict[str, int] = {}
        config.out_dir.mkdir(parents=True, exist_ok=True)
        self._event_writer, self._event_file = self._open_csv(config.event_log, EVENT_FIELDS)
        self._market_writer, self._market_file = self._open_csv(config.market_log, MARKET_FIELDS)
        self._fill_writer, self._fill_file = self._open_csv(config.fill_log, FILL_FIELDS)

    def close(self) -> None:
        self._event_file.close()
        self._market_file.close()
        self._fill_file.close()

    def _open_csv(self, path: Path, fields: list[str]) -> tuple[csv.DictWriter, Any]:
        needs_header = not path.exists() or path.stat().st_size == 0
        handle = path.open("a", newline="", encoding="utf-8")
        writer = csv.DictWriter(handle, fieldnames=fields)
        if needs_header:
            writer.writeheader()
            handle.flush()
        return writer, handle

    def _common_row(self, observed_ms: int) -> dict[str, Any]:
        inventory_snapshot = self.inventory.snapshot(observed_ms, self.state.hl.mid())
        bid_order = self.order_manager.active_orders.get("bid")
        ask_order = self.order_manager.active_orders.get("ask")
        hl_book = self.state.hl.book
        binance_book = self.state.binance.book
        plan = self.state.quote_plan
        risk = self.state.risk
        reconciliation = self.state.position_reconciliation
        hl_age_ms = self.state.hl.book_age_ms(observed_ms)
        binance_age_ms = self.state.binance.book_age_ms(observed_ms)
        cross_recv_skew_ms = (
            None
            if hl_book is None
            or binance_book is None
            or hl_book.recv_time_ms is None
            or binance_book.recv_time_ms is None
            else abs(hl_book.recv_time_ms - binance_book.recv_time_ms)
        )
        return {
            "time_utc": utc_iso(observed_ms),
            "time_ms": observed_ms,
            "mode": "live" if self.config.live else "dry_run",
            "strategy_mode": self.config.strategy_mode,
            "target_coin": self.config.target_coin,
            "binance_symbol": self.config.binance_symbol,
            "hl_connected": str(self.state.hl.connected).lower(),
            "binance_connected": str(self.state.binance.connected).lower(),
            "hl_bid": "" if self.state.hl.best_bid() is None else self.state.hl.best_bid().px,
            "hl_ask": "" if self.state.hl.best_ask() is None else self.state.hl.best_ask().px,
            "hl_mid": "" if self.state.hl.mid() is None else self.state.hl.mid(),
            "hl_spread_bps": "" if self.state.hl.spread_bps() is None else f"{self.state.hl.spread_bps():.6f}",
            "hl_book_time_ms": "" if hl_book is None else hl_book.exchange_time_ms or "",
            "hl_recv_time_ms": "" if hl_book is None else hl_book.recv_time_ms or "",
            "binance_bid": "" if self.state.binance.best_bid() is None else self.state.binance.best_bid().px,
            "binance_ask": "" if self.state.binance.best_ask() is None else self.state.binance.best_ask().px,
            "binance_mid": "" if self.state.binance.mid() is None else self.state.binance.mid(),
            "binance_book_time_ms": "" if binance_book is None else binance_book.exchange_time_ms or "",
            "binance_recv_time_ms": "" if binance_book is None else binance_book.recv_time_ms or "",
            "basis_raw": "" if self.state.fair_value is None or self.state.fair_value.basis_raw is None else f"{self.state.fair_value.basis_raw:.10f}",
            "basis_ema": "" if self.state.fair_value is None or self.state.fair_value.basis_ema is None else f"{self.state.fair_value.basis_ema:.10f}",
            "fair_px": "" if self.state.fair_value is None or self.state.fair_value.fair_px is None else f"{self.state.fair_value.fair_px:.10f}",
            "binance_ret_1s_bps": "" if self.state.fair_value is None or self.state.fair_value.binance_ret_1s_bps is None else f"{self.state.fair_value.binance_ret_1s_bps:.6f}",
            "binance_ret_5s_bps": "" if self.state.fair_value is None or self.state.fair_value.binance_ret_5s_bps is None else f"{self.state.fair_value.binance_ret_5s_bps:.6f}",
            "binance_ret_10s_bps": "" if self.state.fair_value is None or self.state.fair_value.binance_ret_10s_bps is None else f"{self.state.fair_value.binance_ret_10s_bps:.6f}",
            "io_deviation_bps": "" if self.state.fair_value is None or self.state.fair_value.io_deviation_bps is None else f"{self.state.fair_value.io_deviation_bps:.6f}",
            "rapid_move_side": "" if self.state.fair_value is None or self.state.fair_value.rapid_move_side is None else self.state.fair_value.rapid_move_side,
            "rapid_move_bps": "" if self.state.fair_value is None or self.state.fair_value.rapid_move_bps is None else f"{self.state.fair_value.rapid_move_bps:.6f}",
            "inventory": f"{inventory_snapshot.inventory:.10f}",
            "avg_entry_px": "" if inventory_snapshot.avg_entry_px is None else f"{inventory_snapshot.avg_entry_px:.10f}",
            "realized_pnl": f"{inventory_snapshot.realized_pnl:.10f}",
            "unrealized_pnl": "" if inventory_snapshot.unrealized_pnl is None else f"{inventory_snapshot.unrealized_pnl:.10f}",
            "total_fees": f"{inventory_snapshot.total_fees:.10f}",
            "exchange_position": "" if reconciliation.exchange_position is None else f"{reconciliation.exchange_position:.10f}",
            "position_diff": "" if reconciliation.diff is None else f"{reconciliation.diff:.10f}",
            "position_mismatch": str(reconciliation.mismatch).lower(),
            "position_last_reconcile_ms": "" if reconciliation.last_reconcile_ms is None else reconciliation.last_reconcile_ms,
            "live_entry_halt_reason": "" if self.order_manager.live_entries_halted_reason is None else self.order_manager.live_entries_halted_reason,
            "hl_book_age_ms": "" if hl_age_ms is None else hl_age_ms,
            "binance_book_age_ms": "" if binance_age_ms is None else binance_age_ms,
            "cross_recv_skew_ms": "" if cross_recv_skew_ms is None else cross_recv_skew_ms,
            "active_bid_px": "" if bid_order is None else bid_order.price,
            "active_bid_sz": "" if bid_order is None else bid_order.remaining_size if bid_order.remaining_size is not None else bid_order.size,
            "active_bid_oid": "" if bid_order is None or bid_order.order_id is None else bid_order.order_id,
            "active_bid_cloid": "" if bid_order is None else bid_order.cloid_raw,
            "active_bid_reduce_only": "" if bid_order is None else str(bid_order.reduce_only).lower(),
            "active_ask_px": "" if ask_order is None else ask_order.price,
            "active_ask_sz": "" if ask_order is None else ask_order.remaining_size if ask_order.remaining_size is not None else ask_order.size,
            "active_ask_oid": "" if ask_order is None or ask_order.order_id is None else ask_order.order_id,
            "active_ask_cloid": "" if ask_order is None else ask_order.cloid_raw,
            "active_ask_reduce_only": "" if ask_order is None else str(ask_order.reduce_only).lower(),
            "desired_bid_px": "" if plan is None or plan.bid is None else plan.bid.px,
            "desired_bid_sz": "" if plan is None or plan.bid is None else plan.bid.size,
            "desired_ask_px": "" if plan is None or plan.ask is None else plan.ask.px,
            "desired_ask_sz": "" if plan is None or plan.ask is None else plan.ask.size,
            "block_bid": "" if risk is None else str(risk.block_bid).lower(),
            "block_ask": "" if risk is None else str(risk.block_ask).lower(),
            "quoting_allowed": "" if risk is None else str(risk.quoting_allowed).lower(),
        }

    def log_event(self, event: str, reason: str | None = None, raw: Any | None = None) -> None:
        observed_ms = max(
            self.state.hl.last_message_ms or 0,
            self.state.binance.last_message_ms or 0,
            0 if self.state.fair_value is None else self.state.fair_value.time_ms,
            now_ms(),
        )
        row = self._common_row(observed_ms)
        row["event"] = event
        row["reason"] = reason or ""
        row["raw_json"] = "" if raw is None else safe_json(raw)
        self._event_writer.writerow(row)
        self._event_file.flush()
        self._maybe_print_event(event, reason, raw, observed_ms)

    def log_market_snapshot(self, observed_ms: int) -> None:
        row = self._common_row(observed_ms)
        row["event"] = "snapshot"
        row["reason"] = ""
        row["raw_json"] = ""
        self._market_writer.writerow(row)
        self._market_file.flush()
        self._maybe_print_status(observed_ms)

    def log_fill(self, record_type: str, fill: FillRecord, markouts: dict[int, float | None], markouts_complete: bool) -> None:
        row = {
            "record_type": record_type,
            "time_utc": utc_iso(),
            "time_ms": self.inventory._last_update_ms or fill.time_ms,
            "mode": "live" if self.config.live else "dry_run",
            "strategy_mode": self.config.strategy_mode,
            "target_coin": self.config.target_coin,
            "binance_symbol": self.config.binance_symbol,
            "fill_key": fill.fill_key,
            "fill_time_utc": utc_iso(fill.time_ms),
            "fill_time_ms": fill.time_ms,
            "side": fill.side,
            "size": fill.size,
            "price": fill.price,
            "inventory_before": fill.inventory_before,
            "inventory_after": fill.inventory_after,
            "realized_pnl_delta": fill.realized_pnl_delta,
            "total_realized_pnl": fill.total_realized_pnl,
            "fee": fill.fee,
            "fee_token": fill.fee_token,
            "order_id": "" if fill.order_id is None else fill.order_id,
            "trade_id": "" if fill.trade_id is None else fill.trade_id,
            "hash": fill.tx_hash,
            "crossed": str(fill.crossed).lower(),
            "dir_label": fill.dir_label,
            "after_binance_move": str(fill.after_binance_move).lower(),
            "rapid_move_side": fill.rapid_move_side,
            "fair_px": "" if fill.fair_px is None else fill.fair_px,
            "io_mid_px": "" if fill.io_mid_px is None else fill.io_mid_px,
            "basis_ema": "" if fill.basis_ema is None else fill.basis_ema,
            "binance_ret_1s_bps": "" if fill.binance_ret_1s_bps is None else fill.binance_ret_1s_bps,
            "binance_ret_5s_bps": "" if fill.binance_ret_5s_bps is None else fill.binance_ret_5s_bps,
            "binance_ret_10s_bps": "" if fill.binance_ret_10s_bps is None else fill.binance_ret_10s_bps,
            "edge_vs_io_mid_bps": "" if fill.edge_vs_io_mid_bps is None else fill.edge_vs_io_mid_bps,
            "edge_vs_fair_bps": "" if fill.edge_vs_fair_bps is None else fill.edge_vs_fair_bps,
            "markout_1s_bps": "" if markouts.get(1) is None else markouts.get(1),
            "markout_5s_bps": "" if markouts.get(5) is None else markouts.get(5),
            "markout_10s_bps": "" if markouts.get(10) is None else markouts.get(10),
            "markout_30s_bps": "" if markouts.get(30) is None else markouts.get(30),
            "markout_60s_bps": "" if markouts.get(60) is None else markouts.get(60),
            "markouts_complete": str(markouts_complete).lower(),
            "raw_json": safe_json(fill.raw),
        }
        self._fill_writer.writerow(row)
        self._fill_file.flush()
        print(
            f"[{utc_iso()}] fill {fill.side} sz={_fmt_float(fill.size, 4)} px={_fmt_float(fill.price, 4)} "
            f"inv={_fmt_float(fill.inventory_after, 4)} realized={_fmt_float(fill.total_realized_pnl, 4)}",
            flush=True,
        )

    def _maybe_print_status(self, observed_ms: int) -> None:
        interval_ms = self.config.console_status_interval_ms
        if interval_ms <= 0:
            return
        if observed_ms - self._last_console_status_ms < interval_ms:
            return
        self._last_console_status_ms = observed_ms
        fair = self.state.fair_value
        risk = self.state.risk
        bid_order = self.order_manager.active_orders.get("bid")
        ask_order = self.order_manager.active_orders.get("ask")
        hl_age = self.state.hl.book_age_ms(observed_ms)
        binance_age = self.state.binance.book_age_ms(observed_ms)
        reconciliation = self.state.position_reconciliation
        cross_recv_skew_ms = None
        if (
            self.state.hl.book is not None
            and self.state.binance.book is not None
            and self.state.hl.book.recv_time_ms is not None
            and self.state.binance.book.recv_time_ms is not None
        ):
            cross_recv_skew_ms = abs(self.state.hl.book.recv_time_ms - self.state.binance.book.recv_time_ms)
        status = "quoting" if risk is not None and risk.quoting_allowed else f"paused:{'' if risk is None else risk.reason}"
        if self.order_manager.live_entries_halted_reason is not None:
            status = f"entry_halted:{self.order_manager.live_entries_halted_reason}"
        elif reconciliation.mismatch:
            status = "entry_halted:position_mismatch"
        elif reconciliation.exchange_position is not None and abs(reconciliation.exchange_position) > self.config.position_reconcile_tolerance:
            status = "flattening:exchange_position"
        print(
            f"[{utc_iso(observed_ms)}] status {status} "
            f"io={_fmt_float(self.state.hl.mid(), 4)} fair={_fmt_float(None if fair is None else fair.fair_px, 4)} "
            f"dev={_fmt_float(None if fair is None else fair.io_deviation_bps, 2)}bps "
            f"bin5s={_fmt_float(None if fair is None else fair.binance_ret_5s_bps, 2)}bps "
            f"inv={_fmt_float(self.inventory.inventory, 4)} exch_pos={_fmt_float(reconciliation.exchange_position, 4)} "
            f"diff={_fmt_float(reconciliation.diff, 4)} "
            f"active_bid={_fmt_order(bid_order)} active_ask={_fmt_order(ask_order)} "
            f"age_hl={_fmt_ms(hl_age)} age_bin={_fmt_ms(binance_age)} skew={_fmt_ms(cross_recv_skew_ms)}",
            flush=True,
        )

    def _maybe_print_event(self, event: str, reason: str | None, raw: Any | None, observed_ms: int) -> None:
        if event in {"binance_book", "binance_depth", "hl_book", "hl_ctx", "hl_trade"}:
            return
        reason = reason or ""
        if event in {"quote_skip", "quote_cancel_skip", "cancel_all_skip", "flatten_skip"}:
            key = f"{event}:{reason}:{_raw_quote_side(raw)}"
            last_ms = self._last_console_skip_by_key.get(key, 0)
            if observed_ms - last_ms < 10_000:
                return
            self._last_console_skip_by_key[key] = observed_ms
        if event == "quote_place":
            print(f"[{utc_iso(observed_ms)}] quote placed {reason or ''} {_raw_order_summary(raw)}", flush=True)
        elif event == "quote_cancel":
            print(f"[{utc_iso(observed_ms)}] quote canceled reason={reason} {_raw_order_summary(raw)}", flush=True)
        elif event == "quote_reject":
            print(f"[{utc_iso(observed_ms)}] quote rejected reason={reason}", flush=True)
        elif event == "quote_skip":
            print(f"[{utc_iso(observed_ms)}] quote skipped reason={reason} side={_raw_quote_side(raw)}", flush=True)
        elif event == "quote_cancel_skip":
            print(f"[{utc_iso(observed_ms)}] quote cancel skipped reason={reason}", flush=True)
        elif event == "cancel_all":
            print(f"[{utc_iso(observed_ms)}] cancel-all sent reason={reason}", flush=True)
        elif event == "cancel_all_skip":
            print(f"[{utc_iso(observed_ms)}] cancel-all skipped reason={reason}", flush=True)
        elif event == "flatten_submit":
            print(f"[{utc_iso(observed_ms)}] FLATTEN submitted reason={reason} {_raw_flatten_summary(raw)}", flush=True)
        elif event == "flatten_reject":
            print(f"[{utc_iso(observed_ms)}] FLATTEN rejected reason={reason}", flush=True)
        elif event == "flatten_error":
            print(f"[{utc_iso(observed_ms)}] FLATTEN error reason={reason}", flush=True)
        elif event == "flatten_skip":
            print(f"[{utc_iso(observed_ms)}] flatten skipped reason={reason}", flush=True)
        elif event == "unresolved_position_alert":
            print(f"[{utc_iso(observed_ms)}] UNRESOLVED POSITION reason={reason} {_raw_flatten_summary(raw)}", flush=True)
        elif event == "position_reconcile":
            print(f"[{utc_iso(observed_ms)}] position reconcile reason={reason} {_raw_position_summary(raw)}", flush=True)
        elif event == "position_reconcile_error":
            print(f"[{utc_iso(observed_ms)}] position reconcile error reason={reason}", flush=True)
        elif event == "position_mismatch_alert":
            print(f"[{utc_iso(observed_ms)}] POSITION MISMATCH entries halted {_raw_position_summary(raw)}", flush=True)
        elif event == "live_entry_halt":
            print(f"[{utc_iso(observed_ms)}] LIVE ENTRIES HALTED reason={reason}", flush=True)
        elif event == "open_orders_reconcile":
            print(f"[{utc_iso(observed_ms)}] open orders reconcile reason={reason}", flush=True)
        elif event == "order_update":
            print(f"[{utc_iso(observed_ms)}] order update {_raw_order_summary(raw)}", flush=True)
        elif event == "deadman_refresh":
            print(f"[{utc_iso(observed_ms)}] deadman refresh {_raw_status(raw)}", flush=True)
        elif event.endswith("_connected") or event.endswith("_disconnected") or event.endswith("_timeout"):
            print(f"[{utc_iso(observed_ms)}] {event} {reason}".rstrip(), flush=True)
        elif event in {"strategy_task_error", "strategy_task_stopped", "set_leverage"}:
            print(f"[{utc_iso(observed_ms)}] {event} {reason}".rstrip(), flush=True)


def _fmt_float(value: float | None, decimals: int) -> str:
    if value is None:
        return "-"
    return f"{value:.{decimals}f}"


def _fmt_ms(value: int | None) -> str:
    if value is None:
        return "-"
    return f"{value}ms"


def _fmt_order(order: ActiveOrder | None) -> str:
    if order is None:
        return "-"
    suffix = ":RO" if order.reduce_only else ""
    return f"{_fmt_float(order.price, 4)}x{_fmt_float(order.remaining_size if order.remaining_size is not None else order.size, 4)}{suffix}"


def _raw_quote_side(raw: Any | None) -> str:
    if isinstance(raw, dict):
        side = raw.get("quote_side")
        if side is not None:
            return str(side)
    return "-"


def _raw_status(raw: Any | None) -> str:
    if isinstance(raw, dict):
        status = raw.get("status")
        response = raw.get("response")
        if status is not None:
            return f"status={status} response={response}"
    return ""


def _raw_flatten_summary(raw: Any | None) -> str:
    if not isinstance(raw, dict):
        return ""
    size = raw.get("size")
    px = raw.get("limit_px")
    position = raw.get("position_size")
    is_buy = raw.get("is_buy")
    pieces = []
    if position is not None:
        pieces.append(f"pos={position}")
    if is_buy is not None:
        pieces.append(f"side={'buy' if is_buy else 'sell'}")
    if size is not None:
        pieces.append(f"sz={size}")
    if px is not None:
        pieces.append(f"px={px}")
    return " ".join(pieces)


def _raw_position_summary(raw: Any | None) -> str:
    if not isinstance(raw, dict):
        return ""
    exchange_position = raw.get("exchange_position")
    internal_inventory = raw.get("internal_inventory")
    diff = raw.get("diff")
    mismatch = raw.get("mismatch")
    pieces = []
    if exchange_position is not None:
        pieces.append(f"exchange={exchange_position}")
    if internal_inventory is not None:
        pieces.append(f"internal={internal_inventory}")
    if diff is not None:
        pieces.append(f"diff={diff}")
    if mismatch is not None:
        pieces.append(f"mismatch={mismatch}")
    return " ".join(pieces)


def _raw_order_summary(raw: Any | None) -> str:
    if not isinstance(raw, dict):
        return ""
    status = raw.get("status")
    response = raw.get("response")
    if status is not None:
        return f"status={status}" if response is None else f"status={status} response={response}"
    side = raw.get("side") or raw.get("quote_side")
    cloid = raw.get("cloid")
    price = raw.get("price")
    size = raw.get("size")
    pieces = []
    if side is not None:
        pieces.append(f"side={side}")
    if price is not None:
        pieces.append(f"px={price}")
    if size is not None:
        pieces.append(f"sz={size}")
    if cloid is not None:
        pieces.append(f"cloid={cloid}")
    return " ".join(pieces)
