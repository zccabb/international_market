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

from providers import MARKETS, collect, normalize_symbol
from proxy_settings import proxy_setting
from store import EXAMPLES, Store, database_url


class Collector:
    def __init__(self, store, interval):
        self.store = store
        self.interval = interval
        self.changed = threading.Event()
        self.closed = threading.Event()
        self.thread = threading.Thread(target=self.run, name="scheduler")

    def fetch(self, row, daily):
        quote, candles, issues = collect(row["market"], row["symbol"], daily)
        self.store.record(row["id"], quote, candles, issues)
        return any(bar["interval"] == "1d" for bar in candles)

    def run(self):
        due, daily_due, running = {}, {}, {}
        with ThreadPoolExecutor(max_workers=4, thread_name_prefix="quotes") as pool:
            while not self.closed.is_set():
                self.changed.clear()
                now = time.monotonic()
                try:
                    for key, future in list(running.items()):
                        if future.done():
                            del running[key]
                            due[key] = now + self.interval
                            if future.result():
                                daily_due[key] = now + 1800
                    rows = self.store.list()
                    keys = {row["id"] for row in rows}
                    for schedule in (due, daily_due):
                        for key in schedule.keys() - keys:
                            del schedule[key]
                    for row in rows:
                        key = row["id"]
                        if len(running) >= 4:
                            break
                        if key not in running and now >= due.get(key, 0):
                            running[key] = pool.submit(self.fetch, row, now >= daily_due.get(key, 0))
                except Exception as exc:
                    # No connection strings, request headers or response bodies in logs.
                    logging.warning("Collector could not complete task (%s)", type(exc).__name__)
                self.changed.wait(1)


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
