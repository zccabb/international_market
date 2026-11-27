"""Run the collector and its local watchlist dashboard in one process."""
import argparse
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit

from flask import Flask, abort, jsonify, render_template, request
from sqlalchemy.exc import IntegrityError
from waitress import serve

from providers import MARKETS, collect, normalize_symbol, prefetch_quotes
from proxy_settings import proxy_setting
from runtime import MicroBatchRecorder, SymbolCooldown, compute_next_due
from store import EXAMPLES, Store, database_url


class Collector:
    def __init__(self, store, interval):
        self.store = store
        self.interval = interval
        self.changed = threading.Event()
        self.closed = threading.Event()
        self.cooldowns = SymbolCooldown()
        self.recorder = None
        self.thread = threading.Thread(target=self.run, name="scheduler")

    def fetch(self, row, daily, prefetched_quote=None):
        since_stamp = self.store.latest_stamp(row["id"], "1m")
        quote, candles, issues = collect(row["market"], row["symbol"], daily,
                                         since_stamp=since_stamp, prefetched_quote=prefetched_quote)
        entry = (row["id"], quote, candles, issues)
        if self.recorder is not None:
            self.recorder.submit(entry)
        else:
            self.store.record(*entry)
        has_daily = any(bar["interval"] == "1d" for bar in candles)
        succeeded = bool(quote or candles)
        return has_daily, succeeded

    def run(self):
        due, daily_due, running = {}, {}, {}
        self.recorder = MicroBatchRecorder(
            self.store.record_batch,
            max_items=8,
            flush_interval=0.5,
            on_error=lambda exc: logging.warning("Collector could not save batch (%s)", type(exc).__name__),
        )
        try:
            with ThreadPoolExecutor(max_workers=4, thread_name_prefix="quotes") as pool:
                while not self.closed.is_set():
                    self.changed.clear()
                    now = time.monotonic()
                    wait_seconds = 1.0
                    try:
                        for key, (future, started_at) in list(running.items()):
                            if future.done():
                                del running[key]
                                has_daily, succeeded = future.result()
                                if succeeded:
                                    self.cooldowns.record_success(key)
                                    due[key] = compute_next_due(started_at, self.interval, now)
                                else:
                                    backoff = self.cooldowns.record_failure(key, self.interval)
                                    due[key] = (now + backoff) if backoff else compute_next_due(started_at, self.interval, now)
                                if has_daily:
                                    daily_due[key] = now + 1800
                        rows = self.store.list()
                        keys = {row["id"] for row in rows}
                        self.cooldowns.forget_missing(keys)
                        for schedule in (due, daily_due):
                            for key in schedule.keys() - keys:
                                del schedule[key]

                        ready = [row for row in rows if row["id"] not in running and now >= due.get(row["id"], 0)]
                        prefetched = {}
                        for batch_market in ("HK", "TW"):
                            batch_symbols = [r["symbol"] for r in ready if r["market"] == batch_market]
                            if len(batch_symbols) >= 2:
                                prefetched.update(
                                    {(batch_market, sym): q for sym, q in prefetch_quotes(batch_market, batch_symbols).items()}
                                )

                        for row in ready:
                            if len(running) >= 4:
                                break
                            key = row["id"]
                            started = time.monotonic()
                            fut = pool.submit(
                                self.fetch,
                                row,
                                now >= daily_due.get(key, 0),
                                prefetched.get((row["market"], row["symbol"])),
                            )
                            fut.add_done_callback(lambda _f: self.changed.set())
                            running[key] = (fut, started)

                        pending_dues = [due[row["id"]] for row in rows
                                        if row["id"] not in running and row["id"] in due]
                        if len(running) < 4 and any(row["id"] not in running and now >= due.get(row["id"], 0) for row in rows):
                            wait_seconds = 0.05
                        elif pending_dues:
                            wait_seconds = max(0.05, min(1.0, min(pending_dues) - time.monotonic()))
                    except Exception as exc:
                        # No connection strings, request headers or response bodies in logs.
                        logging.warning("Collector could not complete task (%s)", type(exc).__name__)
                    self.changed.wait(wait_seconds)
        finally:
            recorder, self.recorder = self.recorder, None
            if recorder is not None:
                recorder.close()


def create_app(url=None, seed=True):
    app = Flask(__name__)
    app.config.update(MAX_CONTENT_LENGTH=4096, TRUSTED_HOSTS=["localhost", "127.0.0.1", "[::1]"])
    store = Store(url or database_url())
    if seed:
        store.seed()
    app.extensions["store"] = store

    @app.before_request
    def check_origin():
        origin = request.headers.get("Origin")
        if request.method in ("POST", "DELETE") and origin and urlsplit(origin).netloc != request.host:
            abort(403)

    @app.get("/")
    def home():
        return render_template("index.html", markets=MARKETS, examples=EXAMPLES)

    @app.get("/api/watchlist")
    def watchlist():
        return jsonify(store.list())

    @app.post("/api/watchlist")
    def add():
        body = request.get_json(silent=True)
        if not isinstance(body, dict) or not isinstance(body.get("market"), str):
            return jsonify(error="请选择市场并输入证券代码"), 400
        market = body["market"]
        try:
            symbol = normalize_symbol(market, body.get("symbol"))
            key = store.add(market, symbol)
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
        except IntegrityError:
            return jsonify(error="该标的已在采集清单中"), 409
        worker = app.extensions.get("collector")
        if worker:
            worker.changed.set()
        return jsonify(id=key, market=market, symbol=symbol), 201

    @app.delete("/api/watchlist/<key>")
    def remove(key):
        if not store.remove(key):
            abort(404)
        return "", 204

    @app.get("/api/history/<key>")
    def history(key):
        interval = request.args.get("interval", "1m")
        if interval not in ("1m", "1d"):
            return jsonify(error="周期必须是 1m 或 1d"), 400
        return jsonify(store.history(key, interval))

    return app


def main():
    parser = argparse.ArgumentParser(description="国际行情采集与本地看板")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--interval", type=int, default=60, help="单票轮询间隔（秒，至少 10）")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535 or args.interval < 10:
        parser.error("端口必须是 1–65535，采集间隔至少为 10 秒")
    try:
        for market in MARKETS:
            proxy_setting(market)
    except ValueError as exc:
        parser.error(str(exc))
    app = create_app()
    worker = Collector(app.extensions["store"], args.interval)
    app.extensions["collector"] = worker
    worker.thread.start()
    print(f"行情看板 http://127.0.0.1:{args.port}  ·  按 Ctrl+C 退出", flush=True)
    try:
        serve(app, host="127.0.0.1", port=args.port)
    finally:
        worker.closed.set()
        worker.changed.set()
        worker.thread.join()
        app.extensions["store"].engine.dispose()


if __name__ == "__main__":
    main()
