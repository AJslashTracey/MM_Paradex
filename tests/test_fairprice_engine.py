from decimal import Decimal
import unittest

from arb_xyz_bot.fairprice_engine import (
    BookLevel,
    VenueBook,
    bps_difference,
    depth_summary,
    median_decimal,
    simulate_sell_route,
)


class FairPriceEngineTests(unittest.TestCase):
    def test_median_decimal_even_count(self) -> None:
        self.assertEqual(
            median_decimal([Decimal("99"), Decimal("101")]),
            Decimal("100"),
        )

    def test_depth_summary_buckets(self) -> None:
        book = VenueBook(
            symbol="CL",
            venue="hyperliquid_xyz",
            bids=(
                BookLevel("hyperliquid_xyz", Decimal("100"), Decimal("2")),
                BookLevel("hyperliquid_xyz", Decimal("99.95"), Decimal("3")),
                BookLevel("hyperliquid_xyz", Decimal("99"), Decimal("10")),
            ),
            asks=(
                BookLevel("hyperliquid_xyz", Decimal("100.05"), Decimal("4")),
                BookLevel("hyperliquid_xyz", Decimal("100.10"), Decimal("5")),
                BookLevel("hyperliquid_xyz", Decimal("101"), Decimal("10")),
            ),
            exchange_ts_ms=1,
            received_ts_ns=1,
        )
        summary = depth_summary(book, (Decimal("10"),))
        self.assertEqual(summary.bid_buckets["10bps"].base_size, Decimal("5"))
        self.assertEqual(summary.ask_buckets["10bps"].base_size, Decimal("9"))

    def test_simulate_sell_route_walks_depth(self) -> None:
        source_book = VenueBook(
            symbol="CL",
            venue="hyperliquid_xyz",
            bids=(
                BookLevel("hyperliquid_xyz", Decimal("100.10"), Decimal("2")),
                BookLevel("hyperliquid_xyz", Decimal("100.05"), Decimal("3")),
            ),
            asks=(),
            exchange_ts_ms=1,
            received_ts_ns=1,
        )
        hedge_book = VenueBook(
            symbol="CL",
            venue="paradex",
            bids=(),
            asks=(
                BookLevel("paradex", Decimal("99.90"), Decimal("1")),
                BookLevel("paradex", Decimal("99.95"), Decimal("6")),
            ),
            exchange_ts_ms=1,
            received_ts_ns=1,
        )
        route = simulate_sell_route(
            source_venue="hyperliquid_xyz",
            source_book=source_book,
            hedge_books=[hedge_book],
            fair_mid=Decimal("100"),
            fee_bps_by_venue={"hyperliquid_xyz": Decimal("1"), "paradex": Decimal("1")},
            min_fill_edge_bps=Decimal("5"),
        )
        self.assertIsNotNone(route)
        assert route is not None
        self.assertEqual(route.hedge_venues, ("paradex",))
        self.assertEqual(route.matched_base_size, Decimal("5"))
        self.assertEqual(route.source_fill_notional_usd, Decimal("500.35"))
        self.assertEqual(route.hedge_fill_notional_usd, Decimal("499.70"))
        self.assertEqual(route.top_net_edge_bps, Decimal("18"))

    def test_bps_difference(self) -> None:
        self.assertEqual(
            bps_difference(Decimal("100.1"), Decimal("99.9"), Decimal("100")),
            Decimal("20"),
        )


if __name__ == "__main__":
    unittest.main()
