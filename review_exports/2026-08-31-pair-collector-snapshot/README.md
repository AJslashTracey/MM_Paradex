# 2026-08-31 Pair Collector Snapshot

Frozen snapshot of the collect-only market data streams for:

- `para:UNITREE|xyz:UNITREE`
- `io:SNDK|xyz:SNDK`

Source collector paths at snapshot time:

- `exports/pair_collectors/2026-08-31_live_v2/para_UNITREE__xyz_UNITREE/market_data.csv`
- `exports/pair_collectors/2026-08-31_live_v2/io_SNDK__xyz_SNDK/market_data.csv`

Snapshot coverage:

- generated through `2026-08-31T15:54:31.379Z` for `para:UNITREE|xyz:UNITREE`
- generated through `2026-08-31T15:54:32.539Z` for `io:SNDK|xyz:SNDK`

Files:

- `para_UNITREE__xyz_UNITREE_market_data.csv`
- `io_SNDK__xyz_SNDK_market_data.csv`
- `summary.json`

Quick read:

- `para:UNITREE|xyz:UNITREE` has `6603` rows, `651` clean rows, and `620` clean rows with `best_edge_bps >= 10`
- `io:SNDK|xyz:SNDK` has `6585` rows, `640` clean rows, and `0` clean rows with `best_edge_bps >= 10`

Quality filters used in the exported data interpretation:

- `target_book_is_fresh == true`
- `reference_book_is_fresh == true`
- `pair_is_synchronized == true`

`summary.json` contains the exact row counts, time range, and edge/freshness metrics for the snapshot.
