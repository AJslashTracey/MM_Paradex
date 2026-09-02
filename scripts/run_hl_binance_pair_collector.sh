#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -lt 3 ]; then
  echo "usage: $0 HYPERLIQUID_TARGET BINANCE_SYMBOL OUT_DIR [extra collector args...]" >&2
  exit 2
fi

target_coin=$1
binance_symbol=$2
out_dir=$3
shift 3

repo_root=$(cd "$(dirname "$0")/.." && pwd)
mkdir -p "$out_dir"

cd "$repo_root"

exec python3 -m execution.hl_binance_pair_collector \
  --target-coin "$target_coin" \
  --binance-symbol "$binance_symbol" \
  --log "$out_dir/collector_events.csv" \
  --market-log "$out_dir/market_data.csv" \
  --fill-log "$out_dir/collector_fills.csv" \
  --market-log-interval-ms "${PAIR_MARKET_LOG_INTERVAL_MS:-500}" \
  --max-book-age-ms "${PAIR_MAX_BOOK_AGE_MS:-750}" \
  --max-cross-recv-skew-ms "${PAIR_MAX_CROSS_RECV_SKEW_MS:-250}" \
  "$@"
