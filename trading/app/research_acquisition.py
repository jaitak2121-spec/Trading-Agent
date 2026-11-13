"""Fixed pre-existing-research GET-only acquisition; no training or orders.

Credentials are supplied only in this process environment by the Keychain launcher.
Daily checkpoints permit resumption; a failed day stops acquisition for review.
"""
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import sys
import time

from trading.adapters.coinswitch.historical_candles import CoinSwitchCandleTransport
from trading.adapters.observation_evidence import write_json_atomically
from trading.app.historical_candles import download_historical_candles, validate_candle_dataset
from trading.core.secrets import Secret

ROOT=Path(__file__).resolve().parents[2]
FOLDER=ROOT/'artifacts/development-extension-2025-05-06_2025-11-02'
END=dt.datetime(2025,11,2,tzinfo=dt.timezone.utc)
START=END-dt.timedelta(days=180)
DAY_MS=86400000


def check_day(dataset,start,end):
    source=dataset['source']
    if (dataset['symbol']!='BTC/USDT' or dataset['exchange']!='c2c1'
            or source['requested_start_time']!=start or source['requested_end_time']!=end
            or source['interval_minutes']!=1):
        raise ValueError('checkpoint market or boundaries mismatch')
    validation=validate_candle_dataset(dataset,minimum_candles=1440)
    rows=dataset['candles']
    if (not validation.passed or len(rows)!=1440
            or int(rows[0]['open_time'])!=start or int(rows[-1]['close_time'])!=end):
        raise ValueError('day incomplete or invalid; stop without filling missing prices')
    return validation


def main():
    key=os.environ.pop('COINSWITCH_API_KEY',None)
    secret=os.environ.pop('COINSWITCH_API_SECRET',None)
    if not key or not secret:
        raise ValueError('both credentials required before network access')
    transport=CoinSwitchCandleTransport(api_key=Secret(key),api_secret=Secret(secret))
    del key,secret
    FOLDER.mkdir(exist_ok=True)
    ledger_path=FOLDER/'acquisition.json'
    ledger=json.loads(ledger_path.read_text()) if ledger_path.exists() else {
        'schema':'development-extension-acquisition/v1','start':START.isoformat(),
        'end_exclusive':END.isoformat(),'protected_observations_accessed':False,
        'orders_executed':False,'requests':[],'days':{}}
    if ledger['start']!=START.isoformat() or ledger['end_exclusive']!=END.isoformat():
        raise ValueError('checkpoint range mismatch')
    def fetch(parameters):
        item={'parameters':dict(parameters),'requested_at':dt.datetime.now(dt.timezone.utc).isoformat()}
        try:
            response=transport.fetch_with_details(parameters)
            item.update(http_statuses=list(response.statuses),attempts=response.attempts,
                response_sha256=hashlib.sha256(response.body).hexdigest())
            return response.body
        except Exception as exc:
            item.update(http_statuses=list(getattr(exc,'statuses',())),failed=True)
            raise
        finally:
            ledger['requests'].append(item)
            write_json_atomically(ledger_path,ledger)
            time.sleep(1)
    rows=[]
    for day in range(180):
        start=int((START+dt.timedelta(days=day)).timestamp()*1000)
        end=start+DAY_MS
        assert end<=int(END.timestamp()*1000)
        name=(START+dt.timedelta(days=day)).date().isoformat()+'.json'
        path=FOLDER/name
        if path.exists():
            raw=path.read_bytes()
            recorded=ledger['days'].get(name)
            if recorded and recorded['file_sha256']!=hashlib.sha256(raw).hexdigest():
                raise ValueError('checkpoint hash mismatch')
            dataset=json.loads(raw)
        else:
            dataset,validation=download_historical_candles(fetch=fetch,exchange='c2c1',
                symbol='BTC/USDT',interval_minutes=1,start_time_ms=start,end_time_ms=end,
                chunk_candles=1440)
            # Preserve genuine failed responses for investigation, then fail validation below.
            write_json_atomically(path,dataset)
            raw=path.read_bytes()
        validation=check_day(dataset,start,end)
        rows.extend(dataset['candles'])
        ledger['days'][name]={'file_sha256':hashlib.sha256(raw).hexdigest(),
            'candle_sha256':validation.candle_sha256,'candle_count':len(dataset['candles'])}
        write_json_atomically(ledger_path,ledger)
        if (day+1)%10==0 or day==0:
            print(f'Validated days: {day+1}/180; candles: {len(rows)}',flush=True)
    combined={**dataset,'source':{**dataset['source'],
        'requested_start_time':int(START.timestamp()*1000),'requested_end_time':int(END.timestamp()*1000),
        'response_sha256':[r['response_sha256'] for r in ledger['requests'] if 'response_sha256' in r],
        'daily_file_sha256':{k:v['file_sha256'] for k,v in ledger['days'].items()}},'candles':rows}
    combined.pop('integrity',None)
    validation=validate_candle_dataset(combined,minimum_candles=259200)
    if not validation.passed or len(rows)!=259200:
        raise ValueError('combined continuity/integrity failure')
    combined['integrity']=validation.as_details()
    output=FOLDER/'development-candles.json'
    if output.exists():
        raise ValueError('completed output already exists; refusing overwrite')
    write_json_atomically(output,combined)
    ledger['completed']={'file_sha256':hashlib.sha256(output.read_bytes()).hexdigest(),
        'dataset':str(output.relative_to(ROOT)),'integrity':validation.as_details()}
    write_json_atomically(ledger_path,ledger)
    print(json.dumps(ledger['completed']),flush=True)


if __name__=='__main__':
    try:
        main()
    except Exception as exc:
        # No untrusted exception message, request header or credential is displayed.
        print('STOP: acquisition/validation blocked ('+type(exc).__name__+'). Review credential-free checkpoints.',file=sys.stderr)
        raise SystemExit(2)
