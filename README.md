# TradeXYZ Arbitrage Scanner

Small Python tooling for monitoring trade[XYZ] markets, comparing them to
reference venues, and tracking live cross-venue price dislocations.

This repo is not an execution bot. It does not place orders. Execution still
needs wallet signing, account setup, venue-specific order APIs, risk controls,
position sizing, and kill switches.

## Scanner

The scanner:

- pulls live XYZ HIP-3 perp markets from Hyperliquid
- ranks markets by 24h notional volume
- maps each XYZ symbol to likely reference markets
- compares XYZ pricing against Yahoo, native Hyperliquid perps, Paradex, and Binance
- emits JSON or a compact terminal table

No third-party packages are required.

Scan the highest-volume XYZ markets:

```bash
python3 scripts/scan_xyz.py --top 20
```

Scan the stock watchlist:

```bash
python3 scripts/scan_xyz.py --stocks --top 50
```

Scan explicit symbols:

```bash
python3 scripts/scan_xyz.py --symbols SNDK,NVDA,AAPL,MSFT,TSLA,AMD,MU,TSM,COIN,PLTR,HOOD,CRCL
```

JSON output for downstream strategy code:

```bash
python3 scripts/scan_xyz.py --stocks --json
```

Only show gaps above a threshold:

```bash
python3 scripts/scan_xyz.py --stocks --min-edge-bps 25
```

## Volume Rank Tracking

Run one volume-rank check and create the baseline:

```bash
python3 scripts/watch_volume_ranks.py --once
```

Run continuously every 60 seconds:

```bash
python3 scripts/watch_volume_ranks.py --interval-s 60
```

Trigger tracking when a market improves by at least 3 ranks inside the top 50:

```bash
python3 scripts/watch_volume_ranks.py --rank-jump 3 --top 50
```

The watcher writes local state and history under `data/volume_tracker/`:

- `previous_volume_ranks.json`: latest rank snapshot used for comparison
- `volume_rank_snapshots.jsonl`: every top-N rank snapshot
- `volume_rank_events.jsonl`: symbols that newly enter the top-N or rise by the configured rank threshold
- `tracked_symbols.json`: symbols currently being tracked
- `tracked_market_scans.jsonl`: detailed scanner output for tracked symbols

The first run establishes a baseline and should not trigger every symbol. Later
runs promote a symbol into `tracked_symbols.json` when it newly appears in the
top-N set or its volume rank improves by at least `--rank-jump`.

## Live Fair-Price Tracker

The repo also includes a live websocket-based tracker that:

- discovers overlapping Hyperliquid `xyz` and Paradex markets at startup
- keeps top-of-book quotes and full books in memory
- computes a cross-venue fair mid as the median of valid mids
- detects rich-venue deviations relative to that fair price
- estimates whether the deviation is executable by walking live depth
- writes append-only Parquet datasets for `quotes`, `depth`, and `deviations`

Run it locally:

```bash
python3 scripts/run_fairprice_tracker.py --base-dir /tmp/fairprice-data
```

Optional symbol override:

```bash
python3 scripts/run_fairprice_tracker.py --base-dir /tmp/fairprice-data --symbols CL,MU,SPCX
```

The initial live overlap universe is discovered from REST and currently tracks
the symbols that exist on both Hyperliquid `xyz` and Paradex. Quote updates are
stored continuously. Depth snapshots are only stored while a deviation event is
active.

### Deviation Definition

The tracker uses three related notions:

- `fair_mid`: median of fresh, valid venue mids
- `quote_deviation_bps`: `(venue_mid - fair_mid) / fair_mid * 10000`
- `executable_edge_bps`: live sell-rich / buy-cheap edge after walking books

An event opens when all of these are true:

- quote deviation is at least `ARB_FAIRPRICE_MIN_QUOTE_DEVIATION_BPS`
- at least two valid venues contribute to `fair_mid`
- live books exist for the rich venue and at least one hedge venue

The event then records whether it was actually exploitable using:

- `ARB_FAIRPRICE_MIN_EXECUTABLE_EDGE_BPS`
- `ARB_FAIRPRICE_MIN_MATCHED_NOTIONAL_USD`

### Data Layout

The tracker writes Parquet files under `ARB_FAIRPRICE_BASE_DIR`:

- `quotes/date=YYYY-MM-DD/hour=HH/*.parquet`
- `depth/date=YYYY-MM-DD/hour=HH/*.parquet`
- `deviations/date=YYYY-MM-DD/hour=HH/*.parquet`

### Systemd Deployment

Deployment artifacts are in:

- `deploy/systemd/arb-fairprice-live.service`
- `deploy/env/arb-fairprice.env.example`
- `scripts/install_fairprice_service.sh`

The intended server layout is:

- `/home/aj/deploy-box/arb-fairprice/current`
- `/home/aj/deploy-box/arb-fairprice/shared/.env`
- `/home/aj/deploy-box/arb-fairprice/shared/data`

## Data Sources

- `POST https://api.hyperliquid.xyz/info` with `{"type":"metaAndAssetCtxs","dex":"xyz"}` for XYZ markets
- `POST https://api.hyperliquid.xyz/info` with `{"type":"metaAndAssetCtxs"}` for native Hyperliquid perp references
- Yahoo Finance quote endpoints for external reference prices
- Paradex `GET /v1/markets/summary?market=ALL` for public perp mid prices
- Binance USD-M `GET /fapi/v1/exchangeInfo` and `GET /fapi/v1/ticker/bookTicker`

## Important Caveat

These comparisons are indicative only. Futures, ETFs, indexes, ADRs, and
perpetuals are not automatically fungible with XYZ contracts, so a price gap is
not automatically executable arbitrage. Add contract-specific conversion logic,
execution checks, and risk controls before trading.
