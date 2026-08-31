#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -lt 2 ]; then
  echo "usage: $0 PAIR_TARGET OUT_DIR [extra unitree_lag_bot args...]" >&2
  exit 2
fi

pair_target=$1
out_dir=$2
shift 2

repo_root=$(cd "$(dirname "$0")/.." && pwd)
mkdir -p "$out_dir"

exec python3 "$repo_root/execution/unitree_lag_bot.py" \
  --collect-only \
  --pair-target "$pair_target" \
  --log "$out_dir/collector_events.csv" \
  --market-log "$out_dir/market_data.csv" \
  --fill-log "$out_dir/collector_fills.csv" \
  --market-log-interval-ms "${PAIR_MARKET_LOG_INTERVAL_MS:-500}" \
  --max-book-age-ms "${PAIR_MAX_BOOK_AGE_MS:-750}" \
  --max-cross-recv-skew-ms "${PAIR_MAX_CROSS_RECV_SKEW_MS:-250}" \
  "$@"
