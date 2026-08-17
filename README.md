# TradeXYZ Arbitrage Scanner

Small Python scanner for finding high-volume trade[XYZ] markets and comparing them
against likely external reference markets.

This is a scanner, not an execution bot. It does not place orders. It is intended
as the data and signal layer you can build execution around after adding wallet
signing, venue accounts, position limits, and kill switches.

## What It Does

- Pulls live XYZ HIP-3 perp markets from Hyperliquid.
- Ranks markets by 24h notional volume.
- Maps each XYZ symbol to likely alternate markets:
  - Yahoo Finance equities, ETFs, futures, FX, and index symbols.
  - Native Hyperliquid perp symbols where the same crypto symbol exists.
- Computes simple premium/discount versus each reference price.
- Emits JSON or a compact terminal table.

## Install

No third-party packages are required.

```bash
python3 scripts/scan_xyz.py --top 20
```

JSON output:

```bash
python3 scripts/scan_xyz.py --top 20 --json
```

Only show possible arb gaps above a threshold:

```bash
python3 scripts/scan_xyz.py --top 50 --min-edge-bps 25
```

## Notes

The scanner uses:

- `POST https://api.hyperliquid.xyz/info` with `{"type":"metaAndAssetCtxs","dex":"xyz"}` for XYZ markets.
- Yahoo Finance quote endpoints for external reference prices.
- Native Hyperliquid `metaAndAssetCtxs` for crypto perp references.

The comparison is indicative only. Futures, ETFs, indexes, and ADRs may not be
perfectly fungible with XYZ contracts, so a price gap is not automatically an
executable arbitrage. Add contract-specific conversion logic before trading.

