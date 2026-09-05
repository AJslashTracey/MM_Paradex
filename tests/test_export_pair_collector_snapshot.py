import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from execution.export_pair_collector_snapshot import export_snapshot


MARKET_DATA = """time_utc,event,pair,best_edge_bps,target_book_age_ms,reference_book_age_ms,cross_recv_skew_ms,target_book_is_fresh,reference_book_is_fresh,pair_is_synchronized
2026-09-03T09:00:00.000Z,snapshot,io:OAI|binance:OPENAIUSDT,12.0,100,120,20,true,true,true
2026-09-03T09:00:00.500Z,snapshot_stale,io:OAI|binance:OPENAIUSDT,8.0,900,130,30,false,true,false
2026-09-03T09:00:01.000Z,snapshot_desynced,io:OAI|binance:OPENAIUSDT,-1.0,110,140,15,true,true,false
"""

COLLECTOR_EVENTS = """time_utc,event
2026-09-03T09:00:02.000Z,error
2026-09-03T09:00:03.000Z,error
"""

COLLECTOR_FILLS = """time_utc,mode
2026-09-03T09:00:04.000Z,dry_run
"""


class ExportPairCollectorSnapshotTests(unittest.TestCase):
    def test_exports_snapshot_and_summary(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            pair_dir = temp_path / "io_OAI__binance_OPENAIUSDT"
            pair_dir.mkdir()
            (pair_dir / "market_data.csv").write_text(MARKET_DATA)
            (pair_dir / "collector_events.csv").write_text(COLLECTOR_EVENTS)
            (pair_dir / "collector_fills.csv").write_text(COLLECTOR_FILLS)

            export_dir = export_snapshot(
                [str(pair_dir)],
                temp_path / "review_exports",
                export_name="2026-09-03-io-oai-binance-openaiusdt-snapshot",
            )

            self.assertTrue((export_dir / "README.md").is_file())
            self.assertTrue((export_dir / "summary.json").is_file())
            self.assertTrue((export_dir / "io_OAI__binance_OPENAIUSDT_market_data.csv").is_file())
            self.assertTrue((export_dir / "io_OAI__binance_OPENAIUSDT_collector_events.csv").is_file())
            self.assertTrue((export_dir / "io_OAI__binance_OPENAIUSDT_collector_fills.csv").is_file())

            summary = json.loads((export_dir / "summary.json").read_text())
            pair_summary = summary["pairs"]["io:OAI|binance:OPENAIUSDT"]

            self.assertEqual(summary["generated_utc"], "2026-09-03T09:00:01.000Z")
            self.assertEqual(pair_summary["rows"], 3)
            self.assertEqual(pair_summary["snapshot_rows"], 1)
            self.assertEqual(pair_summary["snapshot_stale_rows"], 1)
            self.assertEqual(pair_summary["snapshot_desynced_rows"], 1)
            self.assertEqual(pair_summary["clean_rows"], 1)
            self.assertEqual(pair_summary["target_fresh_rows"], 2)
            self.assertEqual(pair_summary["reference_fresh_rows"], 3)
            self.assertEqual(pair_summary["sync_rows"], 1)
            self.assertEqual(pair_summary["collector_event_rows"], 2)
            self.assertEqual(pair_summary["collector_fill_rows"], 1)
            self.assertEqual(pair_summary["clean_edge_ge_10bps"], 1)
            self.assertAlmostEqual(pair_summary["all_edge_mean_bps"], 6.333333, places=6)
            self.assertAlmostEqual(pair_summary["clean_edge_mean_bps"], 12.0, places=6)

            readme = (export_dir / "README.md").read_text()
            self.assertIn("io:OAI|binance:OPENAIUSDT", readme)
            self.assertIn("clean rows", readme)

    @unittest.skipUnless(shutil.which("zstd"), "zstd is not installed")
    def test_exports_compressed_time_window(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            pair_dir = temp_path / "io_OAI__binance_OPENAIUSDT"
            pair_dir.mkdir()
            (pair_dir / "market_data.csv").write_text(MARKET_DATA)
            (pair_dir / "collector_events.csv").write_text(COLLECTOR_EVENTS)
            (pair_dir / "collector_fills.csv").write_text(COLLECTOR_FILLS)

            export_dir = export_snapshot(
                [str(pair_dir)],
                temp_path / "review_exports",
                export_name="compressed-window",
                compression="zstd",
                last_hours=0.5 / 3600,
            )

            market_path = export_dir / "io_OAI__binance_OPENAIUSDT_market_data.csv.zst"
            self.assertTrue(market_path.is_file())
            self.assertFalse((export_dir / "io_OAI__binance_OPENAIUSDT_market_data.csv").exists())
            decompressed = subprocess.run(
                ["zstd", "-q", "-d", "-c", str(market_path)],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            self.assertNotIn("09:00:00.000Z", decompressed)
            self.assertIn("09:00:00.500Z", decompressed)
            self.assertIn("09:00:01.000Z", decompressed)

            summary = json.loads((export_dir / "summary.json").read_text())
            pair_summary = summary["pairs"]["io:OAI|binance:OPENAIUSDT"]
            self.assertEqual(summary["compression"], "zstd")
            self.assertEqual(pair_summary["rows"], 2)
            self.assertEqual(pair_summary["collector_event_rows"], 0)
            self.assertEqual(pair_summary["collector_fill_rows"], 0)
            self.assertEqual(pair_summary["window_start_utc"], "2026-09-03T09:00:00.500Z")
            self.assertEqual(pair_summary["window_end_utc"], "2026-09-03T09:00:01.000Z")


if __name__ == "__main__":
    unittest.main()
