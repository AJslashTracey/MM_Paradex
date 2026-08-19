from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from .hyperliquid import HyperliquidClient, Market


@dataclass(frozen=True)
class RankedMarket:
    rank: int
    symbol: str
    coin: str
    day_volume_usd: Decimal
    price: Decimal | None
    open_interest_usd: Decimal | None


@dataclass(frozen=True)
class RankTrigger:
    symbol: str
    coin: str
    current_rank: int
    previous_rank: int | None
    rank_change: int | None
    day_volume_usd: Decimal
    reason: str


@dataclass(frozen=True)
class VolumeTrackerResult:
    generated_at: str
    ranked: list[RankedMarket]
    triggers: list[RankTrigger]
    tracked_symbols: list[str]


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def decimal_to_json(value: Decimal | None) -> str | None:
    return str(value) if value is not None else None


def ranked_market_to_dict(market: RankedMarket) -> dict[str, Any]:
    return {
        "rank": market.rank,
        "symbol": market.symbol,
        "coin": market.coin,
        "day_volume_usd": decimal_to_json(market.day_volume_usd),
        "price": decimal_to_json(market.price),
        "open_interest_usd": decimal_to_json(market.open_interest_usd),
    }


def rank_trigger_to_dict(trigger: RankTrigger) -> dict[str, Any]:
    return {
        "symbol": trigger.symbol,
        "coin": trigger.coin,
        "current_rank": trigger.current_rank,
        "previous_rank": trigger.previous_rank,
        "rank_change": trigger.rank_change,
        "day_volume_usd": decimal_to_json(trigger.day_volume_usd),
        "reason": trigger.reason,
    }


def market_rankings(markets: list[Market]) -> list[RankedMarket]:
    sorted_markets = sorted(markets, key=lambda market: market.day_ntl_vlm, reverse=True)
    return [
        RankedMarket(
            rank=index,
            symbol=market.symbol.upper(),
            coin=market.coin,
            day_volume_usd=market.day_ntl_vlm,
            price=market.best_price,
            open_interest_usd=market.open_interest_usd,
        )
        for index, market in enumerate(sorted_markets, start=1)
    ]


class VolumeRankTracker:
    def __init__(
        self,
        state_dir: Path,
        top: int = 50,
        rank_jump: int = 5,
        min_volume_usd: Decimal = Decimal("0"),
    ) -> None:
        self.state_dir = state_dir
        self.top = top
        self.rank_jump = rank_jump
        self.min_volume_usd = min_volume_usd
        self.previous_path = state_dir / "previous_volume_ranks.json"
        self.tracked_path = state_dir / "tracked_symbols.json"
        self.snapshots_path = state_dir / "volume_rank_snapshots.jsonl"
        self.events_path = state_dir / "volume_rank_events.jsonl"

    def run_once(self) -> VolumeTrackerResult:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        generated_at = utc_now()
        rankings = market_rankings(HyperliquidClient().xyz_markets())
        ranked_top = rankings[: self.top]
        previous = self.load_previous_ranks()
        tracked = self.load_tracked_symbols()
        triggers = self.find_triggers(ranked_top, previous)

        for trigger in triggers:
            tracked[trigger.symbol] = {
                "symbol": trigger.symbol,
                "coin": trigger.coin,
                "first_tracked_at": tracked.get(trigger.symbol, {}).get(
                    "first_tracked_at", generated_at
                ),
                "last_triggered_at": generated_at,
                "last_reason": trigger.reason,
                "last_rank": trigger.current_rank,
                "last_day_volume_usd": str(trigger.day_volume_usd),
            }

        self.write_snapshot(generated_at, ranked_top)
        if triggers:
            self.write_events(generated_at, triggers)
        self.write_previous_ranks(generated_at, ranked_top)
        self.write_tracked_symbols(tracked)

        return VolumeTrackerResult(
            generated_at=generated_at,
            ranked=ranked_top,
            triggers=triggers,
            tracked_symbols=sorted(tracked),
        )

    def find_triggers(
        self,
        ranked: list[RankedMarket],
        previous: dict[str, dict[str, Any]],
    ) -> list[RankTrigger]:
        if not previous:
            return []

        triggers: list[RankTrigger] = []

        for market in ranked:
            if market.day_volume_usd < self.min_volume_usd:
                continue

            previous_item = previous.get(market.symbol)
            previous_rank = int(previous_item["rank"]) if previous_item else None
            if previous_rank is None:
                reason = f"new in top {self.top}"
                rank_change = None
            else:
                rank_change = previous_rank - market.rank
                if rank_change < self.rank_jump:
                    continue
                reason = f"rank improved by {rank_change}"

            triggers.append(
                RankTrigger(
                    symbol=market.symbol,
                    coin=market.coin,
                    current_rank=market.rank,
                    previous_rank=previous_rank,
                    rank_change=rank_change,
                    day_volume_usd=market.day_volume_usd,
                    reason=reason,
                )
            )

        return triggers

    def load_previous_ranks(self) -> dict[str, dict[str, Any]]:
        if not self.previous_path.exists():
            return {}
        with self.previous_path.open("r", encoding="utf-8") as file:
            payload = json.load(file)
        return {item["symbol"]: item for item in payload.get("ranked", [])}

    def load_tracked_symbols(self) -> dict[str, dict[str, Any]]:
        if not self.tracked_path.exists():
            return {}
        with self.tracked_path.open("r", encoding="utf-8") as file:
            payload = json.load(file)
        return {item["symbol"]: item for item in payload.get("symbols", [])}

    def write_previous_ranks(self, generated_at: str, ranked: list[RankedMarket]) -> None:
        payload = {
            "generated_at": generated_at,
            "ranked": [ranked_market_to_dict(market) for market in ranked],
        }
        self.previous_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    def write_tracked_symbols(self, tracked: dict[str, dict[str, Any]]) -> None:
        payload = {"symbols": [tracked[symbol] for symbol in sorted(tracked)]}
        self.tracked_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    def write_snapshot(self, generated_at: str, ranked: list[RankedMarket]) -> None:
        payload = {
            "generated_at": generated_at,
            "ranked": [ranked_market_to_dict(market) for market in ranked],
        }
        with self.snapshots_path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(payload) + "\n")

    def write_events(self, generated_at: str, triggers: list[RankTrigger]) -> None:
        with self.events_path.open("a", encoding="utf-8") as file:
            for trigger in triggers:
                payload = {"generated_at": generated_at, **rank_trigger_to_dict(trigger)}
                file.write(json.dumps(payload) + "\n")
