# 2026-09-05 Pair Collector Snapshot

Frozen snapshot of the `systemd --user` collect-only stream for:

- `io:OAI|binance:OPENAIUSDT`

Window: trailing `24` hours ending at each pair's latest market row.

CSV compression: `zstd`.

Source files at snapshot time:

- `exports/pair_collectors/systemd/io_OAI__binance_OPENAIUSDT/market_data.csv`
- `exports/pair_collectors/systemd/io_OAI__binance_OPENAIUSDT/collector_events.csv`
- `exports/pair_collectors/systemd/io_OAI__binance_OPENAIUSDT/collector_fills.csv`

Snapshot coverage:

- `io:OAI|binance:OPENAIUSDT` through `2026-09-05T14:53:40.512Z`

Files:

- `io_OAI__binance_OPENAIUSDT_market_data.csv.zst`
- `io_OAI__binance_OPENAIUSDT_collector_events.csv.zst`
- `io_OAI__binance_OPENAIUSDT_collector_fills.csv.zst`
- `summary.json`

Quick read:

- `io:OAI|binance:OPENAIUSDT`: `171475` rows, `7225` clean rows, `7225` clean rows with `best_edge_bps >= 10`
- Event mix: `7225` clean snapshots, `150516` stale snapshots, `13734` desynced snapshots
- Freshness: target fresh `20.4176%`, reference fresh `59.3258%`, synchronized `6.9794%`, fully clean `4.2134%`

Clean rows are those where:

- `target_book_is_fresh == true`
- `reference_book_is_fresh == true`
- `pair_is_synchronized == true`
- `event == snapshot`
