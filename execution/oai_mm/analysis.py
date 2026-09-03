from __future__ import annotations

import argparse
import csv
import json
from collections import defaultdict
from pathlib import Path
from statistics import mean


def _read_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _to_float(value: str) -> float | None:
    if value == "":
        return None
    try:
        return float(value)
    except ValueError:
        return None


def summarize_run(out_dir: Path) -> dict[str, object]:
    fill_rows = _read_rows(out_dir / "fills.csv")
    market_rows = _read_rows(out_dir / "market.csv")
    latest_by_fill: dict[str, dict[str, str]] = {}
    for row in fill_rows:
        latest_by_fill[row["fill_key"]] = row
    finalized = list(latest_by_fill.values())
    if not finalized:
        return {
            "out_dir": str(out_dir),
            "strategy_mode": "",
            "total_fills": 0,
            "maker_volume": 0.0,
        }
    mode = finalized[0]["strategy_mode"]
    maker_volume = sum((_to_float(row["size"]) or 0.0) * (_to_float(row["price"]) or 0.0) for row in finalized)
    spread_capture = [_to_float(row["edge_vs_io_mid_bps"]) for row in finalized if _to_float(row["edge_vs_io_mid_bps"]) is not None]
    markout_means = {}
    positive_markout_pct = {}
    for window in (1, 5, 10, 30, 60):
        values = [_to_float(row[f"markout_{window}s_bps"]) for row in finalized if _to_float(row[f"markout_{window}s_bps"]) is not None]
        markout_means[str(window)] = None if not values else mean(values)
        positive_markout_pct[str(window)] = None if not values else sum(1 for value in values if value > 0) / len(values) * 100.0
    realized_pnl = sum(_to_float(row["realized_pnl_delta"]) or 0.0 for row in finalized)
    fees = sum(_to_float(row["fee"]) or 0.0 for row in finalized)

    inventory_values = [_to_float(row["inventory"]) for row in market_rows if _to_float(row["inventory"]) is not None]
    unrealized_values = [_to_float(row["unrealized_pnl"]) for row in market_rows if _to_float(row["unrealized_pnl"]) is not None]
    after_move_rows = [row for row in finalized if row["after_binance_move"] == "true"]
    quiet_rows = [row for row in finalized if row["after_binance_move"] != "true"]
    up_move_rows = [row for row in after_move_rows if row["rapid_move_side"] == "up"]
    down_move_rows = [row for row in after_move_rows if row["rapid_move_side"] == "down"]

    def avg_markout(rows: list[dict[str, str]], column: str) -> float | None:
        values = [_to_float(row[column]) for row in rows if _to_float(row[column]) is not None]
        return None if not values else mean(values)

    return {
        "out_dir": str(out_dir),
        "strategy_mode": mode,
        "total_fills": len(finalized),
        "maker_volume": maker_volume,
        "average_spread_captured_bps": None if not spread_capture else mean(spread_capture),
        "average_markout_bps": markout_means,
        "realized_pnl": realized_pnl,
        "fees": fees,
        "max_inventory": None if not inventory_values else max(abs(value) for value in inventory_values),
        "avg_abs_inventory": None if not inventory_values else mean(abs(value) for value in inventory_values),
        "inventory_pnl": None if not unrealized_values else unrealized_values[-1],
        "positive_markout_pct": positive_markout_pct,
        "after_move_fill_count": len(after_move_rows),
        "after_move_avg_5s_markout_bps": avg_markout(after_move_rows, "markout_5s_bps"),
        "after_up_move_fill_count": len(up_move_rows),
        "after_up_move_avg_5s_markout_bps": avg_markout(up_move_rows, "markout_5s_bps"),
        "after_down_move_fill_count": len(down_move_rows),
        "after_down_move_avg_5s_markout_bps": avg_markout(down_move_rows, "markout_5s_bps"),
        "no_move_fill_count": len(quiet_rows),
        "no_move_avg_5s_markout_bps": avg_markout(quiet_rows, "markout_5s_bps"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Summarize OAI MM logs and compare strategy modes.")
    parser.add_argument("out_dirs", nargs="+", type=Path)
    args = parser.parse_args()

    summaries = [summarize_run(path) for path in args.out_dirs]
    grouped: dict[str, list[dict[str, object]]] = defaultdict(list)
    for summary in summaries:
        grouped[str(summary.get("strategy_mode", ""))].append(summary)
    result = {"runs": summaries, "grouped_by_strategy_mode": grouped}
    print(json.dumps(result, indent=2, default=list))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
