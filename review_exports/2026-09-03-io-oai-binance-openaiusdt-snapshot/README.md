# 2026-09-03 Pair Collector Snapshot

Frozen snapshot of the `systemd --user` collect-only stream for:

- `io:OAI|binance:OPENAIUSDT`

Source files at snapshot time:

- `exports/pair_collectors/systemd/io_OAI__binance_OPENAIUSDT/market_data.csv`
- `exports/pair_collectors/systemd/io_OAI__binance_OPENAIUSDT/collector_events.csv`
- `exports/pair_collectors/systemd/io_OAI__binance_OPENAIUSDT/collector_fills.csv`

Snapshot coverage:

- `io:OAI|binance:OPENAIUSDT` through `2026-09-03T09:52:51.868Z`

Files:

- `io_OAI__binance_OPENAIUSDT_market_data.csv`
- `io_OAI__binance_OPENAIUSDT_collector_events.csv`
- `io_OAI__binance_OPENAIUSDT_collector_fills.csv`
- `summary.json`

Quick read:

- `io:OAI|binance:OPENAIUSDT`: `104498` rows, `4359` clean rows, `4359` clean rows with `best_edge_bps >= 10`
- Event mix: `4359` clean snapshots, `91668` stale snapshots, `8471` desynced snapshots
- Freshness: target fresh `20.7066%`, reference fresh `59.6021%`, synchronized `6.5398%`, fully clean `4.1714%`

Clean rows are those where:

- `target_book_is_fresh == true`
- `reference_book_is_fresh == true`
- `pair_is_synchronized == true`
- `event == snapshot`
