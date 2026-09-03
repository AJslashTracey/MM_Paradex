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
    "active_bid_px",
    "active_bid_sz",
    "active_bid_oid",
    "active_bid_cloid",
    "active_ask_px",
    "active_ask_sz",
    "active_ask_oid",
    "active_ask_cloid",
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
            "active_bid_px": "" if bid_order is None else bid_order.price,
            "active_bid_sz": "" if bid_order is None else bid_order.remaining_size if bid_order.remaining_size is not None else bid_order.size,
            "active_bid_oid": "" if bid_order is None or bid_order.order_id is None else bid_order.order_id,
            "active_bid_cloid": "" if bid_order is None else bid_order.cloid_raw,
            "active_ask_px": "" if ask_order is None else ask_order.price,
            "active_ask_sz": "" if ask_order is None else ask_order.remaining_size if ask_order.remaining_size is not None else ask_order.size,
            "active_ask_oid": "" if ask_order is None or ask_order.order_id is None else ask_order.order_id,
            "active_ask_cloid": "" if ask_order is None else ask_order.cloid_raw,
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

    def log_market_snapshot(self, observed_ms: int) -> None:
        row = self._common_row(observed_ms)
        row["event"] = "snapshot"
        row["reason"] = ""
        row["raw_json"] = ""
        self._market_writer.writerow(row)
        self._market_file.flush()

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
