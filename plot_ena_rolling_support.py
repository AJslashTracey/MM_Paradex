#!/usr/bin/env python3
"""Fetch ENAUSDT 1m OHLC data and compute a rolling support trendline."""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import numpy as np


BASE_URL = "https://api.binance.com"
DEFAULT_SYMBOL = "ENAUSDT"
INTERVAL = "1m"
INTERVAL_MS = 60_000
DEFAULT_MINUTES = 1_440
DEFAULT_LOOKBACK = 240
BINANCE_KLINES_LIMIT = 1_000


def utc_iso(timestamp_ms: int) -> str:
    return (
        datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def get_json(base_url: str, path: str, params: dict[str, int | str], timeout: float) -> object:
    query = urllib.parse.urlencode(params)
    url = f"{base_url.rstrip('/')}{path}"
    if query:
        url = f"{url}?{query}"

    request = urllib.request.Request(
        url,
        headers={"User-Agent": "ena-rolling-support-fetcher/1.0"},
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Binance API returned HTTP {exc.code}: {body}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Could not reach Binance API: {exc.reason}") from exc


def get_binance_server_time_ms(base_url: str, timeout: float) -> int:
    payload = get_json(base_url, "/api/v3/time", {}, timeout)
    if not isinstance(payload, dict) or "serverTime" not in payload:
        raise RuntimeError(f"Unexpected Binance server-time response: {payload!r}")
    return int(payload["serverTime"])


def completed_window(now_ms: int, minutes: int) -> tuple[int, int]:
    next_minute_open_ms = (now_ms // INTERVAL_MS) * INTERVAL_MS
    end_time_ms = next_minute_open_ms - 1
    start_time_ms = end_time_ms - (minutes * INTERVAL_MS) + 1
    return start_time_ms, end_time_ms


def fetch_klines(
    *,
    base_url: str,
    symbol: str,
    start_time_ms: int,
    end_time_ms: int,
    limit: int,
    timeout: float,
) -> list[list[object]]:
    payload = get_json(
        base_url,
        "/api/v3/klines",
        {
            "symbol": symbol,
            "interval": INTERVAL,
            "startTime": start_time_ms,
            "endTime": end_time_ms,
            "limit": limit,
        },
        timeout,
    )
    if not isinstance(payload, list):
        raise RuntimeError(f"Unexpected Binance klines response: {payload!r}")
    return payload


def fetch_ohlc_rows(
    *,
    base_url: str,
    symbol: str,
    minutes: int,
    timeout: float,
    use_local_clock: bool,
) -> list[dict[str, float | str]]:
    if minutes < 1:
        raise ValueError("minutes must be at least 1")

    now_ms = int(time.time() * 1000) if use_local_clock else get_binance_server_time_ms(base_url, timeout)
    start_time_ms, end_time_ms = completed_window(now_ms, minutes)

    klines: list[list[object]] = []
    next_start_ms = start_time_ms

    while next_start_ms <= end_time_ms:
        remaining = ((end_time_ms - next_start_ms) // INTERVAL_MS) + 1
        limit = min(BINANCE_KLINES_LIMIT, remaining)
        batch_end_ms = min(end_time_ms, next_start_ms + (limit * INTERVAL_MS) - 1)
        batch = fetch_klines(
            base_url=base_url,
            symbol=symbol,
            start_time_ms=next_start_ms,
            end_time_ms=batch_end_ms,
            limit=limit,
            timeout=timeout,
        )
        if not batch:
            raise RuntimeError(f"Binance returned no klines after {utc_iso(next_start_ms)}")

        klines.extend(batch)
        last_open_time_ms = int(batch[-1][0])
        next_start_ms = last_open_time_ms + INTERVAL_MS

    unique_by_open_time = {int(kline[0]): kline for kline in klines}
    ordered_klines = [unique_by_open_time[key] for key in sorted(unique_by_open_time)]

    if len(ordered_klines) != minutes:
        raise RuntimeError(f"Expected {minutes} candles, received {len(ordered_klines)}")

    return [
        {
            "open_time_utc": utc_iso(int(kline[0])),
            "open": float(kline[1]),
            "high": float(kline[2]),
            "low": float(kline[3]),
            "close": float(kline[4]),
        }
        for kline in ordered_klines
    ]


def fit_trendlines_high_low(
    high: np.ndarray,
    low: np.ndarray,
    close: np.ndarray,
) -> tuple[tuple[float, float], tuple[float, float]]:
    x = np.arange(len(close))
    log_high = np.log(np.asarray(high, dtype=float))
    log_low = np.log(np.asarray(low, dtype=float))
    log_close = np.log(np.asarray(close, dtype=float))

    slope, close_intercept = np.polyfit(x, log_close, 1)
    support_intercept = float(np.min(log_low - slope * x))
    resistance_intercept = float(np.max(log_high - slope * x))

    return (float(slope), support_intercept), (float(slope), resistance_intercept)


def add_rolling_support(
    rows: list[dict[str, float | str]],
    lookback: int,
) -> None:
    if lookback < 2:
        raise ValueError("lookback must be at least 2")
    if lookback >= len(rows):
        raise ValueError("lookback must be smaller than the number of rows")

    support_slope_values = np.full(len(rows), np.nan)
    support_values = np.full(len(rows), np.nan)

    highs = np.array([float(row["high"]) for row in rows])
    lows = np.array([float(row["low"]) for row in rows])
    closes = np.array([float(row["close"]) for row in rows])

    for i in range(lookback, len(rows)):
        window = slice(i - lookback, i)

        support_coefs, _resist_coefs = fit_trendlines_high_low(
            highs[window],
            lows[window],
            closes[window],
        )

        support_slope, support_intercept = support_coefs

        current_log_support = support_slope * lookback + support_intercept
        current_support = float(np.exp(current_log_support))

        support_slope_values[i] = support_slope
        support_values[i] = current_support

    for i, row in enumerate(rows):
        row["support_slope"] = support_slope_values[i]
        row["support"] = support_values[i]


def write_csv(rows: list[dict[str, float | str]], output_path: Path | None) -> None:
    fieldnames = [
        "open_time_utc",
        "open",
        "high",
        "low",
        "close",
        "support_slope",
        "support",
    ]

    output_file = sys.stdout if output_path is None else output_path.open("w", newline="", encoding="utf-8")
    try:
        writer = csv.DictWriter(output_file, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    key: "" if isinstance(row[key], float) and np.isnan(row[key]) else row[key]
                    for key in fieldnames
                }
            )
    finally:
        if output_path is not None:
            output_file.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fetch ENAUSDT 1m candles and compute rolling support using prior candles only.",
    )
    parser.add_argument("--symbol", default=DEFAULT_SYMBOL, help=f"Binance symbol, default: {DEFAULT_SYMBOL}")
    parser.add_argument("--minutes", type=int, default=DEFAULT_MINUTES, help=f"Candles to fetch, default: {DEFAULT_MINUTES}")
    parser.add_argument("--lookback", type=int, default=DEFAULT_LOOKBACK, help=f"Rolling lookback, default: {DEFAULT_LOOKBACK}")
    parser.add_argument("--output", type=Path, default=Path("ena_1m_rolling_support.csv"), help="CSV output path; use '-' for stdout")
    parser.add_argument("--base-url", default=BASE_URL, help=f"Binance API base URL, default: {BASE_URL}")
    parser.add_argument("--timeout", type=float, default=15.0, help="HTTP timeout in seconds")
    parser.add_argument(
        "--use-local-clock",
        action="store_true",
        help="Use this machine's clock instead of Binance server time to choose the completed window.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    output_path = None if str(args.output) == "-" else args.output

    try:
        rows = fetch_ohlc_rows(
            base_url=args.base_url,
            symbol=args.symbol.upper(),
            minutes=args.minutes,
            timeout=args.timeout,
            use_local_clock=args.use_local_clock,
        )
        add_rolling_support(rows, args.lookback)
        write_csv(rows, output_path)
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if output_path is not None:
        first_support = next((row for row in rows if not np.isnan(float(row["support"]))), None)
        print(f"Wrote {len(rows)} rows to {output_path}")
        if first_support is not None:
            print(f"First support row: {first_support['open_time_utc']} {first_support['support']:.8f}")
            print(f"Last support row: {rows[-1]['open_time_utc']} {rows[-1]['support']:.8f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
