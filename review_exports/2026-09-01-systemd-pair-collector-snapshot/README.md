# 2026-09-01 Systemd Pair Collector Snapshot

Frozen snapshot of the `systemd --user` collect-only streams for:

- `para:UNITREE|xyz:UNITREE`
- `io:SNDK|xyz:SNDK`

Source files at snapshot time:

- `exports/pair_collectors/systemd/para_UNITREE__xyz_UNITREE/market_data.csv`
- `exports/pair_collectors/systemd/io_SNDK__xyz_SNDK/market_data.csv`

Snapshot coverage:

- `para:UNITREE|xyz:UNITREE` through `2026-09-01T06:04:09.723Z`
- `io:SNDK|xyz:SNDK` through `2026-09-01T06:04:09.727Z`

Files:

- `para_UNITREE__xyz_UNITREE_market_data.csv`
- `io_SNDK__xyz_SNDK_market_data.csv`
- `summary.json`

Quick read:

- `para:UNITREE|xyz:UNITREE`: `48606` rows, `5603` clean rows, `3259` clean rows with `best_edge_bps >= 10`
- `io:SNDK|xyz:SNDK`: `48658` rows, `5258` clean rows, `2` clean rows with `best_edge_bps >= 10`

Clean rows are those where:

- `target_book_is_fresh == true`
- `reference_book_is_fresh == true`
- `pair_is_synchronized == true`
