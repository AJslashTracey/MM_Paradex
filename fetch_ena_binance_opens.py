#!/usr/bin/env python3
"""Fetch the last 1440 completed 1-minute ENA open prices from Binance Spot."""

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


BASE_URL = "https://api.binance.com"
DEFAULT_SYMBOL = "ENAUSDT"
INTERVAL = "1m"
INTERVAL_MS = 60_000
DEFAULT_MINUTES = 1_440
BINANCE_KLINES_LIMIT = 1_000
LAST_EXPORT_PATH_FILE = Path(".last_binance_open_export")


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
        headers={"User-Agent": "ena-binance-open-fetcher/1.0"},
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


def completed_window(now_ms: int, minutes: int) -> tuple[int, int]:
    next_minute_open_ms = (now_ms // INTERVAL_MS) * INTERVAL_MS
    end_time_ms = next_minute_open_ms - 1
    start_time_ms = end_time_ms - (minutes * INTERVAL_MS) + 1
    return start_time_ms, end_time_ms


def fetch_open_rows(
    *,
    base_url: str,
    symbol: str,
    minutes: int,
    timeout: float,
    use_local_clock: bool,
) -> list[dict[str, str]]:
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
            "open": str(kline[1]),
        }
        for kline in ordered_klines
    ]


def remember_export_path(output_path: Path) -> None:
    LAST_EXPORT_PATH_FILE.write_text(str(output_path.resolve()) + "\n", encoding="utf-8")


def last_export_path() -> Path | None:
    if not LAST_EXPORT_PATH_FILE.exists():
        return None
    path = LAST_EXPORT_PATH_FILE.read_text(encoding="utf-8").strip()
    return Path(path) if path else None


def resolve_output_path(output_path: Path, replace_last_export: bool) -> Path:
    if replace_last_export:
        return last_export_path() or output_path
    return output_path


def write_csv(rows: list[dict[str, str]], output_path: Path | None) -> None:
    fieldnames = ["open_time_utc", "open"]
    if output_path is None:
        writer = csv.DictWriter(sys.stdout, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
        return

    output_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = output_path.with_name(f".{output_path.name}.tmp")
    with temp_path.open("w", newline="", encoding="utf-8") as output_file:
        writer = csv.DictWriter(output_file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    temp_path.replace(output_path)
    remember_export_path(output_path)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fetch the last completed 1-minute ENA open prices from Binance Spot.",
    )
    parser.add_argument("--symbol", default=DEFAULT_SYMBOL, help=f"Binance symbol, default: {DEFAULT_SYMBOL}")
    parser.add_argument("--minutes", type=int, default=DEFAULT_MINUTES, help=f"Candles to fetch, default: {DEFAULT_MINUTES}")
    parser.add_argument("--output", type=Path, default=Path("ena_1m_opens.csv"), help="CSV output path; use '-' for stdout")
    parser.add_argument(
        "--replace-last-export",
        action="store_true",
        help="Replace the last CSV path written by this script, even if a custom --output was used before.",
    )
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
    output_path = None if str(args.output) == "-" else resolve_output_path(args.output, args.replace_last_export)

    try:
        rows = fetch_open_rows(
            base_url=args.base_url,
            symbol=args.symbol.upper(),
            minutes=args.minutes,
            timeout=args.timeout,
            use_local_clock=args.use_local_clock,
        )
        write_csv(rows, output_path)
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if output_path is not None:
        print(f"Wrote {len(rows)} rows to {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
