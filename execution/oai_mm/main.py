from __future__ import annotations

import asyncio
import signal

from execution.executor import HyperliquidExecutor

from .binance_feed import BinanceFeed
from .config import build_arg_parser, config_from_args
from .fair_value_model import FairValueModel
from .hyperliquid_feed import HyperliquidFeed
from .inventory_manager import InventoryManager
from .logger import MMLogger
from .models import BotState, VenueState
from .order_manager import OrderManager
from .position_reconciler import PositionReconciler
from .quote_engine import QuoteEngine
from .risk_manager import RiskManager
from .utils import decimal_places_for_float, load_size_decimals, now_ms, utc_iso


async def run_bot() -> int:
    args = build_arg_parser().parse_args()
    config = config_from_args(args)
    hl_state = VenueState(venue="hyperliquid", symbol=config.target_coin)
    binance_state = VenueState(venue="binance", symbol=config.binance_symbol)
    state = BotState(hl=hl_state, binance=binance_state)
    inventory = InventoryManager()
    executor = (
        HyperliquidExecutor(
            testnet=config.testnet,
            target_coin=config.target_coin,
            timeout_s=config.http_timeout,
        )
        if config.live
        else None
    )
    size_decimals = (
        load_size_decimals(config.target_coin, config.http_timeout)
        if config.live
        else max(decimal_places_for_float(config.order_size), decimal_places_for_float(config.max_order_size))
    )
    trigger = asyncio.Event()
    user_event_queue: asyncio.Queue[tuple[str, dict[str, object]]] = asyncio.Queue()

    order_manager = OrderManager(config, None, inventory, executor, size_decimals)
    logger = MMLogger(config, state, inventory, order_manager)
    order_manager.logger = logger
    position_reconciler = PositionReconciler(config, state, inventory, executor, logger)

    if config.live and executor is not None:
        startup_ms = now_ms()
        existing_orders = executor.get_open_orders(config.target_coin)
        order_manager.ingest_open_orders(existing_orders, startup_ms, reason="startup")
        existing_position = executor.get_position(config.target_coin)
        position_reconciler.seed_exchange_position(existing_position, startup_ms, reason="startup")

    if config.live and executor is not None and config.set_leverage_on_start:
        raw = executor.set_leverage(coin=config.target_coin, leverage=config.leverage, cross=False)
        logger.log_event("set_leverage", raw=raw)

    fair_value_model = FairValueModel(config)
    quote_engine = QuoteEngine(config)
    risk_manager = RiskManager(config)

    stop = asyncio.Event()

    def request_stop() -> None:
        stop.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, request_stop)
        except NotImplementedError:
            pass
    if config.duration_s is not None:
        loop.call_later(config.duration_s, request_stop)

    hl_feed = HyperliquidFeed(
        config=config,
        state=hl_state,
        logger=logger,
        trigger=trigger,
        user_event_queue=user_event_queue,
        user_address=None if executor is None else executor.address,
    )
    binance_feed = BinanceFeed(config=config, state=binance_state, logger=logger, trigger=trigger)

    print(
        f"[{utc_iso()}] mode={'live' if config.live else 'dry_run'} strategy_mode={config.strategy_mode} "
        f"target={config.target_coin} reference={config.binance_symbol} out_dir={config.out_dir}",
        flush=True,
    )

    async def strategy_loop() -> None:
        last_market_snapshot_ms = 0
        last_force_requote_source_ms: int | None = None
        while not stop.is_set():
            try:
                await asyncio.wait_for(trigger.wait(), timeout=config.strategy_loop_interval_ms / 1000)
            except asyncio.TimeoutError:
                pass
            trigger.clear()
            observed_ms = now_ms()
            inventory.observe_time(observed_ms)

            state.fair_value = fair_value_model.update(hl_state, binance_state, observed_ms)

            while not user_event_queue.empty():
                event_name, payload = await user_event_queue.get()
                if event_name == "hl_user_fills":
                    order_manager.handle_fill(payload, state.fair_value, observed_ms)
                    position_reconciler.update_internal()
                elif event_name == "hl_order_updates":
                    order_manager.handle_order_update(payload, observed_ms)

            position_reconciler.maybe_reconcile(observed_ms)

            if state.fair_value is not None and state.fair_value.fair_px is not None:
                state.quote_plan = quote_engine.build_plan(hl_state, state.fair_value.fair_px, inventory, observed_ms)
            else:
                state.quote_plan = None

            state.risk = risk_manager.evaluate(
                hl_state=hl_state,
                binance_state=binance_state,
                fair_value=state.fair_value,
                inventory=inventory.inventory,
                open_notional=order_manager.total_open_notional(),
                observed_ms=observed_ms,
            )

            order_manager.resolve_markouts(observed_ms, hl_state.mid())
            order_manager.maybe_paper_fill_cross(
                None if hl_state.best_bid() is None else hl_state.best_bid().px,
                None if hl_state.best_ask() is None else hl_state.best_ask().px,
                state.fair_value,
                observed_ms,
            )

            position_to_flatten = position_reconciler.position_to_flatten()
            position_halt_reason = position_reconciler.entry_halt_reason()
            live_entry_halt_reason = (
                None
                if order_manager.live_entries_halted_reason is None
                else f"live_entry_halt:{order_manager.live_entries_halted_reason}"
            )
            entry_halt_reason = position_halt_reason or live_entry_halt_reason

            if abs(position_to_flatten) > config.position_reconcile_tolerance:
                order_manager.cancel_non_reducing_orders("position_exit_priority", observed_ms)
                order_manager.flatten_position_if_needed(
                    position_to_flatten,
                    hl_state,
                    observed_ms,
                    position_halt_reason or "position_open",
                )
            elif entry_halt_reason is not None:
                order_manager.cancel_non_reducing_orders(entry_halt_reason, observed_ms)
            elif state.risk.should_cancel_all:
                order_manager.cancel_all(state.risk.reason or "risk_cancel", emergency=True)
            elif not state.risk.quoting_allowed or state.quote_plan is None:
                pass
            else:
                force = False
                if (
                    state.fair_value is not None
                    and state.fair_value.recent_rapid_move_time_ms is not None
                    and state.fair_value.recent_rapid_move_time_ms != last_force_requote_source_ms
                    and state.fair_value.rapid_move_bps is not None
                    and abs(state.fair_value.rapid_move_bps) >= config.force_requote_threshold_bps
                ):
                    force = True
                    last_force_requote_source_ms = state.fair_value.recent_rapid_move_time_ms
                order_manager.sync_quotes(
                    plan=state.quote_plan,
                    observed_ms=observed_ms,
                    force=force,
                    block_bid=state.risk.block_bid,
                    block_ask=state.risk.block_ask,
                )
                order_manager.refresh_deadman(observed_ms)

            if observed_ms - last_market_snapshot_ms >= config.market_snapshot_interval_ms:
                logger.log_market_snapshot(observed_ms)
                last_market_snapshot_ms = observed_ms

    tasks = [
        asyncio.create_task(hl_feed.run(stop)),
        asyncio.create_task(binance_feed.run(stop)),
        asyncio.create_task(strategy_loop()),
    ]
    stop_task = asyncio.create_task(stop.wait())
    try:
        done, _pending = await asyncio.wait([*tasks, stop_task], return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            if task is stop_task:
                continue
            exc = task.exception()
            if exc is not None:
                logger.log_event("strategy_task_error", reason=str(exc), raw={"error": str(exc)})
                raise exc
            logger.log_event("strategy_task_stopped")
            stop.set()
    finally:
        stop_task.cancel()
        try:
            await stop_task
        except asyncio.CancelledError:
            pass
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
        try:
            order_manager.cancel_all("shutdown", emergency=True)
        finally:
            order_manager.flush_pending_markouts()
            logger.close()
    return 0


def main() -> int:
    return asyncio.run(run_bot())


if __name__ == "__main__":
    raise SystemExit(main())
