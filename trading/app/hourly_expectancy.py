"""Stage 2: native-hourly long-horizon ML with isolated chronological selection."""
import argparse
import hashlib
import json
import math
from pathlib import Path
import statistics

from . import net_expectancy as research
from .backtest import BacktestConfig,HistoricalBacktester,load_historical_candles
from .cost_research import write_new

END=1740787200000 # 2025-03-01 UTC; new March/April confirmation stays unopened.


def prepare(paths):
    sources=[];folds=[]
    for path in paths:
        path=Path(path).resolve()
        if any(s in str(path).lower() for s in ('confirmation','segment7','segment-7','segment_7','ml-research-v2')):
            raise ValueError('protected paths forbidden')
        # Hourly artifacts are a separately acquired, explicitly named source;
        # never open the old minute source containing protected observations.
        if 'net-expectancy-hourly-complete-runs' not in str(path):
            raise ValueError('explicit hourly complete development derivatives required')
        bars,hashes=load_historical_candles(path)
        if hashes['qualification_eligible']!='true':raise ValueError('incomplete hourly data')
        research.features(bars[:289],3600)
        start=int(bars[0].open_time.timestamp()*1000);end=int(bars[-1].close_time.timestamp()*1000)
        if end>END:raise ValueError('confirmation period forbidden in development')
        if sources and start<sources[-1]['end_ms']:raise ValueError('overlapping or reversed sources')
        source_id=len(sources);sources.append({'path':str(path),'hashes':hashes,'start_ms':start,'end_ms':end})
        validation=start+72*research.DAY # 12 days context, then 60 days training.
        while validation+15*research.DAY<=end:
            folds.append({'source_id':source_id,'training_start_ms':validation-60*research.DAY,
                'training_end_ms':validation,'validation_start_ms':validation,
                'validation_end_ms':validation+15*research.DAY})
            validation+=15*research.DAY
    if not folds:raise ValueError('no full hourly development folds')
    candidates=[{'kind':kind,'horizon_bars':h,'entry_policy':policy}
                for h in (24,72) for kind in ('ridge','tree','logistic_payoff')
                for policy in ('selective','trend_selective')]
    return {'schema':'hourly-expectancy-plan/v1','stage':2,'sources':sources,'folds':folds,'candidates':candidates,
        'feature_schema':research.FEATURE_SCHEMA,'native_interval_seconds':3600,'feature_names':list(research.FEATURES),
        'feature_units':'lookbacks in native hours; 289 completed consecutive candles; training-only scaling',
        'costs':{k:list(v) for k,v in research.COSTS.items()},
        'additional_execution_stress':'same stress fees/spread/slippage, entry and exit delayed to next native close',
        'entry_floor':'.001 + .1 * training_target_std','trend_filter':'48-hour return > 0, fixed before fitting',
        'risk':'existing 1000 USDT notional/position/exposure caps; 100 daily loss; .5% stop risk; conviction .25..1',
        'target':'net after base costs; next open through causal volatility stop or fixed 24/72h horizon',
        'selection_scope':'only these pre-March-2025 hourly development folds; no stage-1 future-period results used',
        'reserved_new_confirmation':{'start':'2025-03-01T00:00:00Z','end_exclusive':'2025-05-01T00:00:00Z',
            'acquired':False,'feature_or_label_accessed':False,'policy':'one frozen candidate, after development qualification'},
        'alpha_spending':{'total_budget':.01,'stage_allocation':.0025,'candidate_count':len(candidates)},
        'acceptance':{'minimum_folds':6,'minimum_trades_all_scenarios':60,'positive_fold_fraction':2/3,
            'maximum_drawdown':.10,'net_returns_and_excess_10pct_hold_positive':True,
            'selection_adjusted_daily_return_interval_lower_positive':True},
        'implementation_sha256':{name:hashlib.sha256(Path(name).read_bytes()).hexdigest() for name in (
            'trading/app/hourly_expectancy.py','trading/app/net_expectancy.py','trading/app/backtest.py',
            'trading/app/model_training.py','trading/app/dataset_eligibility.py','trading/app/historical_candles.py',
            'trading/core/money.py','trading/core/config.py','trading/strategy/context.py','trading/strategy/signals.py')},
        'production_changed':False,'old_confirmation_accessed':False,'segment_7_accessed':False,'orders_executed':False,
        'limitations':['native hourly stop paths unavailable; conservative stop ordering, no minute execution claim',
            'conditional payoff classifier uses training-average payoffs, which may drift',
            'small effective samples for overlapping 72h labels; purge full horizons and bootstrap day blocks',
            'tax/TDS, hidden liquidity, queue, account permissions and realized fill settlement are unmodeled',
            'hourly/cross-regime evidence is distinct from short public book samples; no historical replacement']}


