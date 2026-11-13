"""Stage 7: lagged spot aggressor flow as auxiliary input; prices stay CoinSwitch."""
import argparse
import bisect
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import statistics

from . import decision_expectancy as decision
from . import funding_expectancy as funding
from . import net_expectancy as research
from .cost_research import write_new

HOUR=3600000
FLOW_FEATURES=('flow_imbalance_1h','flow_imbalance_24h','flow_acceleration_6_24',
               'trade_count_ratio_24_168','mean_trade_size_ratio_24_168','quote_volume_ratio_24_168')


def quarantine_partial_hours(dataset,parent_sha256):
    """Preserve shortened native records separately; never stretch their times."""
    complete=[];quarantined=[]
    for row in dataset['rows']:
        partial=(isinstance(row,list) and len(row)==12 and type(row[0]) is int
                 and type(row[6]) is int and row[0]%HOUR==0 and 0<row[6]-row[0]+1<HOUR)
        (quarantined if partial else complete).append(row)
    output={**dataset,'rows':complete,'quarantine':{'parent_file_sha256':parent_sha256,
        'discarded_partial_hours':len(quarantined),'discarded_open_times':[row[0] for row in quarantined],
        'policy':'incomplete native records excluded; original values/timestamps preserved in quarantine; no filling'}}
    # Any other structural/value defect is still fatal. The whole requested
    # range remains failed because quarantined records create explicit gaps.
    output['integrity']=validate_flow(output,require_complete=False)
    return output,quarantined


def validate_flow(dataset,*,require_complete=True):
    if (dataset.get('schema')!='public-flow-development/v1' or dataset.get('provider')!='Binance public spot API'
            or dataset.get('symbol')!='BTCUSDT' or dataset.get('interval_ms')!=HOUR
            or dataset.get('publication_lag_ms')!=HOUR or dataset.get('credentials_used') is not False
            or dataset.get('orders_executed') is not False or dataset.get('spot_candle_replacement') is not False):
        raise ValueError('invalid independent public flow provenance')
    start,end=dataset['requested_start_ms'],dataset['requested_end_ms']
    if any(type(v) is not int for v in (start,end)) or not 0<=start<end<=1740787200000 or start%HOUR or end%HOUR:
        raise ValueError('flow range must be aligned, historical and development-only')
    times=[]
    for row in dataset['rows']:
        if not isinstance(row,list) or len(row)!=12:raise ValueError('expected native 12-field kline tuple')
        at,closed,trades=row[0],row[6],row[8]
        if type(at) is not int or type(closed) is not int or not start<=at<end or at%HOUR or closed!=at+HOUR-1:
            raise ValueError('invalid native flow time boundary')
        if type(trades) is not int or trades<0:raise ValueError('invalid trade count')
        o,h,l,c,v,q,buy,buy_quote=map(Decimal,(row[i] for i in (1,2,3,4,5,7,9,10)))
        if (not all(value.is_finite() for value in (o,h,l,c,v,q,buy,buy_quote)) or min(o,h,l,c)<=0
                or h<max(o,c) or l>min(o,c) or h<l or min(v,q,buy,buy_quote)<0 or buy>v or buy_quote>q):
            raise ValueError('invalid native OHLC or aggressor volume')
        times.append(at)
    if not times or times!=sorted(set(times)):raise ValueError('flow must be chronological and unique')
    expected=(end-start)//HOUR
    complete=len(times)==expected and all(at==start+i*HOUR for i,at in enumerate(times))
    if require_complete and not complete:raise ValueError('incomplete flow source cannot qualify; derive explicit complete runs')
    return {'passed':complete,'expected_rows':expected,'actual_rows':len(times),'missing_rows':expected-len(times),
            'row_sha256':research.digest(dataset['rows'])}


def derive_runs(dataset,parent_sha256):
    parent=validate_flow(dataset,require_complete=False);runs=[]
    for row in dataset['rows']:
        if not runs or row[0]!=runs[-1][-1][0]+HOUR:runs.append([])
        runs[-1].append(row)
    result=[]
    for i,rows in enumerate(runs,1):
        if len(rows)<168:continue
        derived={**dataset,'rows':rows,'requested_start_ms':rows[0][0],'requested_end_ms':rows[-1][0]+HOUR,
            'derivation':{'parent_file_sha256':parent_sha256,'parent_integrity':parent,
                          'selection':'coverage only; no price/performance inspection','run_id':i}}
        derived['integrity']=validate_flow(derived);result.append(derived)
    return result


