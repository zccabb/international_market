"""Persistent watchlist, last quotes and OHLCV candles."""
import os
import time
from pathlib import Path
from uuid import uuid4

from sqlalchemy import (BigInteger, Column, Float, ForeignKey, MetaData, String, Table,
                        UniqueConstraint, create_engine, delete, event, insert, select, update)
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import URL
from sqlalchemy.exc import IntegrityError

from runtime import CandleWatermark

meta = MetaData()
watch = Table("watchlist", meta,
              Column("id", String(32), primary_key=True),
              Column("market", String(12), nullable=False), Column("symbol", String(32), nullable=False),
              Column("name", String(160)), Column("price", Float(53)), Column("previous", Float(53)),
              Column("currency", String(12)), Column("asof", BigInteger), Column("source", String(64)),
              Column("checked", BigInteger), Column("issue", String(240)),
              UniqueConstraint("market", "symbol"))
bars = Table("candles", meta,
             Column("item", String(32), ForeignKey("watchlist.id", ondelete="CASCADE"), primary_key=True),
             Column("interval", String(4), primary_key=True), Column("stamp", BigInteger, primary_key=True),
             *(Column(field, Float(53), nullable=False) for field in ("open", "high", "low", "close")),
             Column("volume", BigInteger))
flags = Table("app_settings", meta, Column("name", String(32), primary_key=True))

EXAMPLES = {"US": "AAPL", "HK": "0700.HK", "JP": "7203.T", "KR": "005930.KS",
            "TW": "2330.TW", "INDEX": "^GSPC", "FUTURES": "ES=F"}


def database_url():
    backend = os.getenv("MARKET_DB", "sqlite").lower()
    if backend == "sqlite":
        path = Path(os.getenv("MARKET_SQLITE_PATH", "data/market.sqlite3")).resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        return URL.create("sqlite", database=str(path))
    if backend != "mysql":
        raise ValueError("MARKET_DB 必须是 sqlite 或 mysql")
    keys = ["HOST", "USER", "PASSWORD", "DATABASE"]
    missing = [key for key in keys if "MARKET_MYSQL_" + key not in os.environ]
    if missing:
        raise ValueError("缺少 MySQL 环境变量：" + ", ".join("MARKET_MYSQL_" + k for k in missing))
    return URL.create("mysql+pymysql", username=os.environ["MARKET_MYSQL_USER"],
                      password=os.environ["MARKET_MYSQL_PASSWORD"], host=os.environ["MARKET_MYSQL_HOST"],
                      port=int(os.getenv("MARKET_MYSQL_PORT", "3306")), database=os.environ["MARKET_MYSQL_DATABASE"],
                      query={"charset": "utf8mb4"})


class Store:
    def __init__(self, url):
        self.engine = create_engine(url, pool_pre_ping=True, hide_parameters=True)
        self.sqlite = self.engine.dialect.name == "sqlite"
        self.watermarks = CandleWatermark()
        if self.sqlite:
            @event.listens_for(self.engine, "connect")
            def setup(db, _):
                db.execute("PRAGMA foreign_keys=ON")
                db.execute("PRAGMA busy_timeout=15000")
            with self.engine.connect() as conn:
                conn.exec_driver_sql("PRAGMA journal_mode=WAL")
        meta.create_all(self.engine)

    def seed(self):
        try:
            with self.engine.begin() as conn:
                if conn.scalar(select(flags.c.name).where(flags.c.name == "examples")):
                    return
                conn.execute(insert(flags).values(name="examples"))
                conn.execute(insert(watch), [dict(id=uuid4().hex, market=m, symbol=s)
                                             for m, s in EXAMPLES.items()])
        except IntegrityError:
            with self.engine.connect() as conn:
                if not conn.scalar(select(flags.c.name).where(flags.c.name == "examples")):
                    raise

    def add(self, market, symbol):
        key = uuid4().hex
        with self.engine.begin() as conn:
            conn.execute(insert(watch).values(id=key, market=market, symbol=symbol))
        return key

    def remove(self, key):
        with self.engine.begin() as conn:
            removed = bool(conn.execute(delete(watch).where(watch.c.id == key)).rowcount)
        if removed:
            self.watermarks.forget(key)
        return removed

    def latest_stamp(self, key, interval="1m"):
        cached = self.watermarks.latest_stamp(key, interval)
        if cached is not None:
            return cached
        with self.engine.connect() as conn:
            stmt = (select(bars.c.stamp)
                    .where(bars.c.item == key, bars.c.interval == interval)
                    .order_by(bars.c.stamp.desc())
                    .limit(1))
            stamp = conn.scalar(stmt)
        if stamp is not None:
            self.watermarks.seed_latest_stamp(key, interval, int(stamp))
            return int(stamp)
        return None

    def list(self):
        with self.engine.connect() as conn:
            return [dict(row) for row in conn.execute(select(watch).order_by(watch.c.market, watch.c.symbol)).mappings()]

    def history(self, key, interval):
        with self.engine.connect() as conn:
            stmt = select(bars).where(bars.c.item == key, bars.c.interval == interval).order_by(bars.c.stamp.desc()).limit(1500)
            return [dict(row) for row in reversed(conn.execute(stmt).mappings().all())]

    def record(self, key, quote, candles, issues):
        self.record_batch([(key, quote, candles, issues)])

    def record_batch(self, entries):
        if not entries:
            return
        staged = [(key, quote, self.watermarks.filter_candles(key, candles), issues)
                  for key, quote, candles, issues in entries]
        committed = []
        now_ts = int(time.time())
        values = ("open", "high", "low", "close", "volume")
        with self.engine.begin() as conn:
            for key, quote, changed_candles, issues in staged:
                # Acquire the row before saving any data. Deleting and re-adding a
                # symbol creates a new id, so old requests cannot populate the new row.
                where = watch.c.id == key
                result = conn.execute(update(watch).where(where).values(
                    checked=now_ts, issue=" · ".join(issues) or None))
                if not result.rowcount:
                    continue
                if quote:
                    fresh = watch.c.asof.is_(None) | (watch.c.asof <= quote["asof"])
                    conn.execute(update(watch).where(where, fresh).values(**quote))
                for pos in range(0, len(changed_candles), 200):
                    rows = [dict(item=key, **bar) for bar in changed_candles[pos:pos + 200]]
                    if self.sqlite:
                        stmt = sqlite_insert(bars).values(rows)
                        stmt = stmt.on_conflict_do_update(index_elements=["item", "interval", "stamp"],
                                                          set_={k: stmt.excluded[k] for k in values})
                    else:
                        stmt = mysql_insert(bars).values(rows)
                        stmt = stmt.on_duplicate_key_update(**{k: stmt.inserted[k] for k in values})
                    conn.execute(stmt)
                if changed_candles:
                    committed.append((key, changed_candles))
        for key, changed_candles in committed:
            self.watermarks.commit_candles(key, changed_candles)
