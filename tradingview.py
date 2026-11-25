"""Read anonymous quote and chart snapshots with a bounded connection lifetime."""
import json
import re
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from uuid import uuid4

import websocket

CONTRACTS = {"ES1!": "CME_MINI:ES1!", "NQ1!": "CME_MINI:NQ1!", "YM1!": "CBOT_MINI:YM1!",
             "RTY1!": "CME_MINI:RTY1!", "VX1!": "CBOE:VX1!"}


@contextmanager
def feed(commands):
    ws = websocket.create_connection("wss://data.tradingview.com/socket.io/websocket?from=chart%2F",
                                     origin="https://www.tradingview.com", timeout=6)
    def messages():
        end = time.monotonic() + 8
        while time.monotonic() < end:
            ws.settimeout(max(0.1, end - time.monotonic()))
            raw = ws.recv()
            if not raw:
                break
            for payload in re.split(r"~m~\d+~m~", raw):
                if payload.startswith("~h~"):
                    ws.send(f"~m~{len(payload)}~m~{payload}")
                elif payload.startswith("{"):
                    message = json.loads(payload)
                    method = message.get("m")
                    if method in ("symbol_error", "series_error", "critical_error", "protocol_error"):
                        raise ValueError("行情源拒绝了这个标的或周期")
                    yield method, message.get("p", [])
        raise TimeoutError("行情快照超时")
    try:
        for method, params in [("set_auth_token", ["unauthorized_user_token"]), *commands]:
            body = json.dumps({"m": method, "p": params}, separators=(",", ":"))
            ws.send(f"~m~{len(body)}~m~{body}")
        yield messages()
    finally:
        ws.close()


def quote_fields(ticker):
    session = "qs_" + uuid4().hex[:12]
    fields = {}
    commands = [("quote_create_session", [session]),
                ("quote_set_fields", [session, "lp", "lp_time", "prev_close_price", "currency_code", "description"]),
                ("quote_add_symbols", [session, ticker])]
    with feed(commands) as stream:
        for method, params in stream:
            if method == "qsd" and params[0] == session and params[1].get("n") == ticker:
                fields.update(params[1].get("v") or {})
            if method == "quote_completed" and params == [session, ticker]:
                break
    if not fields.get("lp_time") or not fields.get("lp"):
        raise ValueError("暂无有效快照")
    return fields


def series(ticker, period, session_name, count):
    session = "cs_" + uuid4().hex[:12]
    spec = "=" + json.dumps({"symbol": ticker, "session": session_name, "adjustment": "splits"})
    commands = [("chart_create_session", [session, ""]),
                ("resolve_symbol", [session, "symbol_1", spec]),
                ("create_series", [session, "s1", "s1", "symbol_1", period, count, ""])]
    values = {}
    metadata = {}
    with feed(commands) as stream:
        for method, params in stream:
            if method == "symbol_resolved" and params[0] == session:
                metadata = params[2]
            if method == "timescale_update" and params[0] == session:
                for bar in params[1].get("s1", {}).get("s", []):
                    row = bar.get("v", [])
                    if len(row) >= 6 and all(v is not None for v in row[:5]):
                        values[int(row[0])] = row[:6]
            if method == "series_completed" and "s1" in params:
                break
    if not values:
        raise ValueError("暂无有效 K 线")
    return [values[t] for t in sorted(values)], metadata


CHICAGO = ZoneInfo("America/Chicago")


def trading_sessions(metadata, start_date, end_date):
    """Parse provider ETH sessions, including F1/F2 holiday offsets and dayoff.

    Corrections override holidays: holiday trading can belong to the next
    trading day and contain multiple disjoint sessions.
    """
    selected = next((s for s in metadata.get("subsessions", [])
                     if s.get("id") == metadata.get("subsession_id", "regular")), metadata)
    regular = selected.get("session") or metadata.get("session")
    if not regular or metadata.get("timezone") != "America/Chicago":
        raise ValueError("Missing or unsupported futures session metadata")
    corrections = {}
    for item in (selected.get("session-correction") or metadata.get("corrections") or "").split(";"):
        if ":" in item:
            hours, dates = item.rsplit(":", 1)
            corrections.update((d, hours) for d in dates.split(","))
    holidays = set((metadata.get("session_holidays") or "").split(","))
    result = []
    day = start_date
    while day <= end_date:
        key = day.strftime("%Y%m%d")
        hours = corrections.get(key)
        if hours is None:
            hours = "dayoff" if day.weekday() >= 5 or key in holidays else regular
        if hours != "dayoff":
            for segment in hours.split(","):
                match = re.fullmatch(r"(\d{4})(?:F(\d+))?-(\d{4})(?:F(\d+))?", segment)
                if not match:
                    raise ValueError("Unsupported futures session format: " + segment)
                opening, start_offset, closing, end_offset = match.groups()
                # Without an explicit offset, an overnight open is the prior day.
                end_offset = int(end_offset or 0)
                start_offset = int(start_offset) if start_offset is not None else end_offset + int(opening >= closing)
                def stamp(clock, offset):
                    base = datetime.combine(day - timedelta(days=offset), datetime.min.time(), CHICAGO)
                    return base + timedelta(hours=int(clock[:2]), minutes=int(clock[2:]))
                start, end = stamp(opening, start_offset), stamp(closing, end_offset)
                if end <= start:
                    raise ValueError("Invalid futures session bounds")
                result.append({"trade_date": day.isoformat(), "session": "futures", "start": start,
                               "end": end, "end_exclusive": True})
        day += timedelta(days=1)
    return sorted(result, key=lambda item: item["start"])
