#!/usr/bin/env bash
set -euo pipefail

repo_root=$(cd "$(dirname "$0")/.." && pwd)
run_date=${PAIR_COLLECTOR_RUN_DATE:-$(date -u +%F)}
base_dir=${1:-"$repo_root/exports/pair_collectors/$run_date"}
mkdir -p "$base_dir"

start_one() {
  local pair_target=$1
  local slug=$2
  local out_dir="$base_dir/$slug"
  local stdout_log="$out_dir/stdout.log"
  mkdir -p "$out_dir"
  nohup "$repo_root/scripts/run_pair_market_collector.sh" "$pair_target" "$out_dir" \
    >"$stdout_log" 2>&1 &
  local pid=$!
  printf '%s\n' "$pid" >"$out_dir/pid"
  echo "$slug pid=$pid out_dir=$out_dir"
}

start_one "para:UNITREE" "para_UNITREE__xyz_UNITREE"
start_one "io:SNDK" "io_SNDK__xyz_SNDK"
