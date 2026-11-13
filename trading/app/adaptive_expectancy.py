"""Stage 4: select model and holding period only inside purged training history."""
import argparse
import hashlib
import json
import math
from pathlib import Path
import random
import statistics

from . import net_expectancy as r
from .backtest import BacktestConfig,HistoricalBacktester,load_historical_candles
from .cost_research import write_new


def interval(runs,replicates=30000,alpha=.000625):
    if len(runs)<6:return None
    randomizer=random.Random(0);means=[]
    for _ in range(replicates):
        draw=[]
        for _ in runs:
            run=randomizer.choice(runs);block=min(7,len(run));sample=[]
            while len(sample)<len(run):
                start=randomizer.randrange(len(run)-block+1);sample.extend(run[start:start+block])
            draw.extend(sample[:len(run)])
        means.append(statistics.fmean(draw))
    means.sort();tail=alpha/2
    return {'lower':means[int(replicates*tail)],'upper':means[int(replicates*(1-tail))],
        'replicates':replicates,'seed':0,'familywise_alpha':alpha,'outer_candidates':1,
        'method':'resample_whole_validation_periods_then_7day_moving_blocks',
        'caveat':'conditional development evidence; one untouched confirmation and forward execution still required'}


def prepare(paths,symbol='BTC/USDT'):
    if symbol not in ('BTC/USDT','ETH/USDT'):raise ValueError('unsupported research market')
    sources=[];folds=[]
    for path in paths:
        path=Path(path).resolve()
        if not any(name in str(path) for name in ('net-expectancy-cycle-complete-runs','net-expectancy-eth-cycle-complete-runs')) or any(s in str(path).lower()
                for s in ('confirmation','segment7','segment-7','segment_7')):
            raise ValueError('explicit cycle development derivatives required')
        bars,hashes=load_historical_candles(path,symbol=symbol)
        if hashes['qualification_eligible']!='true':raise ValueError('incomplete cycle source')
        r.features(bars[:289],3600)
        start=int(bars[0].open_time.timestamp()*1000);end=int(bars[-1].close_time.timestamp()*1000)
        if end>1740787200000:raise ValueError('development exceeds 2025-03-01')
        if sources and start<sources[-1]['end_ms']:raise ValueError('non-chronological or duplicate sources')
        source_id=len(sources);sources.append({'path':str(path),'hashes':hashes,'start_ms':start,'end_ms':end})
        validation=max(start+12*r.DAY,sources[0]['start_ms']+377*r.DAY)
        # Daily decisions use UTC midnight, never a calendar chosen from returns.
        validation=((validation+r.DAY-1)//r.DAY)*r.DAY
        while validation+30*r.DAY<=end:
            folds.append({'source_id':source_id,'training_start_ms':validation-365*r.DAY,
                'training_end_ms':validation,'inner_start_ms':validation-90*r.DAY,
                'validation_start_ms':validation,'validation_end_ms':validation+30*r.DAY})
            validation+=30*r.DAY
    if not folds:raise ValueError('no full nested walk-forward periods')
    return {'schema':'adaptive-expectancy-plan/v1','stage':4,'symbol':symbol,'sources':sources,'folds':folds,
        'inner_candidates':[{'kind':kind,'horizon_bars':h} for h in (24,72,168)
            for kind in ('ridge','tree','logistic_payoff','regime')],
        'outer_candidates':1,'sampling':'UTC-midnight daily; closed hourly context; full label-horizon purge',
        'feature_schema':r.FEATURE_SCHEMA,'feature_names':list(r.FEATURES),
        'costs':{k:list(v) for k,v in r.COSTS.items()},'entry_floor':'.001 + .1 * training_target_std',
        'selection_rule':'fit before inner 90 days; maximize worst-scenario daily net excess over 10pct hold; require positive net, positive excess, >=3 inner trades, >=60 covered inner days; otherwise cash',
        'refit':'selected kind/horizon only, on all eligible prior 365-day training rows; never outer labels',
        'minimum_training_examples':100,'alpha_spending':{'stage':4,'allocation':.000625,'total_budget':.01},
        'acceptance':{'minimum_folds':12,'minimum_trades_each_cost':60,'positive_fold_fraction':2/3,
            'positive_net_and_10pct_hold_excess':True,'max_drawdown':.10,'daily_return_interval_lower_positive':True},
        'reserved_new_confirmation':{'start':'2025-03-01T00:00:00Z','end_exclusive':'2025-05-01T00:00:00Z',
            'acquired':False,'accessed':False},
        'implementation_sha256':{p:hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in (
            'trading/app/adaptive_expectancy.py','trading/app/net_expectancy.py','trading/app/backtest.py',
            'trading/app/model_training.py','trading/app/dataset_eligibility.py','trading/app/historical_candles.py',
            'trading/core/money.py','trading/core/config.py','trading/strategy/context.py','trading/strategy/signals.py')},
        'orders_executed':False,'production_changed':False,'protected_observations_accessed':False,
        'limitations':['training pools only individually complete pieces; 12-day feature warm-up resets at every gap',
            'validation is independent per contiguous period; no prices or positions cross missing history',
            'missing early history and short segments are retained in acquisition evidence, not silently filled',
            'bootstrap is conditional on available market regimes; fees are scenarios, taxes/TDS and queues omitted']}


def select_inner(evaluations):
    eligible=[row for row in evaluations if row.get('eligible') and math.isfinite(row['score']) and row['score']>0]
    return max(eligible,key=lambda row:(row['score'],row['kind'],-row['horizon_bars'])) if eligible else None


def purged_rows(rows,start,cutoff):
    return [row for row in rows if start<=row['decision_ms'] and row['label_end_ms']<cutoff]


def execute(plan,folder,*,validator,sample_builder=None):
    if validator(plan)!=plan:raise ValueError('frozen adaptive protocol changed')
    folder=Path(folder);folder.mkdir(exist_ok=False)
    symbol=plan.get('symbol','BTC/USDT');asset=symbol.split('/')[0]
    series=[load_historical_candles(s['path'],symbol=symbol)[0] for s in plan['sources']]
    horizons=sorted({candidate['horizon_bars'] for candidate in plan['inner_candidates']})
    reference={(i,h):r.samples(bars,h,3600,24) for i,bars in enumerate(series) for h in horizons}
    examples=sample_builder(reference) if sample_builder else reference
    pooled={h:sorted([row for i in range(len(series)) for row in examples[i,h]],key=lambda row:row['decision_ms']) for h in horizons}
    def simulate(model,h,predictions,source_id,start,end,scenario):
        cost=plan['costs']['base' if scenario=='base' else 'stress']
        bars=[b for b in series[source_id] if start-289*3600000<=int(b.open_time.timestamp()*1000)<end]
        strategy=r.ExpectancyStrategy(model,h,predictions)
        report=dict(HistoricalBacktester(strategy,config=BacktestConfig(symbol=symbol,asset=asset,fee_bps=cost[0],slippage_bps=cost[1],
            spread_bps=cost[2],execution_timing='next_close' if scenario=='delayed_stress' else 'next_open')).run(
            bars,dataset_hashes=plan['sources'][source_id]['hashes'],trade_start_ms=start,trade_end_ms=end).report)
        daily=r.daily_returns(report.pop('equity_curve'));report.pop('report_sha256')
        return {'report':report,'daily_returns':daily,'opportunities':strategy.opportunities,'no_trade_decisions':strategy.filtered}
    folds=[]
    for number,fold in enumerate(plan['folds']):
        evaluations=[];inner_panels=[];inner_models={}
        for source_id,source in enumerate(plan['sources']):
            start=max(source['start_ms']+12*r.DAY,fold['inner_start_ms']);end=min(source['end_ms'],fold['training_end_ms'])
            if end-start>=7*r.DAY:inner_panels.append((source_id,start,end))
        covered=sum((end-start)/r.DAY for _,start,end in inner_panels)
        for candidate in plan['inner_candidates']:
            kind,h=candidate['kind'],candidate['horizon_bars']
            train=purged_rows(pooled[h],fold['training_start_ms'],fold['inner_start_ms'])
            entry={**candidate,'training_samples':len(train),'covered_inner_days':covered,'eligible':False}
            if len(train)<100 or covered<60:
                entry['reason']='insufficient purged inner training or evaluation coverage';evaluations.append(entry);continue
            try:
                if (kind,h) not in inner_models:inner_models[kind,h]=r.fit_model(train,kind,3600,tuple(plan['feature_names']))
                model=dict(inner_models[kind,h])
            except ValueError as exc:
                entry['reason']='inner fitting refused: '+str(exc);evaluations.append(entry);continue
            model['symbol']=symbol
            if 'full_risk' in candidate:model['research_full_risk']=candidate['full_risk']
            if candidate.get('entry_floor') is not None:model['research_entry_floor']=candidate['entry_floor']
            model.pop('model_sha256');model['model_sha256']=r.digest(model)
            assert model['last_training_label_ms']<fold['inner_start_ms']
            scenarios={}
            for scenario in ('base','stress','delayed_stress'):
                reports=[]
                for source_id,start,end in inner_panels:
                    rows=[row for row in examples[source_id,h] if start<=row['decision_ms'] and row['label_end_ms']<end]
                    predictions={row['decision_ms']:r.predict(model,row['features'],3600,tuple(plan['feature_names'])) for row in rows}
                    reports.append(simulate(model,h,predictions,source_id,start,end,scenario))
                metrics=[rpt['report']['metrics'] for rpt in reports]
                scenarios[scenario]={'net_per_day':sum(float(m['total_return']) for m in metrics)/covered,
                    'excess_per_day':sum(float(m['total_return'])-.1*float(m['buy_hold_benchmark_return_after_costs']) for m in metrics)/covered,
                    'trades':sum(m['trade_count'] for m in metrics)}
            entry.update(scenarios=scenarios,score=min(s['excess_per_day'] for s in scenarios.values()),
                eligible=all(s['net_per_day']>0 and s['excess_per_day']>0 and s['trades']>=3 for s in scenarios.values()),
                inner_model_sha256=model['model_sha256'])
            evaluations.append(entry)
        selected=select_inner(evaluations)
        entry={'fold':fold,'inner_candidates':evaluations,'selected':selected,'scenarios':{}}
        if selected:
            h=selected['horizon_bars'];kind=selected['kind']
            train=purged_rows(pooled[h],fold['training_start_ms'],fold['training_end_ms'])
            model=r.fit_model(train,kind,3600,tuple(plan['feature_names']));model.pop('model_sha256')
            model['symbol']=symbol
            if 'full_risk' in selected:model['research_full_risk']=selected['full_risk']
            if selected.get('entry_floor') is not None:model['research_entry_floor']=selected['entry_floor']
            model.update(horizon_bars=h,selection_cutoff_ms=fold['training_end_ms'],fold=fold,
                source_hashes=[s['hashes'] for s in plan['sources']],decision_stride_hours=24,
                auxiliary_source_sha256=plan.get('funding',{}).get('file_sha256'),
                auxiliary_flow_bundle_sha256=plan.get('flow',{}).get('bundle_sha256'))
            model['model_sha256']=r.digest(model);assert model['last_training_label_ms']<fold['validation_start_ms']
            write_new(folder/f'fold-{number}-selected-model.json',model)
            validation=[row for row in examples[fold['source_id'],h] if fold['validation_start_ms']<=row['decision_ms'] and row['label_end_ms']<fold['validation_end_ms']]
            predictions={row['decision_ms']:r.predict(model,row['features'],3600,tuple(plan['feature_names'])) for row in validation}
            entry.update(model_sha256=model['model_sha256'],training_samples=len(train),
                predictions=[{'decision_ms':when,'expected_net_return':score} for when,score in predictions.items()])
        else:
            h=24;model={'kind':'cash','target_std':0.,'interval_seconds':3600,'symbol':symbol};predictions={}
            entry['reason']='no inner candidate demonstrated positive cost-stressed benchmark excess; stay in cash'
        for scenario in ('base','stress','delayed_stress'):
            entry['scenarios'][scenario]=simulate(model,h,predictions,fold['source_id'],fold['validation_start_ms'],fold['validation_end_ms'],scenario)
        if plan.get('funding'):
            expected=[row for row in reference[fold['source_id'],h] if fold['validation_start_ms']<=row['decision_ms'] and row['label_end_ms']<fold['validation_end_ms']]
            available=[row for row in examples[fold['source_id'],h] if fold['validation_start_ms']<=row['decision_ms'] and row['label_end_ms']<fold['validation_end_ms']]
            entry['auxiliary_feature_coverage']=len(available)/len(expected) if expected else 0.
        write_new(folder/f'fold-{number}-evaluation.json',entry);folds.append(entry)
        print('adaptive fold',number,'choice',None if selected is None else (selected['kind'],h),
              'base net',entry['scenarios']['base']['report']['metrics']['total_return'],flush=True)
    summary={}
    for scenario in ('base','stress','delayed_stress'):
        metrics=[f['scenarios'][scenario]['report']['metrics'] for f in folds]
        summary[scenario]={'mean_return':statistics.fmean(float(m['total_return']) for m in metrics),
            'mean_excess_10pct_hold':statistics.fmean(float(m['total_return'])-.1*float(m['buy_hold_benchmark_return_after_costs']) for m in metrics),
            'trades':sum(m['trade_count'] for m in metrics),'positive_folds':sum(float(m['total_return'])>0 for m in metrics),
            'max_drawdown':max(float(m['maximum_drawdown']) for m in metrics),
            'interval':interval([f['scenarios'][scenario]['daily_returns'] for f in folds],
                replicates=plan.get('bootstrap_replicates',30000),alpha=plan['alpha_spending']['allocation'])}
    checks={'coverage':len(folds)>=12,'trades':all(s['trades']>=60 for s in summary.values()),
        'positive_periods':all(s['positive_folds']>=math.ceil(2*len(folds)/3) for s in summary.values()),
        'net_and_excess':all(s['mean_return']>0 and s['mean_excess_10pct_hold']>0 for s in summary.values()),
        'drawdown':all(s['max_drawdown']<=.10 for s in summary.values()),
        'intervals':all(s['interval'] and s['interval']['lower']>0 for s in summary.values())}
    if plan.get('funding'):checks['funding_coverage']=all(f['auxiliary_feature_coverage']>=.95 for f in folds)
    result={'schema':'adaptive-expectancy-results/v1','stage':plan['stage'],'plan_sha256':r.digest(plan),'summary':summary,'checks':checks,
        'development_qualified':all(checks.values()),'cash_folds':sum(f['selected'] is None for f in folds),
        'fold_evidence':[f'fold-{i}-evaluation.json' for i in range(len(folds))],
        'confirmation_accessed':False,'segment_7_accessed':False,'orders_executed':False,'production_changed':False}
    result['result_sha256']=r.digest(result);write_new(folder/'results.json',result);return result


def run(plan,folder):
    return execute(plan,folder,validator=lambda value:prepare([s['path'] for s in value['sources']],value.get('symbol','BTC/USDT')))


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('prepare','run'));parser.add_argument('plan')
    parser.add_argument('--source',action='append');parser.add_argument('--output');args=parser.parse_args(argv)
    if args.action=='prepare':
        if not args.source:parser.error('complete cycle development paths required')
        write_new(args.plan,prepare(args.source))
    else:
        if not args.output:parser.error('new output directory required')
        run(json.loads(Path(args.plan).read_text()),args.output)
    return 0


if __name__=='__main__':raise SystemExit(main())
