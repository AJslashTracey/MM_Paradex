from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from pathlib import Path


DEFAULT_HL_WS_URL = "wss://api.hyperliquid.xyz/ws"
DEFAULT_BINANCE_WS_BASE = "wss://fstream.binance.com/stream"
DEFAULT_MARKOUT_WINDOWS_S = (1, 5, 10, 30, 60)


@dataclass(frozen=True)
class MMConfig:
    out_dir: Path
    target_coin: str = "io:OAI"
    binance_symbol: str = "OPENAIUSDT"
    strategy_mode: str = "binance_basis"
    live: bool = False
    testnet: bool = False
    set_leverage_on_start: bool = True
    leverage: int = 1
    order_size: float = 0.01
    max_order_size: float = 0.01
    max_open_notional: float = 50.0
    quote_half_spread_bps: float = 4.0
    requote_threshold_bps: float = 1.5
    force_requote_threshold_bps: float = 4.0
    max_quote_top_gap_bps: float = 2.0
    min_quote_lifetime_ms: int = 1_000
    basis_ema_period: int = 50
    max_data_age_ms: int = 1_500
    max_cross_recv_skew_ms: int = 500
    max_fair_deviation_bps: float = 30.0
    soft_inventory_limit: float = 0.02
    hard_inventory_limit: float = 0.04
    max_inventory_skew_bps: float = 6.0
    rapid_move_window_s: float = 5.0
    rapid_move_threshold_bps: float = 5.0
    recent_move_lookback_ms: int = 2_000
    market_snapshot_interval_ms: int = 500
    strategy_loop_interval_ms: int = 100
    deadman_ms: int = 15_000
    deadman_refresh_ms: int = 5_000
    recv_timeout_s: float = 75.0
    reconnect_delay_s: float = 3.0
    ping_interval_s: float = 30.0
    duration_s: float | None = None
    http_timeout: float = 10.0
    markout_windows_s: tuple[int, ...] = field(default_factory=lambda: DEFAULT_MARKOUT_WINDOWS_S)
    paper_fill_on_cross: bool = False
    binance_depth_stream: str = "depth5@100ms"
    hl_ws_url: str = DEFAULT_HL_WS_URL
    binance_ws_base: str = DEFAULT_BINANCE_WS_BASE

    @property
    def event_log(self) -> Path:
        return self.out_dir / "events.csv"

    @property
    def market_log(self) -> Path:
        return self.out_dir / "market.csv"

    @property
    def fill_log(self) -> Path:
        return self.out_dir / "fills.csv"


def parse_windows(raw: str) -> tuple[int, ...]:
    values = []
    for piece in raw.split(","):
        piece = piece.strip()
        if not piece:
            continue
        values.append(int(piece))
    if not values:
        raise ValueError("at least one markout window is required")
    return tuple(values)


