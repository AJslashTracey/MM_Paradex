import http.client
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from execution.executor import HyperliquidExecutor, Position, _install_hyperliquid_post_fallback
from execution.oai_mm.basis_estimator import BasisEstimator
from execution.oai_mm.binance_feed import apply_binance_message, binance_stream_url
from execution.oai_mm.config import MMConfig
from execution.oai_mm.fair_value_model import FairValueModel
from execution.oai_mm.hyperliquid_feed import apply_hyperliquid_message
from execution.oai_mm.inventory_manager import InventoryManager
from execution.oai_mm.models import ActiveOrder, BotState, Level, QuoteIntent, VenueBook, VenueState
from execution.oai_mm.order_manager import OrderManager, parse_order_result
from execution.oai_mm.position_reconciler import PositionReconciler
from execution.oai_mm.quote_engine import QuoteEngine
from execution.oai_mm.risk_manager import RiskManager
from execution.oai_mm.utils import decimal_places_for_float


class OaiMmTests(unittest.TestCase):
    def _config(self, **overrides: object) -> MMConfig:
        with tempfile.TemporaryDirectory() as tmpdir:
            base = MMConfig(out_dir=Path(tmpdir))
        values = base.__dict__.copy()
        values.update(overrides)
        return MMConfig(**values)

    def _book(self, bid: float, ask: float, recv_ms: int) -> VenueBook:
        return VenueBook(
            bids=[Level(px=bid, sz=1.0, raw_px=f"{bid:.2f}", raw_sz="1.0")],
            asks=[Level(px=ask, sz=1.0, raw_px=f"{ask:.2f}", raw_sz="1.0")],
            exchange_time_ms=recv_ms - 10,
            recv_time_ms=recv_ms,
            source="test",
        )

    def test_basis_estimator_dedupes_same_sample_key(self) -> None:
        estimator = BasisEstimator(period=10)
        raw_basis, ema_basis = estimator.update(102.0, 100.0, (1, 1))
        self.assertAlmostEqual(raw_basis, 0.02)
        self.assertAlmostEqual(ema_basis, 0.02)
        raw_basis_again, ema_basis_again = estimator.update(102.0, 100.0, (1, 1))
        self.assertAlmostEqual(raw_basis_again, 0.02)
        self.assertAlmostEqual(ema_basis_again, 0.02)

    def test_fair_value_model_tracks_basis_and_binance_returns(self) -> None:
        config = self._config(strategy_mode="binance_basis", rapid_move_threshold_bps=5.0)
        model = FairValueModel(config)
        hl_state = VenueState(venue="hyperliquid", symbol="io:OAI", connected=True)
        binance_state = VenueState(venue="binance", symbol="OPENAIUSDT", connected=True)

        hl_state.book = self._book(101.90, 102.10, 1_000)
        binance_state.book = self._book(99.90, 100.10, 1_000)
        first = model.update(hl_state, binance_state, observed_ms=1_000)
        self.assertIsNotNone(first)
        self.assertAlmostEqual(first.fair_px or 0.0, 102.0, places=6)

        hl_state.book = self._book(102.90, 103.10, 7_000)
        binance_state.book = self._book(100.90, 101.10, 7_000)
        second = model.update(hl_state, binance_state, observed_ms=7_000)
        self.assertIsNotNone(second)
        self.assertGreater(second.binance_ret_5s_bps or 0.0, 90.0)
        self.assertEqual(second.rapid_move_side, "up")
        self.assertAlmostEqual(second.fair_px or 0.0, 103.02, places=2)

    def test_inventory_manager_realizes_pnl_and_generates_skew(self) -> None:
        inventory = InventoryManager()
        inventory.apply_fill(True, 1.0, 100.0, 0.1, 1_000)
        inventory.apply_fill(False, 0.4, 101.0, 0.05, 2_000)
        snapshot = inventory.snapshot(3_000, 100.5)
        self.assertAlmostEqual(snapshot.inventory, 0.6)
        self.assertAlmostEqual(snapshot.avg_entry_px or 0.0, 100.0)
        self.assertAlmostEqual(snapshot.realized_pnl, 0.4)
        self.assertLess(inventory.inventory_skew_bps(soft_limit=1.0, max_skew_bps=10.0), 0.0)

    def test_quote_engine_clamps_quotes_inside_current_spread(self) -> None:
        config = self._config(order_size=0.01, max_order_size=0.01, quote_half_spread_bps=2.0)
        engine = QuoteEngine(config)
        inventory = InventoryManager()
        hl_state = VenueState(venue="hyperliquid", symbol="io:OAI", connected=True)
        hl_state.book = self._book(100.00, 100.10, 1_000)
        plan = engine.build_plan(hl_state, fair_px=100.05, inventory=inventory, observed_ms=1_000)
        self.assertIsNotNone(plan)
        self.assertGreaterEqual(plan.bid.px, 100.00)
        self.assertLess(plan.bid.px, 100.10)
        self.assertGreater(plan.ask.px, 100.00)
        self.assertLessEqual(plan.ask.px, 100.10)

    def test_risk_manager_blocks_bid_at_hard_inventory_limit(self) -> None:
        config = self._config(hard_inventory_limit=0.04, max_open_notional=50.0)
        risk = RiskManager(config)
        hl_state = VenueState(venue="hyperliquid", symbol="io:OAI", connected=True, book=self._book(100.0, 100.1, 5_000))
        binance_state = VenueState(venue="binance", symbol="OPENAIUSDT", connected=True, book=self._book(99.0, 99.1, 5_000))
        fair_value = FairValueModel(config).update(hl_state, binance_state, observed_ms=5_000)
        decision = risk.evaluate(hl_state, binance_state, fair_value, inventory=0.04, open_notional=10.0, observed_ms=5_100)
        self.assertTrue(decision.quoting_allowed)
        self.assertTrue(decision.block_bid)
        self.assertFalse(decision.block_ask)

    def test_binance_parser_handles_book_trade_and_stream_url(self) -> None:
        state = VenueState(venue="binance", symbol="OPENAIUSDT")
        event = apply_binance_message(
            {
                "stream": "openaiusdt@bookTicker",
                "data": {"e": "bookTicker", "b": "1304.12", "B": "5.50", "a": "1304.34", "A": "4.25", "T": 1200},
            },
            state,
        )
        self.assertEqual(event, "binance_book")
        trade_event = apply_binance_message(
            {
                "stream": "openaiusdt@aggTrade",
                "data": {"e": "aggTrade", "p": "1304.2", "q": "0.5", "T": 1300, "a": 55, "m": False},
            },
            state,
        )
        self.assertEqual(trade_event, "binance_trade")
        self.assertEqual(state.last_trade.side, "buy")
        self.assertEqual(
            binance_stream_url("wss://fstream.binance.com/stream", "OPENAIUSDT", "depth5@100ms"),
            "wss://fstream.binance.com/stream?streams=openaiusdt@bookTicker/openaiusdt@markPrice@1s/openaiusdt@aggTrade/openaiusdt@depth5@100ms",
        )

    def test_hyperliquid_parser_handles_books_trades_and_snapshot_fills(self) -> None:
        state = VenueState(venue="hyperliquid", symbol="io:OAI")
        event_name, queue_items = apply_hyperliquid_message(
            {
                "channel": "l2Book",
                "data": {
                    "coin": "io:OAI",
                    "time": 777,
                    "levels": [[{"px": "1304.1", "sz": "0.5"}], [{"px": "1304.9", "sz": "0.4"}]],
                },
            },
            state,
        )
        self.assertEqual(event_name, "hl_book")
        self.assertEqual(queue_items, [])
        trade_name, _ = apply_hyperliquid_message(
            {
                "channel": "trades",
                "data": [{"coin": "io:OAI", "side": "B", "px": "1304.2", "sz": "1", "time": 888, "hash": "0xabc"}],
            },
            state,
        )
        self.assertEqual(trade_name, "hl_trade")
        snapshot_name, queue_items = apply_hyperliquid_message(
            {
                "channel": "userFills",
                "data": {"user": "0x1", "isSnapshot": True, "fills": [{"coin": "io:OAI"}]},
            },
            state,
        )
        self.assertEqual(snapshot_name, "hl_user_fills_snapshot")
        self.assertEqual(queue_items, [])

    def test_parse_order_result_extracts_resting_and_error_status(self) -> None:
        resting = parse_order_result(
            {
                "status": "ok",
                "response": {"data": {"statuses": [{"resting": {"oid": 12345}}]}},
            }
        )
        self.assertEqual(resting.status, "resting")
        self.assertEqual(resting.order_id, 12345)

        rejected = parse_order_result(
            {
                "status": "ok",
                "response": {"data": {"statuses": [{"error": "Order would cross"}]}},
            }
        )
        self.assertEqual(rejected.status, "error")

    def test_parse_order_result_handles_malformed_response_field(self) -> None:
        parsed = parse_order_result({"status": "ok", "response": "ok"})
        self.assertEqual(parsed.status, "unknown")
        self.assertIn("non_dict_response_field", parsed.error)

    def test_parse_order_result_handles_hyperliquid_err_response(self) -> None:
        parsed = parse_order_result({"status": "err", "response": "Too many cumulative requests sent"})
        self.assertEqual(parsed.status, "error")
        self.assertEqual(parsed.error, "Too many cumulative requests sent")

    def test_force_requote_respects_minimum_quote_lifetime(self) -> None:
        manager = OrderManager(
            self._config(min_quote_lifetime_ms=1_000),
            logger=None,
            inventory=InventoryManager(),
            executor=None,
            size_decimals=2,
        )
        active = ActiveOrder(
            quote_side="bid",
            is_buy=True,
            cloid_raw="0x1",
            price=100.0,
            size=0.01,
            placed_time_ms=10_000,
        )
        target = QuoteIntent(quote_side="bid", is_buy=True, px=101.0, size=0.01)

        self.assertFalse(manager._needs_replace(active, target, observed_ms=10_500, force=True))
        self.assertTrue(manager._needs_replace(active, target, observed_ms=11_000, force=True))

    def test_live_request_throttle_and_reject_cooldown(self) -> None:
        manager = OrderManager(
            self._config(
                live=True,
                live_order_action_min_interval_ms=5_000,
                live_reject_cooldown_ms=60_000,
            ),
            logger=None,
            inventory=InventoryManager(),
            executor=None,
            size_decimals=2,
        )
        manager.last_live_order_action_ms = 10_000

        self.assertIn("live_order_action_throttle", manager._live_order_throttle_reason(12_000) or "")
        self.assertIsNone(manager._live_order_throttle_reason(15_000))

        manager._record_request_limit("Too many cumulative requests sent", 20_000, source="entry")
        self.assertEqual(manager.live_order_blocked_until_ms, 80_000)
        self.assertEqual(manager.live_entries_halted_reason, "request_limit:entry")
        self.assertIn("live_entry_halt", manager._live_order_throttle_reason(30_000) or "")
        self.assertIsNone(manager._live_order_throttle_reason(30_000, allow_reduce_only=True))

    def test_deadman_disabled_does_not_call_executor(self) -> None:
        class FakeExecutor:
            def schedule_cancel_all(self, *, ms_from_now: int) -> dict[str, object]:
                raise AssertionError("deadman should be disabled")

        manager = OrderManager(
            self._config(live=True, deadman_ms=0),
            logger=None,
            inventory=InventoryManager(),
            executor=FakeExecutor(),
            size_decimals=2,
        )

        manager.refresh_deadman(10_000)

    def test_live_cancel_uses_shared_action_throttle(self) -> None:
        class FakeLogger:
            def log_event(self, *args: object, **kwargs: object) -> None:
                pass

        class FakeExecutor:
            def __init__(self) -> None:
                self.calls = 0

            def cancel_order_by_cloid(self, *, coin: str, cloid: object) -> dict[str, object]:
                self.calls += 1
                return {"status": "ok"}

        executor = FakeExecutor()
        manager = OrderManager(
            self._config(live=True, live_order_action_min_interval_ms=5_000),
            logger=FakeLogger(),
            inventory=InventoryManager(),
            executor=executor,
            size_decimals=2,
        )
        manager.active_orders["bid"] = ActiveOrder("bid", True, "0x" + "1" * 32, 100.0, 0.01, 1_000)
        manager.last_live_order_action_ms = 10_000

        self.assertFalse(manager._cancel_one("bid", "test", current_ms=12_000))
        self.assertEqual(executor.calls, 0)
        self.assertTrue(manager._cancel_one("bid", "test", current_ms=15_000))
        self.assertEqual(executor.calls, 1)

    def test_reduce_only_exit_bypasses_exposure_and_action_limits(self) -> None:
        class FakeLogger:
            def log_event(self, *args: object, **kwargs: object) -> None:
                pass

        class FakeExecutor:
            def __init__(self) -> None:
                self.kwargs: dict[str, object] = {}

            def place_limit_order(self, **kwargs: object) -> dict[str, object]:
                self.kwargs = kwargs
                return {"status": "ok", "response": {"data": {"statuses": [{"resting": {"oid": 9}}]}}}

        executor = FakeExecutor()
        manager = OrderManager(
            self._config(
                live=True,
                max_open_notional=1.0,
                live_order_action_min_interval_ms=15_000,
            ),
            logger=FakeLogger(),
            inventory=InventoryManager(),
            executor=executor,
            size_decimals=2,
        )
        manager.inventory.inventory = 0.01
        manager.last_live_order_action_ms = 10_000
        manager._place_one(QuoteIntent("ask", False, 100.0, 0.01), 10_001, "exit", reduce_only=True)

        self.assertEqual(executor.kwargs["reduce_only"], True)
        self.assertIn("ask", manager.active_orders)

    def test_emergency_cancel_is_single_flight_during_cooldown(self) -> None:
        class FakeLogger:
            def log_event(self, *args: object, **kwargs: object) -> None:
                pass

        class FakeExecutor:
            def __init__(self) -> None:
                self.calls = 0

            def cancel_all_for_coin(self, coin: str) -> list[dict[str, object]]:
                self.calls += 1
                return []

        executor = FakeExecutor()
        manager = OrderManager(
            self._config(
                live=True,
                live_cancel_all_min_interval_ms=5_000,
                live_cancel_all_when_no_active_orders=True,
            ),
            logger=FakeLogger(),
            inventory=InventoryManager(),
            executor=executor,
            size_decimals=2,
        )
        with mock.patch("execution.oai_mm.order_manager.now_ms", side_effect=[10_000, 12_000]):
            manager.cancel_all("risk", emergency=True)
            manager.cancel_all("risk", emergency=True)

        self.assertEqual(executor.calls, 1)

    def test_emergency_cancel_skips_when_no_active_orders_by_default(self) -> None:
        class FakeLogger:
            def __init__(self) -> None:
                self.events: list[tuple[str, str | None]] = []

            def log_event(self, event: str, reason: str | None = None, **_kwargs: object) -> None:
                self.events.append((event, reason))

        class FakeExecutor:
            def cancel_all_for_coin(self, coin: str) -> list[dict[str, object]]:
                raise AssertionError("cancel_all should not be called without active orders")

        logger = FakeLogger()
        manager = OrderManager(
            self._config(live=True),
            logger=logger,
            inventory=InventoryManager(),
            executor=FakeExecutor(),
            size_decimals=2,
        )

        manager.cancel_all("hyperliquid_stale", emergency=True)

        self.assertIn(("cancel_all_skip", "no_active_orders"), logger.events)

    def test_risk_manager_graces_feed_gaps_before_cancel(self) -> None:
        config = self._config(max_data_age_ms=1_000, feed_unhealthy_cancel_grace_ms=2_000)
        risk = RiskManager(config)
        hl_state = VenueState(venue="hyperliquid", symbol="io:OAI", connected=True, book=self._book(100.0, 100.1, 5_000))
        binance_state = VenueState(venue="binance", symbol="OPENAIUSDT", connected=True, book=self._book(99.0, 99.1, 5_000))
        fair_value = FairValueModel(config).update(hl_state, binance_state, observed_ms=5_000)

        first = risk.evaluate(hl_state, binance_state, fair_value, inventory=0.0, open_notional=0.0, observed_ms=7_000)
        second = risk.evaluate(hl_state, binance_state, fair_value, inventory=0.0, open_notional=0.0, observed_ms=9_000)

        self.assertFalse(first.quoting_allowed)
        self.assertFalse(first.should_cancel_all)
        self.assertEqual(first.reason, "hyperliquid_stale")
        self.assertTrue(second.should_cancel_all)

    def test_flatten_position_uses_reduce_only_ioc_and_bypasses_entry_halt(self) -> None:
        class FakeLogger:
            def __init__(self) -> None:
                self.events: list[tuple[str, str | None, object | None]] = []

            def log_event(self, event: str, reason: str | None = None, raw: object | None = None) -> None:
                self.events.append((event, reason, raw))

        class FakeExecutor:
            def __init__(self) -> None:
                self.kwargs: dict[str, object] = {}

            def exit_reduce_only_ioc(self, **kwargs: object) -> dict[str, object]:
                self.kwargs = kwargs
                return {"status": "ok", "response": {"data": {"statuses": [{"filled": {"oid": 11}}]}}}

        logger = FakeLogger()
        executor = FakeExecutor()
        manager = OrderManager(
            self._config(
                live=True,
                exit_ioc_price_protection_bps=10,
                flatten_cooldown_ms=0,
            ),
            logger=logger,
            inventory=InventoryManager(),
            executor=executor,
            size_decimals=2,
        )
        manager.live_entries_halted_reason = "request_limit:entry"
        hl_state = VenueState(venue="hyperliquid", symbol="io:OAI", connected=True, book=self._book(100.0, 100.1, 10_000))

        manager.flatten_position_if_needed(0.01, hl_state, observed_ms=10_000, reason="position_open")

        self.assertEqual(executor.kwargs["is_buy"], False)
        self.assertEqual(executor.kwargs["size"], 0.01)
        self.assertAlmostEqual(executor.kwargs["limit_px"], 99.9)
        self.assertTrue(any(event == "flatten_submit" for event, _reason, _raw in logger.events))

    def test_flatten_position_does_not_price_stale_book(self) -> None:
        class FakeLogger:
            def __init__(self) -> None:
                self.events: list[tuple[str, str | None]] = []

            def log_event(self, event: str, reason: str | None = None, **_kwargs: object) -> None:
                self.events.append((event, reason))

        class FakeExecutor:
            def exit_reduce_only_ioc(self, **_kwargs: object) -> dict[str, object]:
                raise AssertionError("flatten should wait for a fresh book")

        logger = FakeLogger()
        manager = OrderManager(
            self._config(live=True, max_data_age_ms=500),
            logger=logger,
            inventory=InventoryManager(),
            executor=FakeExecutor(),
            size_decimals=2,
        )
        hl_state = VenueState(venue="hyperliquid", symbol="io:OAI", connected=True, book=self._book(100.0, 100.1, 1_000))

        manager.flatten_position_if_needed(0.01, hl_state, observed_ms=2_000, reason="position_open")

        self.assertIn(("unresolved_position_alert", "hyperliquid_stale"), logger.events)

    def test_flatten_position_respects_separate_cooldown(self) -> None:
        class FakeLogger:
            def log_event(self, *args: object, **kwargs: object) -> None:
                pass

        class FakeExecutor:
            def __init__(self) -> None:
                self.calls = 0

            def exit_reduce_only_ioc(self, **_kwargs: object) -> dict[str, object]:
                self.calls += 1
                return {"status": "ok", "response": {"data": {"statuses": [{"filled": {"oid": 11}}]}}}

        executor = FakeExecutor()
        manager = OrderManager(
            self._config(live=True, flatten_cooldown_ms=30_000),
            logger=FakeLogger(),
            inventory=InventoryManager(),
            executor=executor,
            size_decimals=2,
        )
        hl_state = VenueState(venue="hyperliquid", symbol="io:OAI", connected=True, book=self._book(100.0, 100.1, 10_000))

        manager.flatten_position_if_needed(0.01, hl_state, observed_ms=10_000, reason="position_open")
        manager.flatten_position_if_needed(0.01, hl_state, observed_ms=20_000, reason="position_open")

        self.assertEqual(executor.calls, 1)

    def test_order_update_tracks_reduce_only_active_order(self) -> None:
        class FakeLogger:
            def log_event(self, *args: object, **kwargs: object) -> None:
                pass

        manager = OrderManager(
            self._config(),
            logger=FakeLogger(),
            inventory=InventoryManager(),
            executor=None,
            size_decimals=2,
        )
        payload = {
            "order": {
                "coin": "io:OAI",
                "side": "A",
                "limitPx": "100.0",
                "sz": "0.01",
                "oid": 42,
                "timestamp": 9_000,
                "reduceOnly": True,
            },
            "status": "open",
        }

        manager.handle_order_update(payload, observed_ms=10_000)

        self.assertTrue(manager.has_active_reducing_order(0.01))
        self.assertTrue(manager.active_orders["ask"].reduce_only)
        manager.active_orders["ask"].remaining_size = 0.005
        self.assertFalse(manager.has_active_reducing_order(0.01))
        manager.active_orders["ask"].remaining_size = 0.01

        manager.handle_order_update({**payload, "status": "filled"}, observed_ms=10_001)
        self.assertNotIn("ask", manager.active_orders)

    def test_position_reconciler_halts_entries_on_mismatch(self) -> None:
        class FakeLogger:
            def log_event(self, *args: object, **kwargs: object) -> None:
                pass

        class FakeExecutor:
            def get_position(self, coin: str) -> Position:
                return Position(
                    coin=coin,
                    size=0.02,
                    entry_px=100.0,
                    unrealized_pnl=0.0,
                    liquidation_px=None,
                    raw={},
                )

        config = self._config(live=True, position_reconcile_tolerance=0.001)
        bot_state = BotState(
            hl=VenueState(venue="hyperliquid", symbol="io:OAI"),
            binance=VenueState(venue="binance", symbol="OPENAIUSDT"),
        )
        inventory = InventoryManager()
        inventory.inventory = 0.01
        reconciler = PositionReconciler(config, bot_state, inventory, FakeExecutor(), FakeLogger())

        reconciler.reconcile(10_000, reason="test")

        self.assertEqual(reconciler.entry_halt_reason(), "position_mismatch")

    def test_decimal_places_for_float_preserves_small_config_sizes(self) -> None:
        self.assertEqual(decimal_places_for_float(0.01), 2)
        self.assertEqual(decimal_places_for_float(1.0), 0)

    def test_hyperliquid_executor_initializes_builder_dex_for_prefixed_coin(self) -> None:
        captured: dict[str, object] = {}

        class FakeAPI:
            pass

        class FakeClientError(Exception):
            pass

        class FakeServerError(Exception):
            pass

        class FakeExchange:
            def __init__(self, **kwargs: object) -> None:
                captured.update(kwargs)
                self.info = SimpleNamespace()

        with (
            mock.patch.dict(os.environ, {"PK": "0x" + "11" * 32, "ADDRESS": "0x" + "22" * 20}, clear=False),
            mock.patch("execution.executor.load_dotenv"),
            mock.patch("execution.executor.Account.from_key", return_value="wallet"),
            mock.patch("hyperliquid.api.API", FakeAPI),
            mock.patch("hyperliquid.exchange.Exchange", FakeExchange),
            mock.patch("hyperliquid.utils.constants.MAINNET_API_URL", "https://mainnet.example"),
            mock.patch("hyperliquid.utils.constants.TESTNET_API_URL", "https://testnet.example"),
            mock.patch("hyperliquid.utils.error.ClientError", FakeClientError),
            mock.patch("hyperliquid.utils.error.ServerError", FakeServerError),
        ):
            executor = HyperliquidExecutor(testnet=False, target_coin="io:OAI", timeout_s=12.5)

        self.assertEqual(executor.default_perp_dex, "io")
        self.assertEqual(captured["perp_dexs"], ["io"])
        self.assertEqual(captured["timeout"], 12.5)
        self.assertEqual(captured["account_address"], "0x" + "22" * 20)

    def test_hyperliquid_executor_scopes_state_queries_to_builder_dex(self) -> None:
        class FakeInfo:
            def __init__(self) -> None:
                self.calls: list[tuple[str, str, str]] = []

            def user_state(self, address: str, dex: str = "") -> dict[str, object]:
                self.calls.append(("user_state", address, dex))
                return {
                    "assetPositions": [
                        {
                            "position": {
                                "coin": "io:OAI",
                                "szi": "0.02",
                                "entryPx": "1350.5",
                                "unrealizedPnl": "1.25",
                                "liquidationPx": "1200.0",
                            }
                        }
                    ]
                }

            def open_orders(self, address: str, dex: str = "") -> list[dict[str, object]]:
                self.calls.append(("open_orders", address, dex))
                return [{"coin": "io:OAI", "oid": 7}]

        fake_info = FakeInfo()
        executor = HyperliquidExecutor.__new__(HyperliquidExecutor)
        executor.address = "0xabc"
        executor.default_perp_dex = "io"
        executor.info = fake_info

        position = executor.get_position("io:OAI")
        orders = executor.get_open_orders("io:OAI")

        self.assertIsNotNone(position)
        self.assertAlmostEqual(position.size, 0.02)
        self.assertEqual(orders, [{"coin": "io:OAI", "oid": 7}])
        self.assertEqual(
            fake_info.calls,
            [
                ("user_state", "0xabc", "io"),
                ("open_orders", "0xabc", "io"),
            ],
        )

    def test_hyperliquid_post_fallback_retries_remote_disconnect(self) -> None:
        class FakeAPI:
            base_url = "https://api.example"
            timeout = 5.0

        class FakeClientError(Exception):
            pass

        class FakeServerError(Exception):
            pass

        class FakeResponse:
            def __enter__(self) -> "FakeResponse":
                return self

            def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
                return False

            def read(self) -> bytes:
                return b'{"ok": true}'

        _install_hyperliquid_post_fallback(FakeAPI, FakeClientError, FakeServerError)

        with (
            mock.patch("urllib.request.urlopen", side_effect=[http.client.RemoteDisconnected("eof"), FakeResponse()]) as urlopen,
            mock.patch("execution.executor.time.sleep"),
        ):
            result = FakeAPI().post("/info", {"type": "meta"})

        self.assertEqual(result, {"ok": True})
        self.assertEqual(urlopen.call_count, 2)


if __name__ == "__main__":
    unittest.main()
