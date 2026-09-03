#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import math
import re
import shutil
import statistics
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


EDGE_THRESHOLDS = (1, 2, 5, 10, 20, 30, 50, 100)


@dataclass(frozen=True)
class PairPaths:
    source_dir: Path
    market_data: Path
    collector_events: Path | None
    collector_fills: Path | None
    base_name: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create a frozen export snapshot from one or more pair collector directories."
    )
    parser.add_argument(
        "--pair-dir",
        action="append",
        required=True,
        help="Collector directory that contains market_data.csv and optional collector_events.csv / collector_fills.csv",
    )
    parser.add_argument(
        "--export-root",
        type=Path,
        default=Path("review_exports"),
        help="Directory that receives the generated export folder",
    )
    parser.add_argument(
        "--export-name",
        default=None,
        help="Optional export folder name; defaults to a dated snapshot name",
    )
    return parser.parse_args()


def truthy(value: str | None) -> bool:
    return (value or "").strip().lower() == "true"


def parse_float(value: str | None) -> float | None:
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    return float(text)


def parse_int(value: str | None) -> int | None:
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    return int(text)


def round_stat(value: float | int | None, digits: int = 6) -> float | int | None:
    if value is None:
        return None
    return round(value, digits)


def percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    ordered = sorted(values)
    raw_index = (len(ordered) - 1) * pct
    lower = math.floor(raw_index)
    upper = math.ceil(raw_index)
    if lower == upper:
        return ordered[lower]
    fraction = raw_index - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def slugify_pair(pair_id: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", pair_id.lower()).strip("-")


def display_path(path: Path, cwd: Path) -> str:
    try:
        return str(path.resolve().relative_to(cwd.resolve()))
    except ValueError:
        return str(path.resolve())


def load_pair_paths(path_str: str) -> PairPaths:
    source_dir = Path(path_str).resolve()
    market_data = source_dir / "market_data.csv"
    if not market_data.is_file():
        raise FileNotFoundError(f"missing market_data.csv in {source_dir}")
    collector_events = source_dir / "collector_events.csv"
    collector_fills = source_dir / "collector_fills.csv"
    return PairPaths(
        source_dir=source_dir,
        market_data=market_data,
        collector_events=collector_events if collector_events.is_file() else None,
        collector_fills=collector_fills if collector_fills.is_file() else None,
        base_name=source_dir.name,
    )


def count_csv_rows(path: Path | None) -> int:
    if path is None or not path.is_file():
        return 0
    with path.open(newline="") as handle:
        reader = csv.reader(handle)
        next(reader, None)
        return sum(1 for _ in reader)


def summarize_market_data(path: Path) -> dict[str, Any]:
    rows = 0
    pair_id: str | None = None
    first_time_utc: str | None = None
    last_time_utc: str | None = None
    snapshot_rows = 0
    snapshot_stale_rows = 0
    snapshot_desynced_rows = 0
    target_fresh_rows = 0
    reference_fresh_rows = 0
    sync_rows = 0
    clean_rows = 0
    best_edges: list[float] = []
    clean_best_edges: list[float] = []
    target_book_ages: list[int] = []
    reference_book_ages: list[int] = []
    cross_recv_skews: list[int] = []
    recent_rows: deque[tuple[bool, float | None]] = deque(maxlen=200)

    with path.open(newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            rows += 1
            pair_id = pair_id or row.get("pair")
            time_utc = row.get("time_utc") or None
            if first_time_utc is None:
                first_time_utc = time_utc
            last_time_utc = time_utc or last_time_utc

            event = (row.get("event") or "").strip()
            is_target_fresh = truthy(row.get("target_book_is_fresh"))
            is_reference_fresh = truthy(row.get("reference_book_is_fresh"))
            is_synchronized = truthy(row.get("pair_is_synchronized"))
            is_clean = event == "snapshot" and is_target_fresh and is_reference_fresh and is_synchronized

            if event == "snapshot":
                snapshot_rows += 1
            elif event == "snapshot_stale":
                snapshot_stale_rows += 1
            elif event == "snapshot_desynced":
                snapshot_desynced_rows += 1

            if is_target_fresh:
                target_fresh_rows += 1
            if is_reference_fresh:
                reference_fresh_rows += 1
            if is_synchronized:
                sync_rows += 1
            if is_clean:
                clean_rows += 1

            best_edge_bps = parse_float(row.get("best_edge_bps"))
            if best_edge_bps is not None:
                best_edges.append(best_edge_bps)
                if is_clean:
                    clean_best_edges.append(best_edge_bps)

            target_book_age_ms = parse_int(row.get("target_book_age_ms"))
            if target_book_age_ms is not None:
                target_book_ages.append(target_book_age_ms)
            reference_book_age_ms = parse_int(row.get("reference_book_age_ms"))
            if reference_book_age_ms is not None:
                reference_book_ages.append(reference_book_age_ms)
            cross_recv_skew_ms = parse_int(row.get("cross_recv_skew_ms"))
            if cross_recv_skew_ms is not None:
                cross_recv_skews.append(cross_recv_skew_ms)

            recent_rows.append((is_clean, best_edge_bps if is_clean else None))

    if rows == 0:
        raise ValueError(f"market data is empty: {path}")
    if pair_id is None:
        raise ValueError(f"market data is missing pair values: {path}")

    recent_clean_edges = [edge for is_clean, edge in recent_rows if is_clean and edge is not None]
    recent_clean_count = sum(1 for is_clean, _ in recent_rows if is_clean)

    summary: dict[str, Any] = {
        "rows": rows,
        "first_time_utc": first_time_utc,
        "last_time_utc": last_time_utc,
        "snapshot_rows": snapshot_rows,
        "snapshot_stale_rows": snapshot_stale_rows,
        "snapshot_desynced_rows": snapshot_desynced_rows,
        "target_fresh_rows": target_fresh_rows,
        "reference_fresh_rows": reference_fresh_rows,
        "sync_rows": sync_rows,
        "clean_rows": clean_rows,
        "target_fresh_pct": round(target_fresh_rows * 100.0 / rows, 4),
        "reference_fresh_pct": round(reference_fresh_rows * 100.0 / rows, 4),
        "sync_pct": round(sync_rows * 100.0 / rows, 4),
        "clean_pct": round(clean_rows * 100.0 / rows, 4),
        "all_edge_mean_bps": round_stat(statistics.fmean(best_edges) if best_edges else None),
        "all_edge_median_bps": round_stat(statistics.median(best_edges) if best_edges else None),
        "all_edge_abs_p95_bps": round_stat(percentile([abs(value) for value in best_edges], 0.95)),
        "all_edge_max_bps": round_stat(max(best_edges) if best_edges else None),
        "all_edge_min_bps": round_stat(min(best_edges) if best_edges else None),
        "clean_edge_mean_bps": round_stat(statistics.fmean(clean_best_edges) if clean_best_edges else None),
        "clean_edge_median_bps": round_stat(statistics.median(clean_best_edges) if clean_best_edges else None),
        "clean_edge_abs_p95_bps": round_stat(percentile([abs(value) for value in clean_best_edges], 0.95)),
        "clean_edge_max_bps": round_stat(max(clean_best_edges) if clean_best_edges else None),
        "clean_edge_min_bps": round_stat(min(clean_best_edges) if clean_best_edges else None),
        "target_book_age_median_ms": round_stat(statistics.median(target_book_ages) if target_book_ages else None),
        "reference_book_age_median_ms": round_stat(
            statistics.median(reference_book_ages) if reference_book_ages else None
        ),
        "cross_recv_skew_median_ms": round_stat(statistics.median(cross_recv_skews) if cross_recv_skews else None),
        "recent_200_clean_pct": round(recent_clean_count * 100.0 / len(recent_rows), 4) if recent_rows else 0.0,
        "recent_200_clean_edge_mean_bps": round_stat(
            statistics.fmean(recent_clean_edges) if recent_clean_edges else None
        ),
        "recent_200_clean_edge_median_bps": round_stat(
            statistics.median(recent_clean_edges) if recent_clean_edges else None
        ),
    }
    for threshold in EDGE_THRESHOLDS:
        summary[f"clean_edge_ge_{threshold}bps"] = sum(
            1 for value in clean_best_edges if abs(value) >= float(threshold)
        )
    return {"pair_id": pair_id, "summary": summary}


def default_export_name(pair_ids: list[str]) -> str:
    date_prefix = datetime.now().astimezone().date().isoformat()
    if len(pair_ids) == 1:
        return f"{date_prefix}-{slugify_pair(pair_ids[0])}-snapshot"
    return f"{date_prefix}-pair-collector-snapshot"


def copy_file(source: Path, destination: Path) -> None:
    shutil.copy2(source, destination)


def build_readme(
    export_name: str,
    pair_exports: list[dict[str, Any]],
    cwd: Path,
) -> str:
    pair_ids = [item["pair_id"] for item in pair_exports]
    lines: list[str] = []
    title_prefix = export_name.split("-", 3)[:3]
    date_label = "-".join(title_prefix) if len(title_prefix) >= 3 else export_name
    if len(pair_exports) == 1:
        title = f"# {date_label} Pair Collector Snapshot"
        stream_label = "stream"
    else:
        title = f"# {date_label} Pair Collector Snapshot"
        stream_label = "streams"
    lines.append(title)
    lines.append("")
    lines.append(f"Frozen snapshot of the `systemd --user` collect-only {stream_label} for:")
    lines.append("")
    for pair_id in pair_ids:
        lines.append(f"- `{pair_id}`")
    lines.append("")
    lines.append("Source files at snapshot time:")
    lines.append("")
    for item in pair_exports:
        lines.append(f"- `{display_path(item['source_market'], cwd)}`")
        if item["source_events"] is not None:
            lines.append(f"- `{display_path(item['source_events'], cwd)}`")
        if item["source_fills"] is not None:
            lines.append(f"- `{display_path(item['source_fills'], cwd)}`")
    lines.append("")
    lines.append("Snapshot coverage:")
    lines.append("")
    for item in pair_exports:
        lines.append(f"- `{item['pair_id']}` through `{item['summary']['last_time_utc']}`")
    lines.append("")
    lines.append("Files:")
    lines.append("")
    for item in pair_exports:
        lines.append(f"- `{item['market_file']}`")
        if item["events_file"] is not None:
            lines.append(f"- `{item['events_file']}`")
        if item["fills_file"] is not None:
            lines.append(f"- `{item['fills_file']}`")
    lines.append("- `summary.json`")
    lines.append("")
    lines.append("Quick read:")
    lines.append("")
    for item in pair_exports:
        pair_summary = item["summary"]
        lines.append(
            f"- `{item['pair_id']}`: `{pair_summary['rows']}` rows, `{pair_summary['clean_rows']}` clean rows, "
            f"`{pair_summary['clean_edge_ge_10bps']}` clean rows with `best_edge_bps >= 10`"
        )
    if len(pair_exports) == 1:
        pair_summary = pair_exports[0]["summary"]
        lines.append(
            f"- Event mix: `{pair_summary['snapshot_rows']}` clean snapshots, "
            f"`{pair_summary['snapshot_stale_rows']}` stale snapshots, "
            f"`{pair_summary['snapshot_desynced_rows']}` desynced snapshots"
        )
        lines.append(
            f"- Freshness: target fresh `{pair_summary['target_fresh_pct']}%`, "
            f"reference fresh `{pair_summary['reference_fresh_pct']}%`, "
            f"synchronized `{pair_summary['sync_pct']}%`, fully clean `{pair_summary['clean_pct']}%`"
        )
    lines.append("")
    lines.append("Clean rows are those where:")
    lines.append("")
    lines.append("- `target_book_is_fresh == true`")
    lines.append("- `reference_book_is_fresh == true`")
    lines.append("- `pair_is_synchronized == true`")
    if len(pair_exports) == 1:
        lines.append("- `event == snapshot`")
    return "\n".join(lines) + "\n"


def export_snapshot(pair_dirs: list[str], export_root: Path, export_name: str | None = None) -> Path:
    cwd = Path.cwd()
    pair_paths = [load_pair_paths(path_str) for path_str in pair_dirs]
    pair_ids: list[str] = []
    for paths in pair_paths:
        summarized = summarize_market_data(paths.market_data)
        pair_ids.append(summarized["pair_id"])

    resolved_export_name = export_name or default_export_name(pair_ids)
    export_dir = (export_root if export_root.is_absolute() else cwd / export_root) / resolved_export_name
    export_dir.mkdir(parents=True, exist_ok=False)

    pair_exports: list[dict[str, Any]] = []
    for paths in pair_paths:
        market_file = f"{paths.base_name}_market_data.csv"
        events_file = f"{paths.base_name}_collector_events.csv" if paths.collector_events is not None else None
        fills_file = f"{paths.base_name}_collector_fills.csv" if paths.collector_fills is not None else None

        copied_market = export_dir / market_file
        copy_file(paths.market_data, copied_market)
        copied_events = None
        copied_fills = None
        if paths.collector_events is not None and events_file is not None:
            copied_events = export_dir / events_file
            copy_file(paths.collector_events, copied_events)
        if paths.collector_fills is not None and fills_file is not None:
            copied_fills = export_dir / fills_file
            copy_file(paths.collector_fills, copied_fills)

        summarized = summarize_market_data(copied_market)
        pair_summary = summarized["summary"]
        pair_summary["collector_event_rows"] = count_csv_rows(copied_events)
        pair_summary["collector_fill_rows"] = count_csv_rows(copied_fills)

        pair_export: dict[str, Any] = {
            "pair_id": summarized["pair_id"],
            "source_market": paths.market_data,
            "source_events": paths.collector_events,
            "source_fills": paths.collector_fills,
            "market_file": market_file,
            "events_file": events_file,
            "fills_file": fills_file,
            "summary": pair_summary,
        }
        pair_exports.append(pair_export)

    generated_utc = max(item["summary"]["last_time_utc"] for item in pair_exports)
    summary_json: dict[str, Any] = {
        "export_name": resolved_export_name,
        "generated_utc": generated_utc,
        "pairs": {},
    }
    for item in pair_exports:
        pair_summary = dict(item["summary"])
        pair_summary["file"] = item["market_file"]
        if item["events_file"] is not None:
            pair_summary["events_file"] = item["events_file"]
        if item["fills_file"] is not None:
            pair_summary["fills_file"] = item["fills_file"]
        ordered_summary: dict[str, Any] = {"file": pair_summary.pop("file")}
        if "events_file" in pair_summary:
            ordered_summary["events_file"] = pair_summary.pop("events_file")
        if "fills_file" in pair_summary:
            ordered_summary["fills_file"] = pair_summary.pop("fills_file")
        ordered_summary.update(pair_summary)
        summary_json["pairs"][item["pair_id"]] = ordered_summary

    readme_text = build_readme(resolved_export_name, pair_exports, cwd)
    (export_dir / "README.md").write_text(readme_text)
    (export_dir / "summary.json").write_text(json.dumps(summary_json, indent=2) + "\n")
    return export_dir


def main() -> int:
    args = parse_args()
    export_dir = export_snapshot(args.pair_dir, args.export_root, args.export_name)
    print(export_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
