#!/usr/bin/env python3
"""Analyze HIP-3 lag watcher captures and open Plotly figures in a browser."""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots


DEFAULT_INPUT = Path("exports/hip3_lag/2026-08-24/hip3_lag_order_books.csv")


@dataclass
class TradabilityConfig:
    min_net_edge_bps: float
    min_top_notional: float


def parse_levels(raw: Any) -> list[dict[str, float]]:
    if not isinstance(raw, str) or not raw:
        return []
    try:
        return [
            {
                "px": float(level["px"]),
                "sz": float(level["sz"]),
                "n": float(level.get("n", 0)),
            }
            for level in json.loads(raw)
        ]
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return []


def load_pairs(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    df["ts"] = pd.to_datetime(df["snapshot_time_utc"], utc=True)
    target = df[df["role"] == "target"].copy()
    reference = df[df["role"] == "reference"].copy()

    keep = ["recv_time_ms", "pair", "bid_px", "bid_sz", "ask_px", "ask_sz", "mid_px", "bid_levels_json", "ask_levels_json"]
    pairs = target.merge(
        reference[keep],
        on=["recv_time_ms", "pair"],
        how="inner",
        suffixes=("_target", "_reference"),
    )

    pairs["reference_bid_scaled"] = pairs["bid_px_reference"] * pairs["reference_scale"]
    pairs["reference_ask_scaled"] = pairs["ask_px_reference"] * pairs["reference_scale"]
    pairs["reference_mid_scaled"] = pairs["mid_px_reference"] * pairs["reference_scale"]

    pairs["top_long_edge_bps"] = (
        (pairs["reference_bid_scaled"] - pairs["ask_px_target"])
        / pairs["reference_mid_scaled"]
        * 10_000
    )
    pairs["top_short_edge_bps"] = (
        (pairs["bid_px_target"] - pairs["reference_ask_scaled"])
        / pairs["reference_mid_scaled"]
        * 10_000
    )
    pairs["top_long_notional"] = (
        pd.concat(
            [
                pairs["ask_px_target"] * pairs["ask_sz_target"],
                pairs["reference_bid_scaled"] * pairs["bid_sz_reference"],
            ],
            axis=1,
        )
        .min(axis=1)
        .fillna(0.0)
    )
    pairs["top_short_notional"] = (
        pd.concat(
            [
                pairs["bid_px_target"] * pairs["bid_sz_target"],
                pairs["reference_ask_scaled"] * pairs["ask_sz_reference"],
            ],
            axis=1,
        )
        .min(axis=1)
        .fillna(0.0)
    )
    return pairs


def add_tradability(pairs: pd.DataFrame, config: TradabilityConfig) -> pd.DataFrame:
    pairs = pairs.copy()
    pairs["net_long_edge_bps"] = pairs["top_long_edge_bps"] - config.min_net_edge_bps
    pairs["net_short_edge_bps"] = pairs["top_short_edge_bps"] - config.min_net_edge_bps
    pairs["long_tradable"] = (pairs["net_long_edge_bps"] > 0) & (pairs["top_long_notional"] >= config.min_top_notional)
    pairs["short_tradable"] = (pairs["net_short_edge_bps"] > 0) & (pairs["top_short_notional"] >= config.min_top_notional)
    pairs["best_net_edge_bps"] = pairs[["net_long_edge_bps", "net_short_edge_bps"]].max(axis=1)
    pairs["best_side"] = pairs.apply(
        lambda row: "long_target" if row["net_long_edge_bps"] >= row["net_short_edge_bps"] else "short_target",
        axis=1,
    )
    pairs["tradable"] = pairs["long_tradable"] | pairs["short_tradable"]
    return pairs


def summarize(pairs: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for pair, group in pairs.groupby("pair", sort=True):
        elapsed = (group["ts"].max() - group["ts"].min()).total_seconds()
        tradable = group[group["tradable"]]
        long_tradable = group[group["long_tradable"]]
        short_tradable = group[group["short_tradable"]]
        rows.append(
            {
                "pair": pair,
                "snapshots": len(group),
                "window_s": elapsed,
                "max_long_edge_bps": group["top_long_edge_bps"].max(),
                "max_short_edge_bps": group["top_short_edge_bps"].max(),
                "max_net_long_edge_bps": group["net_long_edge_bps"].max(),
                "max_net_short_edge_bps": group["net_short_edge_bps"].max(),
                "max_abs_mid_dev_bps": group["deviation_bps"].abs().max(),
                "max_abs_oracle_dev_bps": group["target_oracle_vs_fair_bps"].abs().max(),
                "tradable_snapshots": len(tradable),
                "long_tradable_snapshots": len(long_tradable),
                "short_tradable_snapshots": len(short_tradable),
                "tradable_window_s": 0.0 if tradable.empty else (tradable["ts"].max() - tradable["ts"].min()).total_seconds(),
                "median_tradable_notional": 0.0
                if tradable.empty
                else pd.concat(
                    [
                        long_tradable["top_long_notional"],
                        short_tradable["top_short_notional"],
                    ]
                ).median(),
                "max_top_long_notional": group.loc[group["top_long_edge_bps"].idxmax(), "top_long_notional"],
                "max_top_short_notional": group.loc[group["top_short_edge_bps"].idxmax(), "top_short_notional"],
            }
        )
    return pd.DataFrame(rows).sort_values(["tradable_snapshots", "max_long_edge_bps", "max_short_edge_bps"], ascending=False)


def make_edge_figure(pairs: pd.DataFrame) -> go.Figure:
    fig = make_subplots(
        rows=2,
        cols=1,
        shared_xaxes=True,
        vertical_spacing=0.08,
        subplot_titles=("Executable top-of-book edge", "Mid/oracle deviation"),
    )
    for pair, group in pairs.groupby("pair", sort=True):
        fig.add_trace(
            go.Scatter(
                x=group["ts"],
                y=group["top_long_edge_bps"],
                mode="lines",
                name=f"{pair} long",
                legendgroup=pair,
            ),
            row=1,
            col=1,
        )
        fig.add_trace(
            go.Scatter(
                x=group["ts"],
                y=group["top_short_edge_bps"],
                mode="lines",
                name=f"{pair} short",
                legendgroup=pair,
                visible="legendonly",
            ),
            row=1,
            col=1,
        )
        fig.add_trace(
            go.Scatter(
                x=group["ts"],
                y=group["deviation_bps"],
                mode="lines",
                name=f"{pair} mid dev",
                legendgroup=pair,
                visible="legendonly",
            ),
            row=2,
            col=1,
        )
        fig.add_trace(
            go.Scatter(
                x=group["ts"],
                y=group["target_oracle_vs_fair_bps"],
                mode="lines",
                name=f"{pair} oracle dev",
                legendgroup=pair,
            ),
            row=2,
            col=1,
        )
    fig.add_hline(y=0, line_width=1, line_color="gray", row=1, col=1)
    fig.add_hline(y=0, line_width=1, line_color="gray", row=2, col=1)
    fig.update_layout(
        title="HIP-3 Lag Capture: Edges And Deviations",
        xaxis2_title="Time",
        yaxis_title="bps",
        yaxis2_title="bps",
        hovermode="x unified",
        height=850,
    )
    return fig


def make_tradability_figure(pairs: pd.DataFrame) -> go.Figure:
    summary_rows = summarize(pairs)
    fig = make_subplots(
        rows=1,
        cols=2,
        subplot_titles=("Max executable edge by pair", "Tradable snapshot count"),
        horizontal_spacing=0.18,
    )
    fig.add_trace(
        go.Bar(
            x=summary_rows["pair"],
            y=summary_rows[["max_long_edge_bps", "max_short_edge_bps"]].max(axis=1),
            name="max edge bps",
            marker_color="#2374ab",
        ),
        row=1,
        col=1,
    )
    fig.add_trace(
        go.Bar(
            x=summary_rows["pair"],
            y=summary_rows["tradable_snapshots"],
            name="tradable snapshots",
            marker_color="#f26430",
        ),
        row=1,
        col=2,
    )
    fig.update_layout(
        title="HIP-3 Lag Capture: Tradability Summary",
        height=650,
        showlegend=False,
    )
    fig.update_xaxes(tickangle=45)
    fig.update_yaxes(title_text="bps", row=1, col=1)
    fig.update_yaxes(title_text="snapshots", row=1, col=2)
    return fig


def make_pair_focus_figure(pairs: pd.DataFrame, pair: str) -> go.Figure:
    group = pairs[pairs["pair"] == pair].sort_values("ts")
    if group.empty:
        raise ValueError(f"pair not found: {pair}")

    fig = make_subplots(
        rows=3,
        cols=1,
        shared_xaxes=True,
        vertical_spacing=0.07,
        subplot_titles=(f"{pair}: target/reference prices", "Executable edge", "Top-of-book executable notional"),
    )
    fig.add_trace(go.Scatter(x=group["ts"], y=group["mid_px_target"], mode="lines", name="target mid"), row=1, col=1)
    fig.add_trace(go.Scatter(x=group["ts"], y=group["reference_mid_scaled"], mode="lines", name="reference mid scaled"), row=1, col=1)
    fig.add_trace(go.Scatter(x=group["ts"], y=group["ctx_oracle_px"], mode="lines", name="target oracle"), row=1, col=1)
    fig.add_trace(go.Scatter(x=group["ts"], y=group["top_long_edge_bps"], mode="lines", name="long target edge bps"), row=2, col=1)
    fig.add_trace(go.Scatter(x=group["ts"], y=group["top_short_edge_bps"], mode="lines", name="short target edge bps"), row=2, col=1)
    fig.add_trace(go.Scatter(x=group["ts"], y=group["top_long_notional"], mode="lines", name="long top notional"), row=3, col=1)
    fig.add_trace(go.Scatter(x=group["ts"], y=group["top_short_notional"], mode="lines", name="short top notional"), row=3, col=1)
    fig.add_hline(y=0, line_width=1, line_color="gray", row=2, col=1)
    fig.update_layout(title=f"HIP-3 Lag Focus: {pair}", height=900, hovermode="x unified")
    fig.update_yaxes(title_text="price", row=1, col=1)
    fig.update_yaxes(title_text="bps", row=2, col=1)
    fig.update_yaxes(title_text="notional", row=3, col=1)
    return fig


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Analyze HIP-3 lag CSV and open Plotly browser figures.")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT, help=f"Capture CSV path, default: {DEFAULT_INPUT}")
    parser.add_argument("--min-net-edge-bps", type=float, default=10.0, help="Cost/slippage buffer for tradability, default: 10 bps")
    parser.add_argument("--min-top-notional", type=float, default=50.0, help="Minimum top-of-book executable notional, default: 50")
    parser.add_argument("--focus-pair", default=None, help="Specific pair for detailed focus plot; default picks best tradable pair")
    parser.add_argument("--no-show", action="store_true", help="Print summary without opening browser figures")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    pairs = add_tradability(load_pairs(args.input), TradabilityConfig(args.min_net_edge_bps, args.min_top_notional))
    summary_rows = summarize(pairs)

    print(f"Input: {args.input}")
    print(f"Rows: {len(pairs):,} paired snapshots")
    print(f"Window: {pairs['ts'].min()} -> {pairs['ts'].max()}")
    print(f"Tradability config: net edge > {args.min_net_edge_bps} bps, top notional >= {args.min_top_notional}")
    print()
    print(summary_rows.to_string(index=False, float_format=lambda value: f"{value:,.2f}"))

    if args.no_show:
        return 0

    focus_pair = args.focus_pair
    if focus_pair is None:
        tradable_summary = summary_rows[summary_rows["tradable_snapshots"] > 0]
        focus_pair = (
            tradable_summary.iloc[0]["pair"]
            if not tradable_summary.empty
            else summary_rows.iloc[0]["pair"]
        )

    make_edge_figure(pairs).show()
    make_tradability_figure(pairs).show()
    make_pair_focus_figure(pairs, focus_pair).show()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
