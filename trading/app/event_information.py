"""Preregistered event-time incremental information screen; no trader or promotion."""
import argparse
import hashlib
import json
from pathlib import Path
import statistics

from . import adaptive_expectancy as adaptive
from . import funding_expectancy as funding
from . import flow_expectancy as flow
from . import net_expectancy as r
from . import signal_information as diagnostic
from .backtest import load_historical_candles
from .cost_research import write_new

HOUR=3600000
NAMES=('return_12h','volatility_48h','trend_strength')
AUX=('settled_funding_z_7d','lagged_flow_imbalance_24h','lagged_flow_acceleration_6_24')


def event_due(when,horizon,schedule):
    if horizon not in (3,6,24) or schedule not in ('settlement','clock'):
        raise ValueError('unsupported preregistered event definition')
    offset=2*HOUR if schedule=='settlement' else 0
    stride=(24 if horizon==24 else 8)*HOUR
    return (when-offset)%stride==0


def prepare():
    paths=json.loads(Path('artifacts/net-expectancy-cycle-complete-runs/source-index.json').read_text())['paths']
    plan=adaptive.prepare(paths)
    old=json.loads(Path('artifacts/research-registry/flow-expectancy-stage7-plan.json').read_text())
    plan.update(schema='event-information-plan/v1',stage=10,
        hypothesis='lagged leverage and spot flow may be informative near funding publication rather than UTC midnight',
        horizons=[3,6,24],schedules=['clock','settlement'],families=['price_control','auxiliary'],
        feature_names=list(NAMES+AUX),funding=old['funding'],flow=old['flow'],
        event_schedule='settlement: 02,10,18 UTC for 3/6h; 02 UTC for 24h; clock control: 00,08,16 or 00 UTC',
        availability='funding requires >=2h since event; flow complete with >=1h lag; cadence validated; no gaps filled',
        learner='ridge L2=1 with training-only scaling; fixed price control vs three auxiliary fields; no selection/tuning',
        sampling='full horizon purge; outcomes non-overlapping within each schedule; schedules are correlated comparisons',
        inference='10000 within-fold circular shifts and whole-period bootstrap; 12-test correction; reused development exploratory only',
        screen='all tests disclosed; no policy fitting or confirmation access; positive information alone cannot qualify trading')
    plan['implementation_sha256'].update({p:hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in
        ('trading/app/event_information.py','trading/app/signal_information.py','trading/app/funding_expectancy.py','trading/app/flow_expectancy.py')})
    return plan


def project(row,auxiliary):
    return {**row,'features':row['features'] if auxiliary else row['features'][:3]}


