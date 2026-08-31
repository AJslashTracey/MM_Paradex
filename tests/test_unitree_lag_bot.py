import argparse
import csv
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from execution.unitree_lag_bot import BookState, MarketDataLogger, PairConfig, validate_args


class UnitreeLagBotTests(unittest.TestCase):
    def _read_rows(self, path: Path) -> list[dict[str, str]]:
        with path.open(newline="", encoding="utf-8") as handle:
            return list(csv.DictReader(handle))

    def test_market_logger_marks_stale_snapshots_and_logs_freshness(self) -> None:
        pair = PairConfig("para:UNITREE", "xyz:UNITREE")
        states = {
            pair.target_coin: BookState(
                coin=pair.target_coin,
                book_time_ms=1_000,
                recv_time_ms=1_050,
                bids=[{"px": "100", "sz": "1"}],
                asks=[{"px": "101", "sz": "1"}],
                ctx={"oraclePx": "100.5", "markPx": "100.5"},
            ),
            pair.reference_coin: BookState(
                coin=pair.reference_coin,
                book_time_ms=1_100,
                recv_time_ms=1_150,
                bids=[{"px": "100.2", "sz": "1"}],
                asks=[{"px": "100.4", "sz": "1"}],
                ctx={"oraclePx": "100.3", "markPx": "100.3"},
            ),
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "market.csv"
            logger = MarketDataLogger(path, dry_run=True)
            with patch("execution.unitree_lag_bot.now_ms", return_value=2_000):
                logger.write(
                    event="snapshot",
                    pair=pair,
                    signal=None,
                    states=states,
                    position=None,
                    max_book_age_ms=750,
                    max_cross_recv_skew_ms=250,
                    connection_seq=3,
                )
            logger.close()

            rows = self._read_rows(path)
            self.assertEqual(len(rows), 1)
            row = rows[0]
            self.assertEqual(row["event"], "snapshot_stale")
            self.assertEqual(row["connection_seq"], "3")
            self.assertEqual(row["target_book_age_ms"], "1000")
            self.assertEqual(row["reference_book_age_ms"], "900")
            self.assertEqual(row["target_recv_lag_ms"], "50")
            self.assertEqual(row["reference_recv_lag_ms"], "50")
            self.assertEqual(row["cross_recv_skew_ms"], "100")
            self.assertEqual(row["target_book_is_fresh"], "false")
            self.assertEqual(row["reference_book_is_fresh"], "false")
            self.assertEqual(row["pair_is_synchronized"], "true")
            self.assertEqual(row["long_edge_bps"], "-79.760718")
            self.assertEqual(row["short_edge_bps"], "-39.880359")
            self.assertEqual(row["best_edge_bps"], "-39.880359")

    def test_market_logger_skips_incomplete_snapshots(self) -> None:
        pair = PairConfig("para:UNITREE", "xyz:UNITREE")
        states = {
            pair.target_coin: BookState(
                coin=pair.target_coin,
                book_time_ms=1_000,
                recv_time_ms=1_050,
                bids=[{"px": "100", "sz": "1"}],
                asks=[{"px": "101", "sz": "1"}],
            ),
            pair.reference_coin: BookState(coin=pair.reference_coin),
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "market.csv"
            logger = MarketDataLogger(path, dry_run=True)
            logger.write(
                event="snapshot",
                pair=pair,
                signal=None,
                states=states,
                position=None,
                max_book_age_ms=750,
                max_cross_recv_skew_ms=250,
                connection_seq=1,
            )
            logger.close()

            rows = self._read_rows(path)
            self.assertEqual(rows, [])

    def test_validate_args_rejects_collect_only_live_combo(self) -> None:
        args = argparse.Namespace(
            live=True,
            collect_only=True,
            order_notional=20.0,
            max_order_notional=20.0,
            max_order_size=None,
            min_order_notional=5.0,
            max_active_positions=1,
            entry_edge_bps_override=None,
            max_cross_recv_skew_ms=250,
            target_filled_volume_usd=None,
        )
        with self.assertRaisesRegex(ValueError, "--collect-only cannot be combined with --live"):
            validate_args(args)


if __name__ == "__main__":
    unittest.main()
