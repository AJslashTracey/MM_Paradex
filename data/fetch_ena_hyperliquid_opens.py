#!/usr/bin/env python3
"""Export the last 24h of ENA 1-minute open prices from Hyperliquid."""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


BASE_URL = "https://api.hyperliquid.xyz"
DEFAULT_COIN = "ENA"
INTERVAL = "1m"
INTERVAL_MS = 60_000
DEFAULT_MINUTES = 1_440
LAST_EXPORT_PATH_FILE = Path(".last_hyperliquid_open_export")


def utc_iso(timestamp_ms: int) -> str:
    return (
        datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def completed_window(now_ms: int, minutes: int) -> tuple[int, int]:
    next_minute_open_ms = (now_ms // INTERVAL_MS) * INTERVAL_MS
    end_time_ms = next_minute_open_ms - 1
    start_time_ms = end_time_ms - (minutes * INTERVAL_MS) + 1
    return start_time_ms, end_time_ms


def post_json(base_url: str, path: str, payload: dict[str, object], timeout: float) -> object:
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "User-Agent": "ena-hyperliquid-open-exporter/1.0",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Hyperliquid API returned HTTP {exc.code}: {body}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Could not reach Hyperliquid API: {exc.reason}") from exc


def fetch_hyperliquid_1m_opens(
    *,
    coin: str = DEFAULT_COIN,
    minutes: int = DEFAULT_MINUTES,
    base_url: str = BASE_URL,
    timeout: float = 15.0,
    now_ms: int | None = None,
) -> list[dict[str, str]]:
    """Return completed 1-minute open prices for the trailing window.

    The current in-progress candle is excluded. Returned rows contain only
    ``open_time_utc`` and ``open``.
    """
    if minutes < 1:
        raise ValueError("minutes must be at least 1")

    start_time_ms, end_time_ms = completed_window(
        int(time.time() * 1000) if now_ms is None else now_ms,
        minutes,
    )

    payload = {
        "type": "candleSnapshot",
        "req": {
            "coin": coin.upper(),
            "interval": INTERVAL,
            "startTime": start_time_ms,
            "endTime": end_time_ms,
        },
    }
    raw_candles = post_json(base_url, "/info", payload, timeout)
    if not isinstance(raw_candles, list):
        raise RuntimeError(f"Unexpected Hyperliquid candle response: {raw_candles!r}")

    candles_by_open_time: dict[int, dict[str, object]] = {}
    for candle in raw_candles:
        if not isinstance(candle, dict) or "t" not in candle or "o" not in candle:
            raise RuntimeError(f"Unexpected Hyperliquid candle row: {candle!r}")
        open_time_ms = int(candle["t"])
        if start_time_ms <= open_time_ms <= end_time_ms:
            candles_by_open_time[open_time_ms] = candle

    ordered_open_times = sorted(candles_by_open_time)
    if len(ordered_open_times) != minutes:
        raise RuntimeError(f"Expected {minutes} candles, received {len(ordered_open_times)}")

    return [
        {
            "open_time_utc": utc_iso(open_time_ms),
            "open": str(candles_by_open_time[open_time_ms]["o"]),
        }
        for open_time_ms in ordered_open_times
    ]


def export_hyperliquid_1m_opens_last_24h(
    output_path: str | Path = "ena_hyperliquid_1m_opens_24h.csv",
    *,
    coin: str = DEFAULT_COIN,
    timeout: float = 15.0,
) -> list[dict[str, str]]:
    rows = fetch_hyperliquid_1m_opens(coin=coin, minutes=DEFAULT_MINUTES, timeout=timeout)
    write_csv(rows, Path(output_path))
    return rows


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
        description="Export the last 24h of completed 1-minute ENA opens from Hyperliquid.",
    )
    parser.add_argument("--coin", default=DEFAULT_COIN, help=f"Hyperliquid coin, default: {DEFAULT_COIN}")
    parser.add_argument("--minutes", type=int, default=DEFAULT_MINUTES, help=f"Candles to fetch, default: {DEFAULT_MINUTES}")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("ena_hyperliquid_1m_opens_24h.csv"),
        help="CSV output path; use '-' for stdout",
    )
    parser.add_argument(
        "--replace-last-export",
        action="store_true",
        help="Replace the last CSV path written by this script, even if a custom --output was used before.",
    )
    parser.add_argument("--base-url", default=BASE_URL, help=f"Hyperliquid API base URL, default: {BASE_URL}")
    parser.add_argument("--timeout", type=float, default=15.0, help="HTTP timeout in seconds")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    output_path = None if str(args.output) == "-" else resolve_output_path(args.output, args.replace_last_export)

    try:
        rows = fetch_hyperliquid_1m_opens(
            coin=args.coin,
            minutes=args.minutes,
            base_url=args.base_url,
            timeout=args.timeout,
        )
        write_csv(rows, output_path)
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if output_path is not None:
        print(f"Wrote {len(rows)} rows to {output_path}")
        print(f"Start: {rows[0]['open_time_utc']} open={rows[0]['open']}")
        print(f"End: {rows[-1]['open_time_utc']} open={rows[-1]['open']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
