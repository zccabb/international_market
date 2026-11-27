import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

from sqlalchemy import event

from market import create_app
from providers import (BROWSER_HEADERS, FULL_MINUTE_BARS, INCREMENTAL_MINUTE_BARS, collect,
                       continuous_contract, minute_bar_count, prefetch_quotes, us_session_quote)
from runtime import HostRateLimiter, MicroBatchRecorder, SymbolCooldown, compute_next_due


class RuntimeOptimizationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        url = "sqlite:///" + (Path(self.tmp.name) / "quotes.db").as_posix()
        self.app = create_app(url, seed=False)
        self.store = self.app.extensions["store"]

    def tearDown(self):
        self.store.engine.dispose()
        self.tmp.cleanup()

    def test_fixed_rate_and_cooldown_primitives(self):
        self.assertEqual(compute_next_due(100.0, 60, now=118.0), 160.0)
        self.assertEqual(compute_next_due(100.0, 60, now=170.0, min_cooldown=5.0), 175.0)

        limiter = HostRateLimiter((0.0, 0.0), max_multiplier=8.0, recovery_step=0.5)
        self.assertEqual(limiter.on_rate_limited("query1.finance.yahoo.com", cooldown=0.01), 2.0)
        self.assertEqual(limiter.on_rate_limited("query1.finance.yahoo.com", cooldown=0.01), 4.0)
        limiter._hosts["query1.finance.yahoo.com"]["hold_until"] = 0.0
        for _ in range(6):
            limiter.on_success("query1.finance.yahoo.com")
        self.assertEqual(limiter.multiplier("query1.finance.yahoo.com"), 1.0)

        cooldown = SymbolCooldown(threshold=3, max_backoff=300)
        self.assertIsNone(cooldown.record_failure("k1", 60))
        self.assertIsNone(cooldown.record_failure("k1", 60))
        self.assertEqual(cooldown.record_failure("k1", 60), 120.0)
        cooldown.record_success("k1")
        self.assertIsNone(cooldown.record_failure("k1", 60))

    def test_candle_watermark_skips_redundant_sqlite_upserts(self):
        key = self.store.add("TW", "2330.TW")
        b1 = dict(interval="1m", stamp=1000, open=900, high=905, low=898, close=902, volume=100)
        b2 = dict(interval="1m", stamp=1060, open=902, high=906, low=901, close=905, volume=200)
        candle_writes = []

        @event.listens_for(self.store.engine, "before_cursor_execute")
        def count_sql(_conn, _cursor, statement, *_args):
            if "INSERT INTO candles" in statement:
                candle_writes.append(statement)

        self.store.record(key, {"price": 905, "asof": 1060, "source": "Yahoo 台湾"}, [b1, b2], [])
        self.assertEqual(len(candle_writes), 1)
        self.assertEqual(self.store.latest_stamp(key, "1m"), 1060)

        # Identical historical bars produce zero candle INSERT statements.
        self.store.record(key, {"price": 905, "asof": 1060, "source": "Yahoo 台湾"}, [b1, b2], [])
        self.assertEqual(len(candle_writes), 1)

        # Updating the latest bar's close writes only the changed candle.
        b2_updated = dict(b2, close=908)
        self.store.record(key, {"price": 908, "asof": 1065, "source": "Yahoo 台湾"}, [b1, b2_updated], [])
        self.assertEqual(len(candle_writes), 2)
        self.assertEqual([r["close"] for r in self.store.history(key, "1m")], [902.0, 908.0])

        self.store.remove(key)
        self.assertIsNone(self.store.watermarks.latest_stamp(key, "1m"))

    def test_micro_batch_recorder_flushes_by_size_and_timer(self):
        flushed = []
        recorder = MicroBatchRecorder(lambda batch: flushed.append(list(batch)), max_items=3, flush_interval=0.05)
        try:
            for i in range(4):
                recorder.submit(i)
            time.sleep(0.08)
            recorder.submit(4)
            recorder.flush()
        finally:
            recorder.close()
        self.assertEqual([item for batch in flushed for item in batch], [0, 1, 2, 3, 4])

    def test_dynamic_minute_bar_count_for_us_and_futures(self):
        now = int(time.time())
        self.assertEqual(minute_bar_count(None), FULL_MINUTE_BARS)
        self.assertEqual(minute_bar_count(now - 7200), FULL_MINUTE_BARS)
        self.assertEqual(minute_bar_count(now - 120), INCREMENTAL_MINUTE_BARS)

        bar = dict(interval="1m", stamp=now - 60, open=100, high=101, low=99, close=100.5, volume=50)
        with patch("providers.tv_quote", return_value={"price": 100.5, "previous": 99.0, "asof": now - 60,
                                                       "name": "ES1!", "currency": "USD", "source": "TradingView"}), \
             patch("providers.tv_candles", return_value=[bar]) as candles_mock:
            continuous_contract("ES1!", False, since_stamp=now - 120)
            self.assertEqual(candles_mock.call_args.args[3], INCREMENTAL_MINUTE_BARS)

            meta = {"exchangeName": "NMS", "regularMarketTime": now - 120, "regularMarketPrice": 200.0}
            quote = {"price": 200.0, "previous": 198.0, "asof": now - 120,
                     "name": "Apple", "currency": "USD", "source": "Yahoo Finance"}
            us_session_quote("AAPL", meta, [bar], quote, [], since_stamp=now - 120)
            self.assertEqual(candles_mock.call_args.args[3], INCREMENTAL_MINUTE_BARS)

    def test_prefetch_quotes_and_gzip_headers(self):
        self.assertIn("gzip", BROWSER_HEADERS["Accept-Encoding"])
        tw_payload = {
            "data": {"result": {"symbols": [
                {"id": "2330.TW", "name": "台积电", "quote": {
                    "price": {"raw": "1020"}, "previousClose": {"raw": "1000"},
                    "marketTime": 1700000000, "currency": "TWD"}},
                {"id": "2317.TW", "name": "鸿海", "quote": {
                    "price": {"raw": "210"}, "previousClose": {"raw": "205"},
                    "marketTime": 1700000000, "currency": "TWD"}},
            ]}}
        }
        session = MagicMock()
        session.__enter__.return_value = session
        session.headers = {}
        session.get.return_value = Mock(status_code=200, json=Mock(return_value=tw_payload))

        with patch("providers.http_session", return_value=session), patch("providers.HTTP_PACER.acquire"):
            quotes = prefetch_quotes("TW", ["2330.TW", "2317.TW"])
        self.assertEqual(set(quotes), {"2330.TW", "2317.TW"})
        self.assertEqual(quotes["2330.TW"]["price"], 1020.0)

        chart_data = {"meta": {"symbol": "2330.TW", "dataGranularity": "1m"}}
        chart_bars = [dict(interval="1m", stamp=1700000000, open=1015, high=1020, low=1010, close=1020, volume=10)]
        with patch("providers.http_session", return_value=session), \
             patch("providers.local_quote") as single_quote, \
             patch("providers.chart", return_value=(chart_data, chart_bars)):
            quote, candles, issues = collect("TW", "2330.TW", False, prefetched_quote=quotes["2330.TW"])
        single_quote.assert_not_called()
        self.assertEqual(quote["price"], 1020.0)
        self.assertEqual(len(candles), 1)
        self.assertEqual(issues, [])


if __name__ == "__main__":
    unittest.main()
