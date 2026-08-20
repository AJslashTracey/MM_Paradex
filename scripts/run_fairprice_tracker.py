#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from arb_xyz_bot.live_tracker import TrackerConfig, run_tracker


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the live fair-price deviation tracker.")
    parser.add_argument(
        "--base-dir",
        type=Path,
        help="override ARB_FAIRPRICE_BASE_DIR for local runs",
    )
    parser.add_argument(
        "--symbols",
        help="comma-separated symbol override, e.g. CL,MU,SPCX",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        help="python logging level",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    config = TrackerConfig.from_env()
    if args.base_dir is not None:
        config = TrackerConfig(
            **{**config.__dict__, "base_dir": args.base_dir},
        )
    if args.symbols:
        config = TrackerConfig(
            **{
                **config.__dict__,
                "symbols": tuple(symbol.strip().upper() for symbol in args.symbols.split(",") if symbol.strip()),
            },
        )
    asyncio.run(run_tracker(config))


if __name__ == "__main__":
    main()
