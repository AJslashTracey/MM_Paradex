# TradeXYZ Arbitrage Scanner

Small Python scanner for finding high-volume trade[XYZ] stock perp markets and
checking where the same symbols are available on related venues.

This is a scanner, not an execution bot. It does not place orders. Execution
needs wallet signing, account setup, venue-specific order APIs, risk limits,
position sizing, and kill switches.

## Venues

| Venue | Structure | Scanner treatment |
| --- | --- | --- |
| Trade[XYZ] / Hyperliquid HIP-3 | CLOB, HyperCore, 24/7 | Primary market source, ranked by 24h volume |
| Variational Omni | RFQ, not an order book | Public metadata and indicative RFQ quotes |
| Ostium | Oracle/execution-network model | Public Builder API bid/mid/ask and session state |
| Paradex | Roadmap | Marked as not currently tradable for equity perps |

## Usage

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

Only show venue gaps above a threshold:

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

- `previous_volume_ranks.json`: latest rank snapshot used for comparison.
- `volume_rank_snapshots.jsonl`: every top-N rank snapshot.
- `volume_rank_events.jsonl`: symbols that newly enter the top-N or rise by the configured rank threshold.
- `tracked_symbols.json`: symbols currently being tracked.
- `tracked_market_scans.jsonl`: detailed venue scans for tracked symbols.

The first run establishes a baseline and should not trigger every symbol. Later
runs promote a symbol into `tracked_symbols.json` when it newly appears in the
top-N set or its volume rank improves by at least `--rank-jump`.

## Data Sources

- `POST https://api.hyperliquid.xyz/info` with `{"type":"metaAndAssetCtxs","dex":"xyz"}` for Trade[XYZ].
- `GET https://omni-client-api.prod.ap-northeast-1.variational.io/metadata/stats` for Variational Omni public market statistics and indicative quotes.
- `GET https://builder.prod.bedrock.ostium.io/v1/prices` for Ostium public prices.
- Paradex docs currently list Equity Perps as upcoming, so the scanner records it as roadmap-only.

## Important Caveat

A gap is not automatically executable arbitrage. Variational is RFQ, Ostium is
not a CLOB, and stock perps can have session, funding, oracle, and contract
differences. Treat this as an opportunity discovery layer, then add venue-specific
execution and risk checks before trading.
