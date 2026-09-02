# 2026-09-02 Systemd Pair Collector Snapshot

Frozen snapshot of the `systemd --user` collect-only streams for:

- `para:UNITREE|xyz:UNITREE`
- `io:SNDK|xyz:SNDK`

Source files at snapshot time:

- `exports/pair_collectors/systemd/para_UNITREE__xyz_UNITREE/market_data.csv`
- `exports/pair_collectors/systemd/io_SNDK__xyz_SNDK/market_data.csv`

Snapshot coverage:

- `para:UNITREE|xyz:UNITREE` through `2026-09-02T17:57:45.105Z`
- `io:SNDK|xyz:SNDK` through `2026-09-02T17:57:45.167Z`

Files:

- `para_UNITREE__xyz_UNITREE_market_data.csv`
- `io_SNDK__xyz_SNDK_market_data.csv`
- `summary.json`

Quick read:

- `para:UNITREE|xyz:UNITREE`: `175529` rows, `19923` clean rows, `10056` clean rows with `best_edge_bps >= 10`
- `io:SNDK|xyz:SNDK`: `175497` rows, `19021` clean rows, `2` clean rows with `best_edge_bps >= 10`

Clean rows are those where:

- `target_book_is_fresh == true`
- `reference_book_is_fresh == true`
- `pair_is_synchronized == true`
