# 2026-09-05 Pair Collector Snapshot

Frozen snapshot of the `systemd --user` collect-only streams for:

- `io:OAI|binance:OPENAIUSDT`
- `io:SNDK|xyz:SNDK`
- `para:UNITREE|xyz:UNITREE`

Window: trailing `24` hours ending at each pair's latest market row.

CSV compression: `zstd`.

Source files at snapshot time:

- `exports/pair_collectors/systemd/io_OAI__binance_OPENAIUSDT/market_data.csv`
- `exports/pair_collectors/systemd/io_OAI__binance_OPENAIUSDT/collector_events.csv`
- `exports/pair_collectors/systemd/io_OAI__binance_OPENAIUSDT/collector_fills.csv`
- `exports/pair_collectors/systemd/io_SNDK__xyz_SNDK/market_data.csv`
- `exports/pair_collectors/systemd/io_SNDK__xyz_SNDK/collector_events.csv`
- `exports/pair_collectors/systemd/io_SNDK__xyz_SNDK/collector_fills.csv`
- `exports/pair_collectors/systemd/para_UNITREE__xyz_UNITREE/market_data.csv`
- `exports/pair_collectors/systemd/para_UNITREE__xyz_UNITREE/collector_events.csv`
- `exports/pair_collectors/systemd/para_UNITREE__xyz_UNITREE/collector_fills.csv`

Snapshot coverage:

- `io:OAI|binance:OPENAIUSDT` through `2026-09-05T09:40:45.574Z`
- `io:SNDK|xyz:SNDK` through `2026-09-05T09:40:48.666Z`
- `para:UNITREE|xyz:UNITREE` through `2026-09-05T09:40:51.783Z`

Files:

- `io_OAI__binance_OPENAIUSDT_market_data.csv.zst`
- `io_OAI__binance_OPENAIUSDT_collector_events.csv.zst`
- `io_OAI__binance_OPENAIUSDT_collector_fills.csv.zst`
- `io_SNDK__xyz_SNDK_market_data.csv.zst`
- `io_SNDK__xyz_SNDK_collector_events.csv.zst`
- `io_SNDK__xyz_SNDK_collector_fills.csv.zst`
- `para_UNITREE__xyz_UNITREE_market_data.csv.zst`
- `para_UNITREE__xyz_UNITREE_collector_events.csv.zst`
- `para_UNITREE__xyz_UNITREE_collector_fills.csv.zst`
- `summary.json`

Quick read:

- `io:OAI|binance:OPENAIUSDT`: `171497` rows, `6545` clean rows, `6545` clean rows with `best_edge_bps >= 10`
- `io:SNDK|xyz:SNDK`: `85231` rows, `9695` clean rows, `9` clean rows with `best_edge_bps >= 10`
- `para:UNITREE|xyz:UNITREE`: `85149` rows, `9790` clean rows, `9773` clean rows with `best_edge_bps >= 10`

Clean rows are those where:

- `target_book_is_fresh == true`
- `reference_book_is_fresh == true`
- `pair_is_synchronized == true`
