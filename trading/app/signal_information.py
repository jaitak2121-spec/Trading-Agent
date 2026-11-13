"""Preregistered frozen-forecast information/cost diagnostics, never a trader."""
import argparse
from decimal import Decimal
import hashlib
import json
import math
from pathlib import Path
import random
import statistics

from .backtest import load_historical_candles
from .cost_research import write_new
from . import net_expectancy as r

REGISTRY='artifacts/research-registry/net-expectancy-registry-v4.json'
REGISTRY_HASH='a1cf2860add0f308ab19942df2cb0df7456b1e7e1b2b901de0a30aae7b456e46'
PLAN='artifacts/research-registry/hourly-expectancy-stage2-plan.json'


def moments(x,y):
    if len(x)!=len(y) or len(x)<2:raise ValueError('aligned nontrivial pairs required')
    mx,my=statistics.fmean(x),statistics.fmean(y)
    return (sum((a-mx)*(b-my) for a,b in zip(x,y)),sum((a-mx)**2 for a in x),sum((b-my)**2 for b in y))


def correlation(groups):
    totals=[sum(row[i] for row in groups) for i in range(3)]
    return totals[0]/math.sqrt(totals[1]*totals[2]) if min(totals[1:])>0 else None


def nonoverlapping_rows(rows,horizon):
    if type(horizon) is not int or horizon<=0:raise ValueError('positive integer horizon required')
    selected=[]
    for row in rows:
        at,end=row['decision_ms'],row['label_end_ms']
        if type(at) is not int or type(end) is not int or end-at!=horizon*3600000:
            raise ValueError('explicit matching label boundary required')
        if at%(horizon*3600000):continue
        if selected and at<selected[-1]['label_end_ms']:raise ValueError('duplicate or overlapping outcomes')
        selected.append(row)
    return selected


def before_fee_return(net,executable,fee=.001):
    if not math.isfinite(net) or not 0<=fee<1:raise ValueError('invalid return or fee')
    return (net+1)*(1+fee)/(1-fee)-1 if executable else 0.


def information(groups,replicates=10000):
    groups=[g for g in groups if len(g['scores'])>=2]
    if not groups:return {'within_fold_ic':None,'reason':'insufficient groups'}
    parts=[moments(g['scores'],g['outcomes']) for g in groups]
    observed=correlation(parts)
    if observed is None:return {'within_fold_ic':None,'reason':'constant scores or outcomes'}
    randomizer=random.Random(0);null=[];boot=[]
    for _ in range(replicates):
        shifted=[]
        for g in groups:
            offset=randomizer.randrange(len(g['scores']))
            x=g['scores'][offset:]+g['scores'][:offset]
            shifted.append(moments(x,g['outcomes']))
        null.append(correlation(shifted))
        value=correlation([randomizer.choice(parts) for _ in parts])
        if value is not None:boot.append(value)
    boot.sort()
    return {'within_fold_ic':observed,'two_sided_circular_shift_p':
        (1+sum(abs(v)>=abs(observed) for v in null))/(replicates+1),
        'cluster_bootstrap_95_interval':[boot[int(.025*len(boot))],boot[min(len(boot)-1,int(.975*len(boot)))]] if boot and len(boot)>=.95*replicates else None,
        'degenerate_cluster_replicates':replicates-len(boot),
        'nonoverlapping_samples':sum(len(g['scores']) for g in groups),'periods':len(groups),
        'replicates':replicates,'seed':0}


