import tempfile
import unittest
from pathlib import Path

from execution.oai_mm.basis_estimator import BasisEstimator
from execution.oai_mm.binance_feed import apply_binance_message, binance_stream_url
from execution.oai_mm.config import MMConfig
from execution.oai_mm.fair_value_model import FairValueModel
from execution.oai_mm.hyperliquid_feed import apply_hyperliquid_message
from execution.oai_mm.inventory_manager import InventoryManager
from execution.oai_mm.models import Level, VenueBook, VenueState
from execution.oai_mm.order_manager import parse_order_result
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

    def test_decimal_places_for_float_preserves_small_config_sizes(self) -> None:
        self.assertEqual(decimal_places_for_float(0.01), 2)
        self.assertEqual(decimal_places_for_float(1.0), 0)


if __name__ == "__main__":
    unittest.main()
