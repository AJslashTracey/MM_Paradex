# 2026-09-02 OAI/Binance Pair Collector Snapshot

Frozen snapshot of the `systemd --user` collect-only stream for:

- `io:OAI|binance:OPENAIUSDT`

Source files at snapshot time:

- `exports/pair_collectors/systemd/io_OAI__binance_OPENAIUSDT/market_data.csv`
- `exports/pair_collectors/systemd/io_OAI__binance_OPENAIUSDT/collector_events.csv`
- `exports/pair_collectors/systemd/io_OAI__binance_OPENAIUSDT/collector_fills.csv`

Snapshot coverage:

- `io:OAI|binance:OPENAIUSDT` through `2026-09-02T20:50:35.503Z`

Files:

- `io_OAI__binance_OPENAIUSDT_market_data.csv`
- `io_OAI__binance_OPENAIUSDT_collector_events.csv`
- `io_OAI__binance_OPENAIUSDT_collector_fills.csv`
- `summary.json`

Quick read:

- `io:OAI|binance:OPENAIUSDT`: `11365` rows, `476` clean rows, `476` clean rows with `best_edge_bps >= 10`
- Event mix: `476` clean snapshots, `9902` stale snapshots, `987` desynced snapshots
- Freshness: target fresh `20.7655%`, reference fresh `61.5838%`, synchronized `6.7576%`, fully clean `4.1883%`

Clean rows are those where:

- `target_book_is_fresh == true`
- `reference_book_is_fresh == true`
- `pair_is_synchronized == true`
- `event == snapshot`