def flow_features(rows,decision_ms):
    # A kline's native close is inclusive; add 1ms, then an entire hour lag.
    available=[row[6]+1+HOUR for row in rows]
    index=bisect.bisect_right(available,decision_ms)
    if index<168:return None
    window=rows[index-168:index]
    if decision_ms-available[index-1]>=HOUR:return None
    if any(b[0]!=a[0]+HOUR for a,b in zip(window,window[1:])):return None
    volume=[float(row[5]) for row in window];buy=[float(row[9]) for row in window]
    quote=[float(row[7]) for row in window];trades=[row[8] for row in window]
    if min(sum(volume[-24:]),sum(volume[-6:]),volume[-1],sum(trades[-24:]),sum(trades),sum(quote))<=0:return None
    imbalance=lambda n:2*sum(buy[-n:])/sum(volume[-n:])-1
    size_short=sum(quote[-24:])/sum(trades[-24:]);size_long=sum(quote)/sum(trades)
    return [imbalance(1),imbalance(24),imbalance(6)-imbalance(24),
        statistics.fmean(trades[-24:])/statistics.fmean(trades)-1,
        size_short/size_long-1,statistics.fmean(quote[-24:])/statistics.fmean(quote)-1]


def prepare(paths,funding_path,flow_paths,symbol='BTC/USDT'):
    plan=decision.prepare(paths,funding_path,symbol);sources=[]
    for value in flow_paths:
        path=Path(value).resolve()
        if path.parent.name!='public-flow-complete-runs':raise ValueError('explicit complete public flow derivatives required')
        raw=path.read_bytes();dataset=json.loads(raw);integrity=validate_flow(dataset)
        if sources and dataset['requested_start_ms']<sources[-1]['end_ms']:raise ValueError('overlapping flow sources')
        sources.append({'path':str(path),'file_sha256':hashlib.sha256(raw).hexdigest(),
            'start_ms':dataset['requested_start_ms'],'end_ms':dataset['requested_end_ms'],'integrity':integrity})
    if not sources:raise ValueError('public flow sources required')
    plan.update(schema='flow-expectancy-plan/v1',stage=7,feature_names=plan['feature_names']+list(FLOW_FEATURES),
        flow={'sources':sources,'bundle_sha256':research.digest(sources),'publication_lag_ms':HOUR},
        inner_candidates=[{'kind':kind,'horizon_bars':h,'entry_floor':floor,'full_risk':full}
            for h in (6,24,72) for kind in ('ridge','logistic_payoff','boosted')
            for floor in (None,0.) for full in (False,True)],
        alpha_spending={'stage':7,'allocation':.000078125,'total_budget':.01},bootstrap_replicates=100000)
    plan['implementation_sha256']['trading/app/flow_expectancy.py']=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    plan['limitations']+=['Binance aggressor/trade counts are informational only; CoinSwitch execution prices never replaced',
        'one-hour-lagged public aggregates do not establish point-in-time order-book depth or actual order fills']
    return plan


def execute(plan,folder,*,validator):
    rows=[]
    for source in plan['flow']['sources']:
        raw=Path(source['path']).read_bytes()
        if hashlib.sha256(raw).hexdigest()!=source['file_sha256']:raise ValueError('flow source hash changed')
        data=json.loads(raw);validate_flow(data);rows.extend(data['rows'])
    cache={}
    def augment(reference):
        output={}
        for key,examples in reference.items():
            output[key]=[]
            for row in examples:
                when=row['decision_ms']
                if when not in cache:cache[when]=flow_features(rows,when)
                if cache[when] is not None:output[key].append({**row,'features':row['features']+cache[when]})
        return output
    return funding.execute(plan,folder,augment=augment,validator=validator)


def run(plan,folder):
    return execute(plan,folder,validator=lambda value:
        prepare([s['path'] for s in value['sources']],value['funding']['path'],
                [s['path'] for s in value['flow']['sources']],value.get('symbol','BTC/USDT')))


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('prepare','run'));parser.add_argument('plan')
    parser.add_argument('--source',action='append');parser.add_argument('--funding-source');parser.add_argument('--flow-source',action='append')
    parser.add_argument('--output');args=parser.parse_args(argv)
    if args.action=='prepare':
        if not args.source or not args.funding_source or not args.flow_source:parser.error('explicit development source bundles required')
        write_new(args.plan,prepare(args.source,args.funding_source,args.flow_source))
    else:
        if not args.output:parser.error('new output directory required')
        run(json.loads(Path(args.plan).read_text()),args.output)
    return 0


if __name__=='__main__':raise SystemExit(main())
