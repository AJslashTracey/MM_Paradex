# Pair Collector

Current scope is collecting and later trading a small pair set with explicit
freshness tracking.

Active pairs:

- `para:UNITREE` vs `xyz:UNITREE`
- `io:SNDK` vs `xyz:SNDK`

Main runtime:

- `execution/unitree_lag_bot.py`

Collector helpers:

- `scripts/run_pair_market_collector.sh`
- `scripts/start_pair_collectors.sh`

Useful commands:

```bash
# Run one collect-only stream into a dedicated output directory.
./scripts/run_pair_market_collector.sh 'para:UNITREE' exports/pair_collectors/manual/para_UNITREE__xyz_UNITREE

# Start both pair collectors.
./scripts/start_pair_collectors.sh
```

The collector writes per-snapshot:

- raw top-of-book prices and sizes
- per-leg book timestamps and receive timestamps
- per-leg book age and receive lag
- cross-venue receive skew
- raw long/short/best edge values
- explicit freshness and synchronization flags
