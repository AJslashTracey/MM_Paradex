import unittest

from execution.hl_binance_pair_collector import (
    apply_binance_message,
    apply_hyperliquid_message,
    binance_stream_url,
)
from execution.unitree_lag_bot import BookState


class HlBinancePairCollectorTests(unittest.TestCase):
    def test_apply_binance_book_ticker_updates_reference_book(self) -> None:
        state = BookState("binance:OPENAIUSDT")
        updated = apply_binance_message(
            {
                "stream": "openaiusdt@bookTicker",
                "data": {
                    "e": "bookTicker",
                    "b": "1304.12",
                    "B": "5.50",
                    "a": "1304.34",
                    "A": "4.25",
                    "E": 1234,
                    "T": 1200,
                },
            },
            state,
        )
        self.assertTrue(updated)
        self.assertEqual(state.bids, [{"px": "1304.12", "sz": "5.50"}])
        self.assertEqual(state.asks, [{"px": "1304.34", "sz": "4.25"}])
        self.assertEqual(state.book_time_ms, 1200)
        self.assertIsNotNone(state.recv_time_ms)

    def test_apply_binance_mark_price_updates_reference_ctx(self) -> None:
        state = BookState("binance:OPENAIUSDT")
        updated = apply_binance_message(
            {
                "stream": "openaiusdt@markPrice@1s",
                "data": {
                    "e": "markPriceUpdate",
                    "p": "1305.5",
                    "i": "1304.8",
                },
            },
            state,
        )
        self.assertTrue(updated)
        self.assertEqual(
            state.ctx,
            {"markPx": "1305.5", "oraclePx": "1304.8", "midPx": "1305.5"},
        )
        self.assertIsNotNone(state.ctx_recv_time_ms)

    def test_apply_hyperliquid_l2_book_updates_target_book(self) -> None:
        state = BookState("io:OAI")
        updated = apply_hyperliquid_message(
            {
                "channel": "l2Book",
                "data": {
                    "coin": "io:OAI",
                    "time": 777,
                    "levels": [
                        [{"px": "1304.1", "sz": "0.5"}],
                        [{"px": "1304.9", "sz": "0.4"}],
                    ],
                },
            },
            "io:OAI",
            state,
        )
        self.assertTrue(updated)
        self.assertEqual(state.bids, [{"px": "1304.1", "sz": "0.5"}])
        self.assertEqual(state.asks, [{"px": "1304.9", "sz": "0.4"}])
        self.assertEqual(state.book_time_ms, 777)
        self.assertIsNotNone(state.recv_time_ms)

    def test_builds_binance_stream_url(self) -> None:
        self.assertEqual(
            binance_stream_url("wss://fstream.binance.com/stream", "OPENAIUSDT"),
            "wss://fstream.binance.com/stream?streams=openaiusdt@bookTicker/openaiusdt@markPrice@1s",
        )


if __name__ == "__main__":
    unittest.main()