def prepare():
    raw=Path(REGISTRY).read_bytes()
    if hashlib.sha256(raw).hexdigest()!=REGISTRY_HASH:raise ValueError('frozen registry changed')
    registry=json.loads(raw)
    if registry['registry_sha256']!=r.digest({k:v for k,v in registry.items() if k!='registry_sha256'}):raise ValueError('registry content mismatch')
    study=next(e for e in registry['experiments'] if e['id']=='stage2-native-hourly')
    source=json.loads(Path(PLAN).read_text())
    return {'schema':'frozen-signal-information-plan/v1','scope':'exploratory development diagnostics; no qualification or promotion',
        'registry_sha256':REGISTRY_HASH,'study':study,'source_plan':source,
        'groups':[{'kind':kind,'horizon':h} for h in (24,72) for kind in ('ridge','tree','logistic_payoff')],
        'sampling':'UTC epoch modulo full horizon, no label overlap; same indices for all learners',
        'score_bins':'terciles defined by frozen-model scores on its own purged training rows only',
        'regimes':'48h return positive/nonpositive x volatility_48 above/below training median',
        'controls':'momentum baseline; within-fold centered IC; independent circular score shifts within each fold',
        'inference':'10000 shifts and period bootstrap; six-test Bonferroni; exploratory reused development, not confirmation',
        'costs':'base 10/2/2 bp; same-fill before-fee diagnostic; raw no-stop next-open horizon comparison',
        'implementation_sha256':{p:hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in
            ('trading/app/signal_information.py','trading/app/net_expectancy.py','trading/app/backtest.py')},
        'models_refitted':False,'thresholds_optimized':False,'protected_observations_accessed':False,'orders_executed':False}