def validate_config(config: MMConfig) -> None:
    if config.strategy_mode not in {"binance_basis", "io_mid"}:
        raise ValueError("--strategy-mode must be one of: binance_basis, io_mid")
    if config.order_size <= 0:
        raise ValueError("--order-size must be positive")
    if config.max_order_size <= 0:
        raise ValueError("--max-order-size must be positive")
    if config.order_size > config.max_order_size:
        raise ValueError("--order-size cannot exceed --max-order-size")
    if config.max_open_notional <= 0:
        raise ValueError("--max-open-notional must be positive")
    if config.quote_half_spread_bps <= 0:
        raise ValueError("--quote-half-spread-bps must be positive")
    if config.requote_threshold_bps <= 0:
        raise ValueError("--requote-threshold-bps must be positive")
    if config.force_requote_threshold_bps <= 0:
        raise ValueError("--force-requote-threshold-bps must be positive")
    if config.min_quote_lifetime_ms < 0:
        raise ValueError("--min-quote-lifetime-ms cannot be negative")
    if config.basis_ema_period <= 0:
        raise ValueError("--basis-ema-period must be positive")
    if config.max_data_age_ms <= 0:
        raise ValueError("--max-data-age-ms must be positive")
    if config.max_cross_recv_skew_ms <= 0:
        raise ValueError("--max-cross-recv-skew-ms must be positive")
    if config.max_fair_deviation_bps <= 0:
        raise ValueError("--max-fair-deviation-bps must be positive")
    if config.soft_inventory_limit <= 0:
        raise ValueError("--soft-inventory-limit must be positive")
    if config.hard_inventory_limit <= 0:
        raise ValueError("--hard-inventory-limit must be positive")
    if config.hard_inventory_limit < config.soft_inventory_limit:
        raise ValueError("--hard-inventory-limit must be >= --soft-inventory-limit")
    if config.max_inventory_skew_bps < 0:
        raise ValueError("--max-inventory-skew-bps cannot be negative")
    if config.rapid_move_window_s <= 0:
        raise ValueError("--rapid-move-window-s must be positive")
    if config.rapid_move_threshold_bps <= 0:
        raise ValueError("--rapid-move-threshold-bps must be positive")
    if config.market_snapshot_interval_ms <= 0:
        raise ValueError("--market-snapshot-interval-ms must be positive")
    if config.strategy_loop_interval_ms <= 0:
        raise ValueError("--strategy-loop-interval-ms must be positive")
    if config.deadman_ms <= 0:
        raise ValueError("--deadman-ms must be positive")
    if config.deadman_refresh_ms <= 0:
        raise ValueError("--deadman-refresh-ms must be positive")
    if config.deadman_refresh_ms >= config.deadman_ms:
        raise ValueError("--deadman-refresh-ms must be smaller than --deadman-ms")
    if config.recv_timeout_s <= 0:
        raise ValueError("--recv-timeout-s must be positive")
    if config.reconnect_delay_s <= 0:
        raise ValueError("--reconnect-delay-s must be positive")
    if config.ping_interval_s <= 0:
        raise ValueError("--ping-interval-s must be positive")
    if config.http_timeout <= 0:
        raise ValueError("--http-timeout must be positive")
    if config.live and config.testnet:
        raise ValueError("--live with --testnet is not supported for io:OAI")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Neutral market-maker for Hyperliquid io:OAI using Binance OPENAIUSDT.")
    parser.add_argument("--out-dir", type=Path, required=True, help="Directory for CSV logs and analysis inputs")
    parser.add_argument("--target-coin", default="io:OAI")
    parser.add_argument("--binance-symbol", default="OPENAIUSDT")
    parser.add_argument("--strategy-mode", default="binance_basis", choices=("binance_basis", "io_mid"))
    parser.add_argument("--live", action="store_true", help="Send real Hyperliquid orders")
    parser.add_argument("--testnet", action="store_true", help="Use Hyperliquid testnet through the executor wrapper")
    parser.add_argument(
        "--no-set-leverage-on-start",
        dest="set_leverage_on_start",
        action="store_false",
        help="Do not explicitly set leverage on startup",
    )
    parser.add_argument("--leverage", type=int, default=1)
    parser.add_argument("--order-size", type=float, default=0.01)
    parser.add_argument("--max-order-size", type=float, default=0.01)
    parser.add_argument("--max-open-notional", type=float, default=50.0)
    parser.add_argument("--quote-half-spread-bps", type=float, default=4.0)
    parser.add_argument("--requote-threshold-bps", type=float, default=1.5)
    parser.add_argument("--force-requote-threshold-bps", type=float, default=4.0)
    parser.add_argument("--max-quote-top-gap-bps", type=float, default=2.0)
    parser.add_argument("--min-quote-lifetime-ms", type=int, default=1_000)
    parser.add_argument("--basis-ema-period", type=int, default=50)
    parser.add_argument("--max-data-age-ms", type=int, default=1_500)
    parser.add_argument("--max-cross-recv-skew-ms", type=int, default=500)
    parser.add_argument("--max-fair-deviation-bps", type=float, default=30.0)
    parser.add_argument("--soft-inventory-limit", type=float, default=0.02)
    parser.add_argument("--hard-inventory-limit", type=float, default=0.04)
    parser.add_argument("--max-inventory-skew-bps", type=float, default=6.0)
    parser.add_argument("--rapid-move-window-s", type=float, default=5.0)
    parser.add_argument("--rapid-move-threshold-bps", type=float, default=5.0)
    parser.add_argument("--recent-move-lookback-ms", type=int, default=2_000)
    parser.add_argument("--market-snapshot-interval-ms", type=int, default=500)
    parser.add_argument("--strategy-loop-interval-ms", type=int, default=100)
    parser.add_argument("--deadman-ms", type=int, default=15_000)
    parser.add_argument("--deadman-refresh-ms", type=int, default=5_000)
    parser.add_argument("--recv-timeout-s", type=float, default=75.0)
    parser.add_argument("--reconnect-delay-s", type=float, default=3.0)
    parser.add_argument("--ping-interval-s", type=float, default=30.0)
    parser.add_argument("--duration-s", type=float, default=None)
    parser.add_argument("--http-timeout", type=float, default=10.0)
    parser.add_argument("--markout-windows-s", default="1,5,10,30,60")
    parser.add_argument("--paper-fill-on-cross", action="store_true")
    parser.add_argument("--binance-depth-stream", default="depth5@100ms")
    parser.add_argument("--hl-ws-url", default=DEFAULT_HL_WS_URL)
    parser.add_argument("--binance-ws-base", default=DEFAULT_BINANCE_WS_BASE)
    parser.set_defaults(set_leverage_on_start=True)
    return parser


