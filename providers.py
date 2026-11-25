"""Public, credential-free quote and candle adapters."""
import math
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests
import websocket

from tradingview import CHICAGO, CONTRACTS, quote_fields, series, trading_sessions

MARKETS = {"US": "美股", "HK": "港股", "JP": "日股", "KR": "韩股", "TW": "台股",
           "INDEX": "国际指数", "FUTURES": "期货"}
PATTERNS = {"US": r"[A-Z]{1,8}(?:-[A-Z]{1,2})?", "HK": r"\d{1,5}(?:\.HK)?",
            "JP": r"\d[0-9A-Z]{3}(?:\.T)?", "TW": r"\d{4,6}[A-Z]?(?:\.TW|\.TWO)?",
            "KR": r"\d{6}(?:\.KS|\.KQ)?", "INDEX": r"\^[A-Z0-9.-]{1,18}",
            "FUTURES": r"[A-Z0-9]{1,10}=F"}
TW_GATEWAY = "https://tw-gw-finance.media.yahoo.com/api/v1/gql/saved_query"


def normalize_symbol(market, value):
    if not isinstance(value, str) or market not in PATTERNS:
        raise ValueError("请选择市场并输入证券代码")
    symbol = value.strip().upper()
    if market == "FUTURES" and symbol in CONTRACTS:
        return symbol
    if not re.fullmatch(PATTERNS[market], symbol, re.ASCII):
        raise ValueError("证券代码格式与所选市场不匹配")
    if market == "HK":
        return str(int(symbol.split(".")[0])).zfill(4) + ".HK"
    if market in ("JP", "TW", "KR") and "." not in symbol:
        return symbol + {"JP": ".T", "TW": ".TW", "KR": ".KS"}[market]
    return symbol


def number(value):
    if value is None:
        return None
    try:
        n = float(str(value).replace(",", ""))
    except ValueError:
        return None
    return n if math.isfinite(n) else None


def snapshot(price, previous, stamp, name, currency, source):
    price = number(price)
    if price is None or price <= 0 or not stamp or int(stamp) <= 0:
        raise ValueError("缺少有效成交价或行情时间")
    return dict(price=price, previous=number(previous), asof=int(stamp), name=(name or "")[:160],
                currency=(currency or "")[:12], source=source)


def parse_chart(data, symbol, interval):
    meta = data["meta"]
    if meta["symbol"].upper() != symbol or meta["dataGranularity"] != interval:
        raise ValueError("行情标的或周期不匹配")
    q = data["indicators"]["quote"][0]
    rows = {}
    for index, epoch in enumerate(data.get("timestamp") or []):
        def at(field):
            values = q.get(field) or []
            return number(values[index]) if index < len(values) else None
        ohlc = {field: at(field) for field in ("open", "high", "low", "close")}
        if not epoch or any(v is None for v in ohlc.values()):
            continue
        if interval == "1d":
            local = datetime.fromtimestamp(epoch, ZoneInfo(meta["exchangeTimezoneName"]))
            stamp = int(datetime(local.year, local.month, local.day, tzinfo=timezone.utc).timestamp())
        else:
            stamp = int(epoch) // 60 * 60
        volume = at("volume")
        rows[stamp] = dict(interval=interval, stamp=stamp, **ohlc, volume=int(volume) if volume is not None else None)
    if not rows:
        raise ValueError("暂无有效 K 线")
    return list(rows.values())


def request(client, url, **kwargs):
    r = client.get(url, timeout=(4, 8), **kwargs)
    r.raise_for_status()
    return r


