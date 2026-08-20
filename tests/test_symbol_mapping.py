from decimal import Decimal
import unittest
from unittest.mock import patch
import urllib.error

from arb_xyz_bot.hyperliquid import normalize_xyz_symbol
from arb_xyz_bot.paradex import ParadexMarket
from arb_xyz_bot.references import references_for_symbol, yahoo_quotes


class SymbolMappingTests(unittest.TestCase):
    def test_normalize_xyz_symbol_strips_prefix(self) -> None:
        self.assertEqual(normalize_xyz_symbol("xyz:TSLA"), "TSLA")
        self.assertEqual(normalize_xyz_symbol("BTC"), "BTC")

    def test_references_include_paradex_match(self) -> None:
        refs = references_for_symbol("BTC", {"ETH"}, {"BTC": "BTC-USD-PERP"})
        self.assertEqual([(ref.venue, ref.symbol) for ref in refs], [("Paradex", "BTC-USD-PERP")])

    def test_references_keep_static_and_hyperliquid_matches(self) -> None:
        refs = references_for_symbol("GOLD", {"GOLD"}, {})
        self.assertEqual(
            [(ref.venue, ref.symbol) for ref in refs],
            [("Yahoo", "GC=F"), ("Hyperliquid", "GOLD")],
        )

    def test_binance_reference_requires_native_crypto_match(self) -> None:
        refs = references_for_symbol("QNT", {"QNT"}, {}, {"QNT": "QNTUSDT"})
        self.assertEqual(
            [(ref.venue, ref.symbol) for ref in refs],
            [("Hyperliquid", "QNT"), ("Binance", "QNTUSDT")],
        )

    def test_binance_reference_skips_non_native_same_ticker(self) -> None:
        refs = references_for_symbol("BB", set(), {}, {"BB": "BBUSDT"})
        self.assertEqual(
            [(ref.venue, ref.symbol) for ref in refs],
            [("Yahoo", "BB")],
        )

    def test_paradex_market_mid_price_prefers_bid_ask(self) -> None:
        market = ParadexMarket(
            symbol="BTC-USD-PERP",
            base_symbol="BTC",
            bid=Decimal("100"),
            ask=Decimal("102"),
            mark_price=Decimal("99"),
        )
        self.assertEqual(market.mid_price, Decimal("101"))

    def test_paradex_market_mid_price_falls_back_to_mark(self) -> None:
        market = ParadexMarket(
            symbol="BTC-USD-PERP",
            base_symbol="BTC",
            bid=None,
            ask=None,
            mark_price=Decimal("99"),
        )
        self.assertEqual(market.mid_price, Decimal("99"))

    @patch("arb_xyz_bot.references.urllib.request.urlopen")
    def test_yahoo_quotes_returns_empty_on_provider_error(self, mock_urlopen) -> None:
        mock_urlopen.side_effect = urllib.error.HTTPError(
            url="https://query1.finance.yahoo.com/v7/finance/quote",
            code=401,
            msg="Unauthorized",
            hdrs=None,
            fp=None,
        )
        self.assertEqual(yahoo_quotes(["TSLA"]), {})


if __name__ == "__main__":
    unittest.main()