def config_from_args(args: argparse.Namespace) -> MMConfig:
    config = MMConfig(
        out_dir=args.out_dir,
        target_coin=args.target_coin,
        binance_symbol=args.binance_symbol,
        strategy_mode=args.strategy_mode,
        live=args.live,
        testnet=args.testnet,
        set_leverage_on_start=args.set_leverage_on_start,
        leverage=args.leverage,
        order_size=args.order_size,
        max_order_size=args.max_order_size,
        max_open_notional=args.max_open_notional,
        quote_half_spread_bps=args.quote_half_spread_bps,
        requote_threshold_bps=args.requote_threshold_bps,
        force_requote_threshold_bps=args.force_requote_threshold_bps,
        max_quote_top_gap_bps=args.max_quote_top_gap_bps,
        min_quote_lifetime_ms=args.min_quote_lifetime_ms,
        basis_ema_period=args.basis_ema_period,
        max_data_age_ms=args.max_data_age_ms,
        max_cross_recv_skew_ms=args.max_cross_recv_skew_ms,
        max_fair_deviation_bps=args.max_fair_deviation_bps,
        soft_inventory_limit=args.soft_inventory_limit,
        hard_inventory_limit=args.hard_inventory_limit,
        max_inventory_skew_bps=args.max_inventory_skew_bps,
        rapid_move_window_s=args.rapid_move_window_s,
        rapid_move_threshold_bps=args.rapid_move_threshold_bps,
        recent_move_lookback_ms=args.recent_move_lookback_ms,
        market_snapshot_interval_ms=args.market_snapshot_interval_ms,
        strategy_loop_interval_ms=args.strategy_loop_interval_ms,
        deadman_ms=args.deadman_ms,
        deadman_refresh_ms=args.deadman_refresh_ms,
        recv_timeout_s=args.recv_timeout_s,
        reconnect_delay_s=args.reconnect_delay_s,
        ping_interval_s=args.ping_interval_s,
        duration_s=args.duration_s,
        http_timeout=args.http_timeout,
        markout_windows_s=parse_windows(args.markout_windows_s),
        paper_fill_on_cross=args.paper_fill_on_cross,
        binance_depth_stream=args.binance_depth_stream,
        hl_ws_url=args.hl_ws_url,
        binance_ws_base=args.binance_ws_base,
    )
    validate_config(config)
    return config

