"""Outbound market data connections. All proxy settings are supplied at runtime."""
import os
from urllib.parse import unquote, urlsplit

import requests
from requests.utils import get_environ_proxies, select_proxy


def parse_proxy(value, setting):
    try:
        url = urlsplit(value)
        if (url.scheme not in {"http", "socks5", "socks5h"} or not url.hostname
                or url.port == 0 or url.path not in ("", "/") or url.query or url.fragment
                or any(char.isspace() for char in value)):
            raise ValueError
        return url
    except ValueError:
        # Parsing errors can contain the raw URL, including a password.
        raise ValueError(f"{setting} 须使用有效的 http、socks5 或 socks5h 代理地址") from None


def proxy_setting(market):
    key = "MARKET_PROXY_" + market
    if not os.environ.get(key, "").strip():
        key = "MARKET_PROXY"
    value = os.environ.get(key, "").strip() or "system"
    if value.lower() in ("direct", "system"):
        return value.lower()
    parse_proxy(value, key)
    return value


def http_session(market):
    policy = proxy_setting(market)
    client = requests.Session()
    if policy != "system":
        client.trust_env = False
        client.verify = os.getenv("REQUESTS_CA_BUNDLE") or os.getenv("CURL_CA_BUNDLE") or True
        if policy != "direct":
            client.proxies.update(http=policy, https=policy)
    return client


def websocket_options(market):
    policy = proxy_setting(market)
    if policy == "system":
        # Match Requests' proxy and NO_PROXY selection for the WSS destination.
        target = "https://data.tradingview.com/"
        policy = select_proxy(target, get_environ_proxies(target)) or "direct"
    if policy == "direct":
        return {"http_no_proxy": ["*"]}
    url = parse_proxy(policy, "WebSocket 代理")
    auth = (unquote(url.username), unquote(url.password or "")) if url.username else None
    return {
        "proxy_type": url.scheme,
        "http_proxy_host": url.hostname,
        "http_proxy_port": url.port or (80 if url.scheme == "http" else 1080),
        "http_proxy_auth": auth,
        "http_proxy_timeout": 6,
        # The destination is TradingView. An explicit nonempty bypass list keeps
        # websocket-client from consulting NO_PROXY a second time.
        "http_no_proxy": ["localhost"],
    }
