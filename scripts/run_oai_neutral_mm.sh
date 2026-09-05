#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -lt 1 ]; then
  echo "usage: $0 OUT_DIR [extra market-maker args...]" >&2
  exit 2
fi

out_dir=$1
shift

repo_root=$(cd "$(dirname "$0")/.." && pwd)
mkdir -p "$out_dir"

extra_args=()
if [ "${OAI_MM_LIVE:-0}" = "1" ]; then
  extra_args+=(--live)
fi
if [ "${OAI_MM_TESTNET:-0}" = "1" ]; then
  extra_args+=(--testnet)
fi
if [ "${OAI_MM_NO_SET_LEVERAGE_ON_START:-0}" = "1" ]; then
  extra_args+=(--no-set-leverage-on-start)
fi
if [ "${OAI_MM_ALLOW_LIVE_CANCEL_ALL_WITHOUT_ACTIVE_ORDERS:-0}" = "1" ]; then
  extra_args+=(--allow-live-cancel-all-without-active-orders)
fi
if [ "${OAI_MM_NO_HALT_ENTRIES_ON_REQUEST_LIMIT:-0}" = "1" ]; then
  extra_args+=(--no-halt-entries-on-request-limit)
fi
if [ "${OAI_MM_NO_HALT_ENTRIES_ON_POSITION_MISMATCH:-0}" = "1" ]; then
  extra_args+=(--no-halt-entries-on-position-mismatch)
fi

cd "$repo_root"
set +u

exec python3 -m execution.oai_mm.main \
  --out-dir "$out_dir" \
  --target-coin "${OAI_MM_TARGET_COIN:-io:OAI}" \
  --binance-symbol "${OAI_MM_BINANCE_SYMBOL:-OPENAIUSDT}" \
  --strategy-mode "${OAI_MM_STRATEGY_MODE:-binance_basis}" \
  --leverage "${OAI_MM_LEVERAGE:-1}" \
  --order-size "${OAI_MM_ORDER_SIZE:-0.01}" \
  --max-order-size "${OAI_MM_MAX_ORDER_SIZE:-0.01}" \
  --max-open-notional "${OAI_MM_MAX_OPEN_NOTIONAL:-50}" \
  --quote-half-spread-bps "${OAI_MM_QUOTE_HALF_SPREAD_BPS:-4}" \
  --requote-threshold-bps "${OAI_MM_REQUOTE_THRESHOLD_BPS:-3}" \
  --force-requote-threshold-bps "${OAI_MM_FORCE_REQUOTE_THRESHOLD_BPS:-4}" \
  --max-quote-top-gap-bps "${OAI_MM_MAX_QUOTE_TOP_GAP_BPS:-2}" \
  --min-quote-lifetime-ms "${OAI_MM_MIN_QUOTE_LIFETIME_MS:-5000}" \
  --basis-ema-period "${OAI_MM_BASIS_EMA_PERIOD:-50}" \
  --max-data-age-ms "${OAI_MM_MAX_DATA_AGE_MS:-4000}" \
  --max-cross-recv-skew-ms "${OAI_MM_MAX_CROSS_RECV_SKEW_MS:-2500}" \
  --feed-unhealthy-cancel-grace-ms "${OAI_MM_FEED_UNHEALTHY_CANCEL_GRACE_MS:-5000}" \
  --max-fair-deviation-bps "${OAI_MM_MAX_FAIR_DEVIATION_BPS:-30}" \
  --soft-inventory-limit "${OAI_MM_SOFT_INVENTORY_LIMIT:-0.02}" \
  --hard-inventory-limit "${OAI_MM_HARD_INVENTORY_LIMIT:-0.04}" \
  --max-inventory-skew-bps "${OAI_MM_MAX_INVENTORY_SKEW_BPS:-6}" \
  --rapid-move-window-s "${OAI_MM_RAPID_MOVE_WINDOW_S:-5}" \
  --rapid-move-threshold-bps "${OAI_MM_RAPID_MOVE_THRESHOLD_BPS:-5}" \
  --recent-move-lookback-ms "${OAI_MM_RECENT_MOVE_LOOKBACK_MS:-2000}" \
  --market-snapshot-interval-ms "${OAI_MM_MARKET_SNAPSHOT_INTERVAL_MS:-500}" \
  --console-status-interval-ms "${OAI_MM_CONSOLE_STATUS_INTERVAL_MS:-5000}" \
  --strategy-loop-interval-ms "${OAI_MM_STRATEGY_LOOP_INTERVAL_MS:-100}" \
  --live-order-action-min-interval-ms "${OAI_MM_LIVE_ORDER_ACTION_MIN_INTERVAL_MS:-15000}" \
  --live-reject-cooldown-ms "${OAI_MM_LIVE_REJECT_COOLDOWN_MS:-120000}" \
  --live-cancel-all-min-interval-ms "${OAI_MM_LIVE_CANCEL_ALL_MIN_INTERVAL_MS:-15000}" \
  --exit-ioc-price-protection-bps "${OAI_MM_EXIT_IOC_PRICE_PROTECTION_BPS:-10}" \
  --flatten-cooldown-ms "${OAI_MM_FLATTEN_COOLDOWN_MS:-30000}" \
  --unresolved-position-alert-interval-ms "${OAI_MM_UNRESOLVED_POSITION_ALERT_INTERVAL_MS:-5000}" \
  --position-reconcile-interval-ms "${OAI_MM_POSITION_RECONCILE_INTERVAL_MS:-30000}" \
  --position-reconcile-tolerance "${OAI_MM_POSITION_RECONCILE_TOLERANCE:-0.000000001}" \
  --deadman-ms "${OAI_MM_DEADMAN_MS:-0}" \
  --deadman-refresh-ms "${OAI_MM_DEADMAN_REFRESH_MS:-5000}" \
  --recv-timeout-s "${OAI_MM_RECV_TIMEOUT_S:-75}" \
  --reconnect-delay-s "${OAI_MM_RECONNECT_DELAY_S:-3}" \
  --ping-interval-s "${OAI_MM_PING_INTERVAL_S:-30}" \
  --markout-windows-s "${OAI_MM_MARKOUT_WINDOWS_S:-1,5,10,30,60}" \
  --binance-depth-stream "${OAI_MM_BINANCE_DEPTH_STREAM:-depth5@100ms}" \
  "${extra_args[@]}" \
  "$@"
