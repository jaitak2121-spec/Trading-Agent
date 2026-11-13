"""Durable closed-bar experience with a bounded inference cache; no execution."""
import datetime as dt
import json
import hashlib

from ..marketdata.candle_store import InMemoryCandleStore
from ...core.marketdata import Candle
from ...core.money import Price, Quantity


def candle_row(bar):
    return {'symbol':bar.symbol,'open_time':int(bar.open_time.timestamp()*1000),
            'interval_seconds':bar.interval_seconds,'open':str(bar.open.amount),
            'high':str(bar.high.amount),'low':str(bar.low.amount),'close':str(bar.close.amount),
            'volume':str(bar.volume.amount),'source':bar.source}



class SQLiteCandleStore(InMemoryCandleStore):
    def __init__(self,state,*,symbol,currency,asset,source,clock,cache_size=4096):
        super().__init__()
        self.state=state;self.symbol=symbol;self.currency=currency;self.asset=asset
        self.source=source;self.clock=clock;self.cache_size=cache_size
        with state.transaction():
            state.connection.execute('CREATE TABLE IF NOT EXISTS market_candles '
                '(symbol TEXT NOT NULL, open_ms INTEGER NOT NULL, payload TEXT NOT NULL, payload_sha256 TEXT NOT NULL, '
                'PRIMARY KEY(symbol,open_ms))')
        with state.read() as connection:
            rows=connection.execute('SELECT open_ms,payload,payload_sha256 FROM market_candles WHERE symbol=? ORDER BY open_ms DESC LIMIT ?',
                                    (symbol,cache_size)).fetchall()
        for row in reversed(rows):
            if hashlib.sha256(row[1].encode()).hexdigest()!=row[2]:raise ValueError('persisted candle hash mismatch')
            value=json.loads(row[1]);bar=self._decode(value)
            if row[0]!=value['open_time']:raise ValueError('persisted candle key mismatch')
            if candle_row(bar)!=value:raise ValueError('persisted candle cannot be reproduced')
            self._validate(bar)
            last=self.last(symbol)
            if last is not None and bar.open_time<=last.open_time:raise ValueError('persisted candle chronology mismatch')
            super().append(bar)

    def _decode(self,row):
        return Candle(symbol=row['symbol'],open_time=dt.datetime.fromtimestamp(row['open_time']/1000,dt.timezone.utc),
            interval_seconds=row['interval_seconds'],volume=Quantity(row['volume'],self.asset),source=row['source'],
            **{name:Price(row[name],self.currency) for name in ('open','high','low','close')})

    def _validate(self,bar):
        if (bar.symbol!=self.symbol or bar.source!=self.source or bar.close.currency!=self.currency
                or bar.volume.asset!=self.asset or bar.interval_seconds!=60 or not bar.is_complete(self.clock.now())):
            raise ValueError('persisted experience must be complete matching one-minute market bars')

    def append(self,bar):
        self._validate(bar);row=candle_row(bar);encoded=json.dumps(row,sort_keys=True)
        with self.state.transaction():
            prior=self.state.connection.execute('SELECT payload FROM market_candles WHERE symbol=? AND open_ms=?',
                                                (bar.symbol,row['open_time'])).fetchone()
            if prior:
                if prior[0]!=encoded:raise ValueError('conflicting persisted candle replay')
                return
            last=self.last(bar.symbol)
            if last is not None and bar.open_time<=last.open_time:raise ValueError('out-of-order persisted candle')
            self.state.connection.execute('INSERT INTO market_candles VALUES(?,?,?,?)',
                (bar.symbol,row['open_time'],encoded,hashlib.sha256(encoded.encode()).hexdigest()))
        super().append(bar)
        with self._lock:
            self._bars[bar.symbol]=self._bars[bar.symbol][-self.cache_size:]

    def status(self):
        with self.state.read() as connection:
            row=connection.execute('SELECT COUNT(*),MIN(open_ms),MAX(open_ms) FROM market_candles WHERE symbol=?',(self.symbol,)).fetchone()
        return {'persistent':True,'candles':row[0],'first_open_ms':row[1],'last_open_ms':row[2],
                'cached_candles':len(self),'gaps_filled':False}

    def verify_replay(self,bar):
        self.append(bar)