def execute(plan,folder,*,validator=prepare,sample_builder=None):
    if validator(plan)!=plan:raise ValueError('frozen hourly protocol/code/data changed')
    folder=Path(folder);folder.mkdir(exist_ok=False)
    series=[load_historical_candles(s['path'])[0] for s in plan['sources']]
    reference_examples={(i,h):research.samples(bars,h,3600) for i,bars in enumerate(series) for h in (24,72)}
    examples=sample_builder(series,reference_examples) if sample_builder else reference_examples
    fit_cache={};baseline_cache={};results={}
    for candidate in plan['candidates']:
        kind,h,policy=candidate['kind'],candidate['horizon_bars'],candidate['entry_policy']
        name=f'{kind}-h{h}-{policy}';folds=[]
        for number,fold in enumerate(plan['folds']):
            key=(kind,h,number)
            train,validation=research.split_samples(examples[fold['source_id'],h],fold)
            if key not in fit_cache:
                model=research.fit_model(train,kind,3600,tuple(plan['feature_names']));model.pop('model_sha256')
                model.update(horizon_bars=h,target_costs_bps=plan['costs']['base'],fold=fold,
                    source_hashes=plan['sources'][fold['source_id']]['hashes'],
                    auxiliary_source_hashes=[s['hashes'] for s in plan.get('auxiliary_sources',[])],
                    stop_policy='clip(3 * volatility_48_hourly * sqrt(horizon_bars), .01, .10)')
                model['model_sha256']=research.digest(model)
                assert model['last_training_label_ms']<fold['validation_start_ms']
                write_new(folder/f'{kind}-h{h}-fold-{number}-model.json',model)
                predictions={r['decision_ms']:research.predict(model,r['features'],3600,tuple(plan['feature_names'])) for r in validation}
                fit_cache[key]=(model,predictions)
            model,predictions=fit_cache[key]
            bars=[b for b in series[fold['source_id']] if fold['validation_start_ms']-289*3600000
                  <=int(b.open_time.timestamp()*1000)<fold['validation_end_ms']]
            def simulate(entry_policy,cost,timing='next_open'):
                strategy=research.ExpectancyStrategy(model,h,predictions,entry_policy)
                outcome=dict(HistoricalBacktester(strategy,config=BacktestConfig(fee_bps=cost[0],
                    slippage_bps=cost[1],spread_bps=cost[2],execution_timing=timing)).run(bars,
                    dataset_hashes=plan['sources'][fold['source_id']]['hashes'],
                    trade_start_ms=fold['validation_start_ms'],trade_end_ms=fold['validation_end_ms']).report)
                returns=research.daily_returns(outcome.pop('equity_curve'));outcome.pop('report_sha256')
                return {'report':outcome,'daily_returns':returns,'opportunities':strategy.opportunities,
                        'no_trade_decisions':strategy.filtered}
            scenarios={k:simulate(policy,cost) for k,cost in plan['costs'].items()}
            scenarios['delayed_stress']=simulate(policy,plan['costs']['stress'],'next_close')
            if (h,number) not in baseline_cache:
                baseline_cache[h,number]={p:simulate(p,plan['costs']['base'])['report']['metrics']
                    for p in ('always','momentum','contrarian','cash')}
            entry={'fold':fold,'training_samples':len(train),'validation_samples':len(validation),
                'model_sha256':model['model_sha256'],'prediction_sha256':research.digest(predictions),
                'predictions':[{'decision_ms':when,'expected_net_return':score} for when,score in predictions.items()],
                'scenarios':scenarios,'baselines':baseline_cache[h,number]}
            if plan.get('auxiliary_sources'):
                _,expected=research.split_samples(reference_examples[fold['source_id'],h],fold)
                entry['auxiliary_feature_coverage']=len(validation)/len(expected) if expected else 0
            write_new(folder/f'{name}-fold-{number}-evaluation.json',entry);folds.append(entry)
            print(name,'fold',number,'base net',scenarios['base']['report']['metrics']['total_return'],flush=True)
        summary={}
        for scenario in ('base','stress','delayed_stress'):
            metrics=[f['scenarios'][scenario]['report']['metrics'] for f in folds]
            summary[scenario]={'mean_return':statistics.fmean(float(m['total_return']) for m in metrics),
                'mean_excess_10pct_hold':statistics.fmean(float(m['total_return'])-.1*float(m['buy_hold_benchmark_return_after_costs']) for m in metrics),
                'trades':sum(m['trade_count'] for m in metrics),'positive_folds':sum(float(m['total_return'])>0 for m in metrics),
                'max_drawdown':max(float(m['maximum_drawdown']) for m in metrics),
                'daily_return_interval':research.confidence([f['scenarios'][scenario]['daily_returns'] for f in folds],
                    len(plan['candidates']),replicates=30000,familywise_alpha=plan['alpha_spending']['stage_allocation'])}
        checks={'coverage':len(folds)>=6,'trades':all(s['trades']>=60 for s in summary.values()),
            'positive_periods':all(s['positive_folds']>=math.ceil(2*len(folds)/3) for s in summary.values()),
            'net_and_excess':all(s['mean_return']>0 and s['mean_excess_10pct_hold']>0 for s in summary.values()),
            'drawdown':all(s['max_drawdown']<=.10 for s in summary.values()),
            'intervals':all(s['daily_return_interval'] and s['daily_return_interval']['lower']>0 for s in summary.values())}
        if plan.get('auxiliary_sources'):
            checks['auxiliary_coverage']=all(f['auxiliary_feature_coverage']>=.95 for f in folds)
        result={'summary':summary,'checks':checks,'development_qualified':all(checks.values()),
                'fold_evidence':[f'{name}-fold-{n}-evaluation.json' for n in range(len(folds))]}
        write_new(folder/f'{name}-decision.json',result);results[name]=result
    result={'schema':'hourly-expectancy-results/v1','stage':plan['stage'],'plan_sha256':research.digest(plan),'results':results,
        'selected_candidates':[n for n,r in results.items() if r['development_qualified']],
        'reserved_confirmation_accessed':False,'old_confirmation_accessed':False,'segment_7_accessed':False,
        'production_changed':False,'orders_executed':False}
    result['result_sha256']=research.digest(result);write_new(folder/'results.json',result)
    return result


def run(plan,folder):
    return execute(plan,folder,validator=lambda value:prepare([s['path'] for s in value['sources']]))


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('prepare','run'));parser.add_argument('plan')
    parser.add_argument('--source',action='append');parser.add_argument('--output')
    args=parser.parse_args(argv)
    if args.action=='prepare':
        if not args.source:parser.error('explicit hourly sources required')
        write_new(args.plan,prepare(args.source))
    else:
        if not args.output:parser.error('new output directory required')
        run(json.loads(Path(args.plan).read_text()),args.output)
    return 0


if __name__=='__main__':raise SystemExit(main())