def chart(client, market, symbol, interval):
    if market == "TW":
        params = dict(namespace="twfinance", version="v2", id="chart", symbol=symbol,
                      period="d" if interval == "1d" else "1m")
        try:
            data = request(client, TW_GATEWAY, params=params).json()["data"]["chart"][0]
            return data, parse_chart(data, symbol, interval)
        except (requests.RequestException, ValueError, TypeError, IndexError, KeyError):
            pass
    params = dict(interval=interval, range="1mo" if interval == "1d" else "1d",
                  includePrePost="true" if market == "US" and interval == "1m" else "false")
    for host in ("query1", "query2"):
        try:
            url = f"https://{host}.finance.yahoo.com/v8/finance/chart/{symbol}"
            data = request(client, url, params=params).json()["chart"]["result"][0]
            return data, parse_chart(data, symbol, interval)
        except (requests.RequestException, ValueError, TypeError, IndexError, KeyError):
            if host == "query2":
                raise


def local_quote(client, market, symbol):
    if market == "HK":
        code = symbol.split(".")[0].zfill(5)
        r = request(client, "https://qt.gtimg.cn/q=hk" + code, headers={"Referer": "https://gu.qq.com/"})
        fields = r.content.decode("gb18030").split('"')[1].split("~")
        if fields[2] != code:
            raise ValueError("腾讯行情代码不匹配")
        dt = datetime.strptime(fields[30], "%Y/%m/%d %H:%M:%S").replace(tzinfo=ZoneInfo("Asia/Hong_Kong"))
        return snapshot(fields[3], fields[4], dt.timestamp(), fields[1], "HKD", "腾讯行情")
    if market == "KR":
        code, exchange = symbol.split(".")
        data = request(client, "https://polling.finance.naver.com/api/realtime/domestic/stock/" + code).json()["datas"][0]
        if data["itemCode"] != code or data["stockExchangeType"]["code"] != exchange:
            raise ValueError("NAVER 行情代码不匹配")
        price = number(data["closePrice"])
        prev = price - number(data["compareToPreviousClosePrice"])
        stamp = datetime.fromisoformat(data["localTradedAt"]).timestamp()
        return snapshot(price, prev, stamp, data["stockName"], "KRW", "NAVER")
    params = dict(namespace="twfinance", version="v2", id="symbolsWithQuote", symbols=symbol, format="true")
    entry = request(client, TW_GATEWAY, params=params).json()["data"]["result"]["symbols"][0]
    if entry["id"] != symbol:
        raise ValueError("台湾行情代码不匹配")
    q = entry["quote"]
    return snapshot(q["price"]["raw"], q["previousClose"]["raw"], q["marketTime"],
                    entry["name"], q["currency"], "Yahoo 台湾")


def tv_quote(ticker):
    fields = quote_fields(ticker)
    return snapshot(fields["lp"], fields.get("prev_close_price"), fields["lp_time"],
                    fields.get("description"), fields.get("currency_code") or "USD", "TradingView")


def tv_candles(ticker, interval, session, count):
    raw, metadata = series(ticker, "1" if interval == "1m" else "1D", session, count)
    sessions = []
    if interval == "1d":
        first = datetime.fromtimestamp(raw[0][0], CHICAGO).date()
        last = datetime.fromtimestamp(raw[-1][0], CHICAGO).date()
        sessions = trading_sessions(metadata, first - timedelta(days=1), last + timedelta(days=9))
    rows = {}
    for stamp, opening, high, low, close, volume in raw:
        if interval == "1d":
            local = datetime.fromtimestamp(stamp, CHICAGO)
            trading = next((s for s in sessions if s["start"] <= local < s["end"]), None)
            if not trading:
                continue
            stamp = int(datetime.fromisoformat(trading["trade_date"]).replace(tzinfo=timezone.utc).timestamp())
        rows[stamp] = dict(interval=interval, stamp=stamp, open=opening, high=high, low=low,
                           close=close, volume=int(volume) if volume is not None else None)
    if not rows:
        raise ValueError("暂无有效 K 线")
    return list(rows.values())


TV_ERRORS = (websocket.WebSocketException, OSError, ValueError, KeyError, IndexError, TypeError)


