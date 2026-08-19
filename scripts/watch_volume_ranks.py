#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
import time
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from arb_xyz_bot.scanner import row_to_dict, scan
from arb_xyz_bot.volume_tracker import VolumeRankTracker, VolumeTrackerResult


def usd(value: Decimal | None) -> str:
    if value is None:
        return "-"
    if value >= Decimal("1000000"):
        return f"${value / Decimal('1000000'):.2f}M"
    if value >= Decimal("1000"):
        return f"${value / Decimal('1000'):.2f}K"
    return f"${value:.2f}"


def result_to_dict(result: VolumeTrackerResult) -> dict[str, object]:
    return {
        "generated_at": result.generated_at,
        "ranked": [
            {
                "rank": market.rank,
                "symbol": market.symbol,
                "coin": market.coin,
                "day_volume_usd": str(market.day_volume_usd),
                "price": str(market.price) if market.price is not None else None,
                "open_interest_usd": str(market.open_interest_usd)
                if market.open_interest_usd is not None
                else None,
            }
            for market in result.ranked
        ],
        "triggers": [
            {
                "symbol": trigger.symbol,
                "coin": trigger.coin,
                "current_rank": trigger.current_rank,
                "previous_rank": trigger.previous_rank,
                "rank_change": trigger.rank_change,
                "day_volume_usd": str(trigger.day_volume_usd),
                "reason": trigger.reason,
            }
            for trigger in result.triggers
        ],
        "tracked_symbols": result.tracked_symbols,
    }


def append_tracked_scan(state_dir: Path, generated_at: str, symbols: list[str]) -> None:
    if not symbols:
        return
    rows = scan(top=max(len(symbols), 1), symbols=set(symbols))
    payload = {
        "generated_at": generated_at,
        "tracked_symbols": symbols,
        "rows": [row_to_dict(row) for row in rows],
    }
    with (state_dir / "tracked_market_scans.jsonl").open("a", encoding="utf-8") as file:
        file.write(json.dumps(payload) + "\n")


def print_result(result: VolumeTrackerResult, detailed_scan_enabled: bool) -> None:
    print(f"\n{result.generated_at} top volume snapshot")
    print(f"{'Rank':>4} {'XYZ':<8} {'24h Vol':>12} {'Price':>12}")
    print("-" * 44)
    for market in result.ranked[:10]:
        price = f"{market.price:.6g}" if market.price is not None else "-"
        print(f"{market.rank:>4} {market.symbol:<8} {usd(market.day_volume_usd):>12} {price:>12}")

    if not result.triggers:
        print("No new rank triggers.")
    else:
        print("Rank triggers:")
        for trigger in result.triggers:
            previous = trigger.previous_rank if trigger.previous_rank is not None else "new"
            print(
                f"- {trigger.symbol}: rank {previous} -> {trigger.current_rank}, "
                f"{usd(trigger.day_volume_usd)}, {trigger.reason}"
            )

    if result.tracked_symbols:
        action = "and scanned" if detailed_scan_enabled else "tracked"
        print(f"Tracked symbols {action}: {', '.join(result.tracked_symbols)}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Watch XYZ volume ranks and track symbols that rise.")
    parser.add_argument("--state-dir", default="data/volume_tracker", help="directory for rank state and logs")
    parser.add_argument("--top", type=int, default=50, help="rank universe to compare and snapshot")
    parser.add_argument("--rank-jump", type=int, default=5, help="minimum upward rank move to trigger tracking")
    parser.add_argument(
        "--min-volume-usd",
        type=Decimal,
        default=Decimal("1000000"),
        help="ignore rank triggers below this 24h volume",
    )
    parser.add_argument("--interval-s", type=float, default=60.0, help="seconds between checks in loop mode")
    parser.add_argument("--once", action="store_true", help="run one check and exit")
    parser.add_argument("--json", action="store_true", help="print machine-readable JSON")
    parser.add_argument(
        "--no-scan-tracked",
        action="store_true",
        help="only update tracked symbol state; do not write detailed venue scans",
    )
    args = parser.parse_args()

    state_dir = Path(args.state_dir)
    tracker = VolumeRankTracker(
        state_dir=state_dir,
        top=args.top,
        rank_jump=args.rank_jump,
        min_volume_usd=args.min_volume_usd,
    )

    while True:
        result = tracker.run_once()
        detailed_scan_enabled = not args.no_scan_tracked
        if detailed_scan_enabled and result.tracked_symbols:
            append_tracked_scan(state_dir, result.generated_at, result.tracked_symbols)

        if args.json:
            print(json.dumps(result_to_dict(result), indent=2))
        else:
            print_result(result, detailed_scan_enabled)

        if args.once:
            return
        time.sleep(args.interval_s)


if __name__ == "__main__":
    main()