def run(plan,folder):
    if prepare()!=plan:raise ValueError('frozen event protocol changed')
    folder=Path(folder);folder.mkdir(exist_ok=False)
    raw=Path(plan['funding']['path']).read_bytes()
    if hashlib.sha256(raw).hexdigest()!=plan['funding']['file_sha256']:raise ValueError('funding hash changed')
    events=json.loads(raw);funding.validate_funding(events);events=events['events']
    flow_rows=[]
    for source in plan['flow']['sources']:
        raw=Path(source['path']).read_bytes()
        if hashlib.sha256(raw).hexdigest()!=source['file_sha256']:raise ValueError('flow hash changed')
        data=json.loads(raw);flow.validate_flow(data);flow_rows.extend(data['rows'])
    cache={};reference={};coverage={}
    for source_id,source in enumerate(plan['sources']):
        bars,hashes=load_historical_candles(source['path'])
        if hashes!=source['hashes']:raise ValueError('development price source changed')
        for h in plan['horizons']:
            sampled=r.samples(bars,h,3600)
            for schedule in plan['schedules']:
                rows=[];expected=0
                for row in sampled:
                    when=row['decision_ms']
                    if not event_due(when,h,schedule):continue
                    expected+=1
                    if when not in cache:
                        f=funding.funding_features(events,when);q=flow.flow_features(flow_rows,when)
                        # The clock control intentionally cannot see a just-settled rate.
                        cache[when]=[f[3],q[1],q[2]] if f is not None and q is not None else None
                    if cache[when] is None:continue
                    if schedule=='settlement':
                        last=max(e['fundingTime'] for e in events if e['fundingTime']<=when-funding.LAG)
                        if when-last<2*HOUR-60000:raise ValueError('funding publication guard failed')
                    rows.append({**row,'features':[row['features'][i] for i in (2,6,16)]+cache[when]})
                reference[source_id,h,schedule]=rows
                coverage[f'{source_id}/{h}/{schedule}']={'expected':expected,'available':len(rows)}
    results={}
    for h in plan['horizons']:
        for schedule in plan['schedules']:
            pooled=sorted([row for i in range(len(plan['sources'])) for row in reference[i,h,schedule]],key=lambda row:row['decision_ms'])
            groups={family:[] for family in plan['families']};predictions={family:[] for family in groups}
            for number,fold in enumerate(plan['folds']):
                train=adaptive.purged_rows(pooled,fold['training_start_ms'],fold['training_end_ms'])
                validation=[row for row in reference[fold['source_id'],h,schedule]
                    if fold['validation_start_ms']<=row['decision_ms'] and row['label_end_ms']<fold['validation_end_ms']]
                if any(a['label_end_ms']>b['decision_ms'] for a,b in zip(validation,validation[1:])):
                    raise ValueError('event labels overlap')
                if len(train)<100 or len(validation)<2:continue
                for family in groups:
                    auxiliary=family=='auxiliary';names=NAMES+AUX if auxiliary else NAMES
                    model=r.fit_model([project(row,auxiliary) for row in train],'ridge',3600,names)
                    assert model['last_training_label_ms']<fold['validation_start_ms']
                    write_new(folder/f'h{h}-{schedule}-{family}-fold-{number}-model.json',model)
                    scores=[r.predict(model,project(row,auxiliary)['features'],3600,names) for row in validation]
                    outcomes=[row['net_return_target'] for row in validation]
                    groups[family].append({'scores':scores,'outcomes':outcomes})
                    predictions[family].extend({'fold':number,'decision_ms':row['decision_ms'],'label_end_ms':row['label_end_ms'],
                        'forecast':score,'net_return':row['net_return_target'],'training_mean':model['target_mean']}
                        for row,score in zip(validation,scores))
            key=f'h{h}-{schedule}';summary={}
            for family in groups:
                records=predictions[family]
                metrics=diagnostic.information(groups[family]);p=metrics.get('two_sided_circular_shift_p')
                metrics['twelve_test_bonferroni_p']=min(1.,p*12) if p is not None else None
                metrics['mse_vs_training_mean_improvement']=statistics.fmean(
                    (row['net_return']-row['training_mean'])**2-(row['net_return']-row['forecast'])**2 for row in records) if records else None
                metrics['positive_net_fraction']=statistics.fmean(row['net_return']>0 for row in records) if records else None
                summary[family]=metrics
            if len(predictions['price_control'])!=len(predictions['auxiliary']):raise ValueError('paired information comparison misaligned')
            summary['auxiliary_minus_price_mse_improvement']=statistics.fmean(
                (a['net_return']-a['forecast'])**2-(b['net_return']-b['forecast'])**2
                for a,b in zip(predictions['price_control'],predictions['auxiliary'])) if predictions['auxiliary'] else None
            write_new(folder/f'{key}-predictions.json',predictions);results[key]=summary
            print(key,json.dumps(summary),flush=True)
    result={'schema':'event-information-results/v1','plan_sha256':r.digest(plan),'results':results,'coverage':coverage,
        'new_candidate_qualified':False,'protected_observations_accessed':False,'orders_executed':False,'production_changed':False}
    result['result_sha256']=r.digest(result);write_new(folder/'results.json',result);return result


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('action',choices=('prepare','run'))
    parser.add_argument('plan');parser.add_argument('--output');args=parser.parse_args(argv)
    if args.action=='prepare':write_new(args.plan,prepare())
    else:
        if not args.output:parser.error('new output directory required')
        run(json.loads(Path(args.plan).read_text()),args.output)
    return 0


if __name__=='__main__':raise SystemExit(main())
