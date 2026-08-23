# Regime dependent Market Making 

My plan for this algo right here is to profit from quoting around an external fair price, with a regime dependent inventory managment system. 


Data needed:
Hyperliquid: Data:BBO; timestamp, bid/ask size; purpose: realtime extermal fair value as basis 

Fairprice v1: F=(HLbid+HLask)/2 

Paradex: 

Data: BBO: bid/ask + size, => determine available spread and quote placement 

Market metadata  tick size, lot size minimum order, Generate orders 

Trades: price, size, side timestamp => measure activity and possible fill conditions 

Book updates: sequence number and timestamp => detect gaps or stale books

Funding: current rate => inventory cost

For now I think L1 data is enough 


Account data
Open order: Id, side, price, size, remaining size =>  order reconcilliation 


## HIP-3 lag/oracle watcher

Run the default RWA lag monitor:

```bash
python3 scripts/watch_hip3_lag.py
```

It tracks these default pairs:

- `para:UNITREE` vs `xyz:UNITREE`
- `io:SNDK` vs `xyz:SNDK`
- `mkts:US500` vs `xyz:SP500` with `0.1` reference scale
- `para:AAOI`, `para:AVGO`, `para:CRWD`, `para:IREN`, `para:RDDT`, `para:NET` vs matching `xyz` markets

When a deviation crosses threshold it appends order-book snapshots to
`data/hip3_lag_order_books.csv`. Each row includes top of book, full bid/ask
levels from Hyperliquid `l2Book`, book age, mark/oracle/funding context, and
the trigger metrics.

Useful examples:

```bash
# Lower the trigger thresholds.
python3 scripts/watch_hip3_lag.py --edge-bps 10 --mid-deviation-bps 50 --oracle-deviation-bps 50

# Timed capture around a catalyst.
python3 scripts/watch_hip3_lag.py --duration-s 1800 --output data/unitree_event_books.csv

# Add a custom pair. The last value is an optional scale applied to the reference.
python3 scripts/watch_hip3_lag.py --pair para:UNITREE=xyz:UNITREE --pair mkts:US500,xyz:SP500,0.1
```
