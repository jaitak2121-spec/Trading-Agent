"""Stage 5: settled public funding as information, never as spot cash income."""
import argparse
import bisect
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import statistics

from . import adaptive_expectancy as adaptive
from . import net_expectancy as research
from .cost_research import write_new

FUNDING_FEATURES=('funding_last','funding_mean_24h','funding_mean_7d','funding_z_7d',
                  'funding_change_24h','funding_age_hours')
LAG=3600000
EIGHT_HOURS=28800000


def validate_funding(dataset):
    if (dataset.get('schema')!='public-funding-development/v1' or dataset.get('symbol')!='BTCUSDT'
            or dataset.get('provider')!='Binance USD-M perpetual public API'
            or dataset.get('credentials_used') is not False or dataset.get('orders_executed') is not False
            or dataset.get('spot_candle_replacement') is not False or dataset.get('availability_lag_ms')!=LAG):
        raise ValueError('explicit uncredentialed funding provenance required')
    start,end=dataset['requested_start_ms'],dataset['requested_end_ms']
    if type(start) is not int or type(end) is not int or not 0<=start<end<=1740787200000:
        raise ValueError('funding must remain strictly in development history')
    events=dataset['events'];times=[]
    for event in events:
        at=event['fundingTime'];rate=Decimal(event['fundingRate'])
        if (event['symbol']!='BTCUSDT' or type(at) is not int or not start<=at<end
                or not rate.is_finite() or abs(rate)>1 or event.get('rateType','Regular')!='Regular'):
            raise ValueError('invalid public funding observation')
        times.append(at)
    if len(times)<21 or times!=sorted(set(times)):
        raise ValueError('funding events must be unique and strictly chronological')
    # Expected BTC 8-hour cadence is independently checked against actual
    # recorded timestamps, allowing exchange millisecond timestamp jitter.
    if times[0]>start+60000 or times[-1]<end-EIGHT_HOURS-60000:
        raise ValueError('funding requested range not covered')
    if any(abs((b-a)-EIGHT_HOURS)>60000 for a,b in zip(times,times[1:])):
        raise ValueError('missing or changed-cadence funding events; no forward fill')
    return {'events':len(times),'first_event_ms':times[0],'last_event_ms':times[-1],
            'events_sha256':research.digest(events),'verified_cadence':'8 hours +/- 60 seconds timestamp jitter'}


def funding_features(events,decision_ms):
    times=[event['fundingTime'] for event in events]
    index=bisect.bisect_right(times,decision_ms-LAG)
    if index<21:return None
    window=events[index-21:index];when=[row['fundingTime'] for row in window]
    if decision_ms-when[-1]>EIGHT_HOURS+LAG+60000:return None
    if any(abs((b-a)-EIGHT_HOURS)>60000 for a,b in zip(when,when[1:])):return None
    rates=[float(Decimal(row['fundingRate'])) for row in window]
    mean=statistics.fmean(rates);scale=statistics.pstdev(rates)
    return [rates[-1],statistics.fmean(rates[-3:]),mean,(rates[-1]-mean)/scale if scale else 0.,
            rates[-1]-rates[-4],(decision_ms-when[-1])/3600000]


def prepare(paths,funding_path,symbol='BTC/USDT'):
    path=Path(funding_path).resolve()
    if path.parent.name!='public-funding-development' or path.name!='dataset.json':
        raise ValueError('explicit public development funding artifact required')
    raw=path.read_bytes();dataset=json.loads(raw);integrity=validate_funding(dataset)
    plan=adaptive.prepare(paths,symbol)
    plan.update(schema='funding-adaptive-expectancy-plan/v1',stage=5,
        feature_names=list(research.FEATURES+FUNDING_FEATURES),
        funding={'path':str(path),'file_sha256':hashlib.sha256(raw).hexdigest(),'integrity':integrity,
                 'availability_lag_ms':LAG,'minimum_outer_coverage':.95},
        inner_candidates=[{'kind':kind,'horizon_bars':h} for h in (24,72,168)
                          for kind in ('ridge','tree','logistic_payoff')],
        alpha_spending={'stage':5,'allocation':.0003125,'total_budget':.01})
    plan['implementation_sha256']['trading/app/funding_expectancy.py']=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    plan['limitations']+=['Binance perpetual funding is auxiliary information, not CoinSwitch spot price replacement or funding income',
        'settled-rate publication is modeled with a fixed conservative one-hour lag; prospective availability remains to verify']
    return plan


def execute(plan,folder,*,validator,augment=None):
    path=Path(plan['funding']['path']);raw=path.read_bytes()
    if hashlib.sha256(raw).hexdigest()!=plan['funding']['file_sha256']:raise ValueError('funding source changed')
    dataset=json.loads(raw);validate_funding(dataset);cache={}
    def build(reference):
        result={}
        for key,rows in reference.items():
            result[key]=[]
            for row in rows:
                when=row['decision_ms']
                if when not in cache:cache[when]=funding_features(dataset['events'],when)
                if cache[when] is not None:result[key].append({**row,'features':row['features']+cache[when]})
        return augment(result) if augment else result
    return adaptive.execute(plan,folder,sample_builder=build,validator=validator)


def run(plan,folder):
    return execute(plan,folder,validator=lambda value:
        prepare([s['path'] for s in value['sources']],value['funding']['path'],value.get('symbol','BTC/USDT')))


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('prepare','run'));parser.add_argument('plan')
    parser.add_argument('--source',action='append');parser.add_argument('--funding-source');parser.add_argument('--output')
    args=parser.parse_args(argv)
    if args.action=='prepare':
        if not args.source or not args.funding_source:parser.error('explicit development inputs required')
        write_new(args.plan,prepare(args.source,args.funding_source))
    else:
        if not args.output:parser.error('new output directory required')
        run(json.loads(Path(args.plan).read_text()),args.output)
    return 0


if __name__=='__main__':raise SystemExit(main())