def continuous_contract(symbol, daily):
    ticker = CONTRACTS[symbol]
    candles, issues, quote = [], [], None
    try:
        quote = tv_quote(ticker)
    except TV_ERRORS:
        pass
    try:
        candles = tv_candles(ticker, "1m", "regular", 500)
        last = candles[-1]
        if not quote or last["stamp"] > quote["asof"]:
            quote = snapshot(last["close"], (quote or {}).get("previous"), last["stamp"], symbol, "USD", "TradingView 分钟成交")
    except TV_ERRORS:
        issues.append("分钟行情暂不可用")
    if daily:
        try:
            candles.extend(tv_candles(ticker, "1d", "regular", 100))
        except TV_ERRORS:
            issues.append("日线暂不可用")
    return quote, candles, issues


def chart_quote(meta):
    # chartPreviousClose depends on the requested range. Use the 1-day intraday
    # response for this fallback, never the base price of a multi-day range.
    return snapshot(meta.get("regularMarketPrice"), meta.get("previousClose") or meta.get("chartPreviousClose"),
                    meta.get("regularMarketTime"), meta.get("shortName") or meta.get("name") or meta["symbol"],
                    meta.get("currency"), "Yahoo Finance")


def us_session_quote(symbol, meta, minutes, quote, issues):
    exchange = {"NMS": "NASDAQ", "NGM": "NASDAQ", "NCM": "NASDAQ", "NYQ": "NYSE",
                "PCX": "AMEX"}.get(meta.get("exchangeName"))
    traded = {}
    if exchange:
        ticker = exchange + ":" + symbol.replace("-", ".")
        try:
            live = tv_quote(ticker)
            if live["asof"] >= quote["asof"]:
                quote = live
        except TV_ERRORS:
            issues.append("流式报价暂不可用")
        try:
            extended = tv_candles(ticker, "1m", "24h", 500)
            traded = {bar["stamp"]: bar for bar in extended if bar["volume"] and bar["volume"] > 0}
        except TV_ERRORS:
            issues.append("全时段分钟线暂不可用")
    combined = {bar["stamp"]: bar for bar in minutes}
    combined.update(traded)
    last = max((bar for bar in combined.values() if bar["volume"] and bar["volume"] > 0),
               key=lambda bar: bar["stamp"], default=None)
    if last and last["stamp"] > quote["asof"]:
        ny = ZoneInfo("America/New_York")
        local = datetime.fromtimestamp(last["stamp"], ny)
        regular = datetime.fromtimestamp(meta["regularMarketTime"], ny)
        after_close = local.date() > regular.date() or local.hour >= 16
        previous = meta.get("regularMarketPrice") if after_close else quote["previous"]
        source = "TradingView 分钟成交" if last["stamp"] in traded else "Yahoo 分钟成交"
        quote = snapshot(last["close"], previous, last["stamp"], quote["name"], quote["currency"], source)
    return quote, list(combined.values())


def collect(market, symbol, daily):
    if market == "FUTURES" and symbol in CONTRACTS:
        return continuous_contract(symbol, daily)
    quote = None
    candles, issues = [], []
    with requests.Session() as client:
        client.headers["User-Agent"] = "Mozilla/5.0 (compatible; InternationalMarket/1.0)"
        if market in ("HK", "KR", "TW"):
            try:
                quote = local_quote(client, market, symbol)
            except (requests.RequestException, KeyError, IndexError, ValueError, TypeError):
                issues.append("本地报价源不可用")
        try:
            data, minutes = chart(client, market, symbol, "1m")
            meta = data["meta"]
            if not quote:
                quote = chart_quote(meta)
            if market == "US":
                quote, minutes = us_session_quote(symbol, meta, minutes, quote, issues)
            candles.extend(minutes)
        except (requests.RequestException, ValueError, TypeError, KeyError, IndexError, OSError):
            issues.append("分钟行情暂不可用")
        if daily:
            try:
                _, rows = chart(client, market, symbol, "1d")
                candles.extend(rows)
            except (requests.RequestException, ValueError, TypeError, KeyError, IndexError, OSError):
                issues.append("日线暂不可用")
    return quote, candles, issues
