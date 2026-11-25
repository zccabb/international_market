import tempfile
import unittest
from pathlib import Path

from market import create_app
from providers import normalize_symbol, parse_chart


class MarketTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        url = "sqlite:///" + (Path(self.tmp.name) / "quotes.db").as_posix()
        self.app = create_app(url, seed=False)
        self.store = self.app.extensions["store"]
        self.client = self.app.test_client()

    def tearDown(self):
        self.store.engine.dispose()
        self.tmp.cleanup()

    def test_watchlist_and_deleted_fetch(self):
        payload = {"market": "HK", "symbol": "00700"}
        r = self.client.post("/api/watchlist", json=payload)
        self.assertEqual(r.status_code, 201)
        key = r.json["id"]
        self.assertEqual(self.client.post("/api/watchlist", json={"market": "HK", "symbol": "700.HK"}).status_code, 409)
        self.assertEqual(self.client.delete(f"/api/watchlist/{key}").status_code, 204)
        new_key = self.client.post("/api/watchlist", json=payload).json["id"]
        self.assertNotEqual(key, new_key)
        self.store.record(key, {"price": 20, "asof": 200, "source": "test"}, [], [])
        self.assertIsNone(self.client.get("/api/watchlist").json[0]["price"])

    def test_history_upsert_and_quote_order(self):
        key = self.client.post("/api/watchlist", json={"market": "US", "symbol": "aapl"}).json["id"]
        candle = dict(interval="1m", stamp=120, open=12, high=13, low=10, close=12, volume=None)
        self.store.record(key, {"price": 13, "asof": 180, "source": "first"}, [candle], [])
        self.store.record(key, {"price": 9, "asof": 120, "source": "older"}, [dict(candle, close=11)], ["日线暂不可用"])
        self.assertEqual(self.client.get("/api/watchlist").json[0]["price"], 13)
        bars = self.client.get(f"/api/history/{key}?interval=1m").json
        self.assertEqual([bar["close"] for bar in bars], [11])

    def test_invalid_input(self):
        for body in ([], {"market": "HK", "symbol": "700.US"}, {"market": "US", "symbol": "https://example.com"}):
            self.assertEqual(self.client.post("/api/watchlist", json=body).status_code, 400)

    def test_seed_once(self):
        self.store.seed()
        ids = [r["id"] for r in self.store.list()]
        self.assertTrue(ids)
        for key in ids:
            self.store.remove(key)
        self.store.seed()
        self.assertFalse(self.store.list())

    def test_symbol_normalization(self):
        self.assertEqual(normalize_symbol("FUTURES", "vx1!"), "VX1!")
        self.assertEqual(normalize_symbol("HK", "00700"), "0700.HK")
        self.assertEqual(normalize_symbol("KR", "035720.KQ"), "035720.KQ")
        self.assertEqual(normalize_symbol("TW", "6488.TWO"), "6488.TWO")

    def test_chart_missing_bar_is_not_zero(self):
        data = {"meta": {"symbol": "AAPL", "dataGranularity": "1m"},
                "timestamp": [120, 180], "indicators": {"quote": [{
                    "open": [10, None], "high": [11, None], "low": [9, None],
                    "close": [10, None], "volume": [None, None]}]}}
        bars = parse_chart(data, "AAPL", "1m")
        self.assertEqual(len(bars), 1)
        self.assertIsNone(bars[0]["volume"])
        with self.assertRaises(ValueError):
            parse_chart(data, "MSFT", "1m")


if __name__ == "__main__":
    unittest.main()
