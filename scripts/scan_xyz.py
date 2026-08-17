#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from arb_xyz_bot.scanner import row_to_dict, scan


def usd(value: Decimal | None) -> str:
    if value is None:
        return "-"
    if value >= Decimal("1000000"):
        return f"${value / Decimal('1000000'):.2f}M"
    if value >= Decimal("1000"):
        return f"${value / Decimal('1000'):.2f}K"
    return f"${value:.2f}"


def main() -> None:
    parser = argparse.ArgumentParser(description="Scan trade[XYZ] markets for reference-market gaps.")
    parser.add_argument("--top", type=int, default=20, help="number of highest-volume XYZ markets to scan")
    parser.add_argument(
        "--min-edge-bps",
        type=Decimal,
        default=Decimal("0"),
        help="only print comparisons with an absolute gap at least this many bps",
    )
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    args = parser.parse_args()

    rows = scan(top=args.top, min_edge_bps=args.min_edge_bps)

    if args.json:
        print(json.dumps([row_to_dict(row) for row in rows], indent=2))
        return

    print(f"{'XYZ':<10} {'Price':>12} {'24h Vol':>12} {'OI est':>12}  References")
    print("-" * 96)
    for row in rows:
        market = row.market
        refs = []
        for comparison in row.comparisons:
            refs.append(
                f"{comparison.reference.venue}:{comparison.reference.symbol} "
                f"{comparison.edge_bps:+.1f}bps"
            )
        ref_text = "; ".join(refs) if refs else "-"
        price = f"{market.best_price:.6g}" if market.best_price is not None else "-"
        print(
            f"{market.symbol:<10} {price:>12} "
            f"{usd(market.day_ntl_vlm):>12} {usd(market.open_interest_usd):>12}  {ref_text}"
        )


if __name__ == "__main__":
    main()