def run(plan,folder):
    if prepare()!=plan:raise ValueError('frozen diagnostic protocol changed')
    folder=Path(folder);folder.mkdir(exist_ok=False)
    original=plan['source_plan'];sources=[]
    for source in original['sources']:
        bars,hashes=load_historical_candles(source['path'])
        if hashes!=source['hashes']:raise ValueError('frozen development source changed')
        sources.append(bars)
    rows={(i,h):r.samples(bars,h,3600) for i,bars in enumerate(sources) for h in (24,72)}
    by_time=[{int(b.close_time.timestamp()*1000):i for i,b in enumerate(bars)} for bars in sources]
    model_records={Path(m['path']).name:m for m in plan['study']['saved_models']}
    eval_records={Path(m['path']).name:m for m in plan['study']['evaluations']}
    results={}
    for specification in plan['groups']:
        kind,h=specification['kind'],specification['horizon'];name=f'{kind}-h{h}'
        grouped=[];gross_grouped=[];momentum=[];cells={};all_rows=[];prediction_checks=0
        for number,fold in enumerate(original['folds']):
            model_path=Path(f'artifacts/hourly-expectancy-stage2-results/{name}-fold-{number}-model.json')
            model_raw=model_path.read_bytes();model=json.loads(model_raw)
            if hashlib.sha256(model_raw).hexdigest()!=model_records[model_path.name]['file_sha256']:raise ValueError('frozen model changed')
            eval_path=Path(f'artifacts/hourly-expectancy-stage2-results/{name}-selective-fold-{number}-evaluation.json')
            eval_raw=eval_path.read_bytes();evaluation=json.loads(eval_raw)
            if hashlib.sha256(eval_raw).hexdigest()!=eval_records[eval_path.name]['file_sha256']:raise ValueError('frozen evaluation changed')
            train,validation=r.split_samples(rows[fold['source_id'],h],fold)
            if r.digest(train)!=model['training_rows_sha256']:raise ValueError('training provenance mismatch')
            training_scores=sorted(r.predict(model,row['features'],3600) for row in train)
            cuts=[training_scores[len(training_scores)//3],training_scores[2*len(training_scores)//3]]
            volatility_cut=statistics.median(row['features'][6] for row in train)
            saved={row['decision_ms']:row['expected_net_return'] for row in evaluation['predictions']}
            group={'scores':[],'outcomes':[]};gross={'scores':[],'outcomes':[]};control={'scores':[],'outcomes':[]}
            bars=sources[fold['source_id']]
            for row in nonoverlapping_rows(validation,h):
                when=row['decision_ms']
                score=r.predict(model,row['features'],3600)
                if abs(score-saved[when])>1e-12:raise ValueError('saved forecast cannot be reproduced')
                prediction_checks+=1;i=by_time[fold['source_id']][when]
                entry=bars[i+1].open.amount*Decimal('1.0001')*Decimal('1.0002')
                stop=r.Price.rounded(bars[i].close.amount*(1-Decimal(str(r.stop_fraction(row['features'],h)))),bars[i].close.currency).amount
                executable=entry>stop
                net=row['net_return_target']
                pre_fee=before_fee_return(net,executable)
                raw=float(bars[i+h+1].open.amount/bars[i+1].open.amount-1)
                bin_id=0 if score<cuts[0] else 1 if score<cuts[1] else 2
                regime=('up' if row['features'][3]>0 else 'nonpositive')+'-'+('highvol' if row['features'][6]>volatility_cut else 'lowvol')
                cell=f'{regime}/tercile-{bin_id}'
                point={'fold':number,'decision_ms':when,'label_end_ms':row['label_end_ms'],'forecast':score,
                       'net_stop_return':net,'same_fill_pre_fee_return':pre_fee,'raw_no_stop_return':raw,
                       'executable_at_next_open':executable,'score_tercile':bin_id,'regime':regime}
                all_rows.append(point);cells.setdefault(cell,[]).append(point)
                group['scores'].append(score);group['outcomes'].append(net)
                gross['scores'].append(score);gross['outcomes'].append(raw)
                control['scores'].append(row['features'][3]);control['outcomes'].append(net)
            grouped.append(group);gross_grouped.append(gross);momentum.append(control)
        summaries={}
        for cell,values in cells.items():
            active=[p for p in values if p['executable_at_next_open']]
            mean_pre=statistics.fmean(p['same_fill_pre_fee_return'] for p in active) if active else None
            summaries[cell]={'samples':len(values),'periods':len({p['fold'] for p in values}),
                'mean_net_stop_return':statistics.fmean(p['net_stop_return'] for p in values),
                'mean_same_fill_pre_fee_return':mean_pre,'mean_raw_no_stop_return':statistics.fmean(p['raw_no_stop_return'] for p in values),
                'positive_net_fraction':statistics.fmean(p['net_stop_return']>0 for p in values),
                'descriptive_break_even_fee_bps':10000*mean_pre/(2+mean_pre) if mean_pre is not None and mean_pre>0 else None,
                'not_a_validated_trading_rule':True}
        result={'frozen_forecast_net_information':information(grouped),'raw_no_stop_information':information(gross_grouped),
                'momentum_control':information(momentum),'conditional_cells':summaries,'predictions_reproduced':prediction_checks,
                'positive_net_opportunity_fraction':statistics.fmean(p['net_stop_return']>0 for p in all_rows),
                'mean_net_stop_return':statistics.fmean(p['net_stop_return'] for p in all_rows),
                'mean_same_fill_pre_fee_return':statistics.fmean(p['same_fill_pre_fee_return'] for p in all_rows),
                'samples':all_rows}
        write_new(folder/f'{name}.json',result);results[name]={k:v for k,v in result.items() if k!='samples'}
        print(name,json.dumps(result['frozen_forecast_net_information']),flush=True)
    for result in results.values():
        stats=result['frozen_forecast_net_information']
        p=stats.get('two_sided_circular_shift_p');stats['six_test_bonferroni_p']=min(1.,p*6) if p is not None else None
    final={'schema':'frozen-signal-information-results/v1','plan_sha256':r.digest(plan),'results':results,
        'new_candidate_qualified':False,'models_refitted':False,'protected_observations_accessed':False,'orders_executed':False}
    final['result_sha256']=r.digest(final);write_new(folder/'results.json',final);return final


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('action',choices=('prepare','run'))
    parser.add_argument('plan');parser.add_argument('--output');args=parser.parse_args(argv)
    if args.action=='prepare':write_new(args.plan,prepare())
    else:
        if not args.output:parser.error('new diagnostic output directory required')
        run(json.loads(Path(args.plan).read_text()),args.output)
    return 0


if __name__=='__main__':raise SystemExit(main())
