import os
import unittest
from unittest.mock import patch

from python_socks import ProxyError

from websocket._url import get_proxy_info

from proxy_settings import http_session, proxy_setting, websocket_options


class ProxyTest(unittest.TestCase):
    def test_rejected_socks_request_marks_futures_data_unavailable(self):
        from providers import continuous_contract

        with patch("providers.tv_quote", side_effect=ProxyError("denied")), \
             patch("providers.tv_candles", side_effect=ProxyError("denied")):
            quote, bars, issues = continuous_contract("ES1!", True)
        self.assertIsNone(quote)
        self.assertEqual(bars, [])
        self.assertEqual(len(issues), 2)

    def test_market_override_applies_to_both_transports(self):
        env = {"MARKET_PROXY": "http://user:p%40ss@proxy.example:8080",
               "MARKET_PROXY_HK": "direct", "NO_PROXY": "*",
               "HTTPS_PROXY": "http://other.example:8080"}
        with patch.dict(os.environ, env, clear=True):
            with http_session("US") as session:
                self.assertFalse(session.trust_env)
                self.assertEqual(session.proxies["https"], env["MARKET_PROXY"])
            ws = websocket_options("US")
            self.assertEqual(get_proxy_info("data.tradingview.com", True,
                             ws["http_proxy_host"], ws["http_proxy_port"],
                             ws["http_proxy_auth"], ws["http_no_proxy"]),
                             ("proxy.example", 8080, ("user", "p@ss")))
            with http_session("HK") as session:
                self.assertFalse(session.trust_env)
                self.assertEqual(session.proxies, {})
            self.assertEqual(websocket_options("HK"), {"http_no_proxy": ["*"]})

    def test_system_mode_and_market_override(self):
        with patch.dict(os.environ, {}, clear=True):
            with http_session("US") as session:
                self.assertTrue(session.trust_env)
        with patch.dict(os.environ, {"MARKET_PROXY": "direct", "MARKET_PROXY_US": "system",
                                     "HTTPS_PROXY": "socks5h://proxy.example:1080",
                                     "NO_PROXY": "localhost"}, clear=True):
            self.assertEqual(websocket_options("US")["proxy_type"], "socks5h")
            self.assertEqual(websocket_options("FUTURES"), {"http_no_proxy": ["*"]})

    def test_rejects_bad_configuration_without_exposing_credentials(self):
        for url in ("invalid", "http://user:secret@host:99999", "socks5://host:0",
                    "https://user:secret@host:443", "http://host:80/list"):
            with self.subTest(url=url), patch.dict(os.environ, {"MARKET_PROXY": url}, clear=True):
                with self.assertRaises(ValueError) as err:
                    proxy_setting("HK")
                self.assertNotIn("secret", str(err.exception))
                self.assertIn("MARKET_PROXY", str(err.exception))
