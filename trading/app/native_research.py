"""Preregistered native-5m complete-run development research; never deploys."""
import argparse
import hashlib
import json
import math
import platform
from pathlib import Path
from decimal import Decimal

from .backtest import BacktestConfig,BacktestFold,HistoricalBacktester,load_historical_candles
from .complete_runs import derive_complete_runs
from .cost_research import bootstrap,write_new
from .dataset_eligibility import assess_dataset,require_qualification_eligibility
from ..strategy.examples import MovingAverageCrossover
from ..strategy.research import HistoricalRuleStrategy
from ..strategy.cost_aware import CostAwareRule
from ..core.config import RiskConfig
from ..core.money import Money,USDT

ROOT=Path(__file__).resolve().parents[2]
PARENT=ROOT/'artifacts/btc-usdt-5m-2025-05-06_2025-11-02-development.json'
PARENT_HASH='e7aca21f39fac23a2340ae0c22d5bad8a06bdbd063573055adcc3336579eda2f'
DAY=86400000
COSTS={'base':('10','2','2'),'stress':('20','5','4')}


def candidates():
    return {'ma-crossover':lambda:MovingAverageCrossover(),
        **{rule:(lambda r=rule:HistoricalRuleStrategy(r)) for rule in ('momentum','mean-reversion','volatility-filtered-momentum')},
        **{rule:(lambda r=rule:CostAwareRule(r)) for rule in ('breakout','trend-pullback')}}


def specification(derived):
    paths=['trading/app/native_research.py','trading/app/complete_runs.py','trading/app/backtest.py',
        'trading/app/dataset_eligibility.py','trading/app/cost_research.py','trading/strategy/cost_aware.py',
        'trading/strategy/examples.py','trading/strategy/research.py','trading/strategy/indicators.py',
        'trading/strategy/context.py','trading/core/money.py','trading/core/config.py']
    folds=[]
    for run in derived:
        start=run['source']['requested_start_time'];end=run['source']['requested_end_time']
        validation=start+30*DAY
        while validation+15*DAY<=end:
            folds.append({'run_id':run['development_derivation']['run_id'],
                'fold_id':f'run-{run["development_derivation"]["run_id"]}-{validation}',
                'train_start_ms':validation-30*DAY,'train_end_ms':validation,
                'validation_start_ms':validation,'validation_end_ms':validation+15*DAY})
            validation+=15*DAY
    return {'schema':'native-five-minute-development-plan/v1','parent_file_sha256':PARENT_HASH,
        'python':platform.python_version(),'implementation_sha256':{
            p:hashlib.sha256((ROOT/p).read_bytes()).hexdigest() for p in paths},
        'native_interval_seconds':300,'features_or_parameters_tuned':False,
        'folds':folds,'warmup_days':30,'validation_days':15,'model_fitting':False,
        'candidates':{name:{'name':factory().name,'configuration':getattr(factory(),'configuration',{})} for name,factory in candidates().items()},
        'costs_bps':{name:list(values) for name,values in COSTS.items()},'starting_equity':'10000','max_notional':'1000','max_daily_loss':'100',
        'risk_fraction':'.005','buy_hold_allocation':'.1',
        'acceptance':{'minimum_folds':6,'minimum_closed_trades_each_cost':100,
            'minimum_positive_folds_each_cost':math.ceil(2*len(folds)/3),
            'positive_mean_net_and_excess_returns_each_cost':True,
            'max_drawdown_each_fold':'.10','positive_trade_pnl_interval_each_cost':True,
            'bootstrap':{'replicates':2000,'block_trades':10,'seed':0,'candidate_count':6,'familywise_alpha':.05}},
        'derived_integrity':[assess_dataset(d) for d in derived],
        'scope':'development_only','final_test_accessed':False,'confirmation_accessed':False,
        'limitations':['native five-minute stop/target ordering is conservative stop-first; minute path unavailable',
            'older historical development data is not prospective confirmation',
            'account fees are assumptions; tax/TDS/depth/latency not modeled',
            'complete derivatives do not repair the incomplete parent',
            'independent confirmation and forward paper evidence required; no deployment']}


def evaluate(derived,plan):
    risk=RiskConfig(base_currency=USDT,max_order_notional=Money('1000',USDT),
        max_position_notional=Money('1000',USDT),max_gross_exposure=Money('1000',USDT),
        max_daily_loss=Money('100',USDT),risk_fraction_per_trade=Decimal('.005'))
    reports={}
    for name,factory in candidates().items():
        scenarios={}
        for cost,(fee,slip,spread) in COSTS.items():
            results=[]
            for details in plan['folds']:
                run=next(d for d in derived if d['development_derivation']['run_id']==details['run_id'])
                require_qualification_eligibility(assess_dataset(run))
                from .backtest import load_historical_candles
                run_path=ROOT/f'artifacts/native-5m-complete-runs/run-{details["run_id"]}.json'
                candles,hashes=load_historical_candles(run_path)
                selected=[c for c in candles if details['train_start_ms']<=int(c.open_time.timestamp()*1000)<details['validation_end_ms']]
                result=HistoricalBacktester(factory(),config=BacktestConfig(fee_bps=fee,slippage_bps=slip,spread_bps=spread),risk=risk).run(
                    selected,dataset_hashes=hashes,trade_start_ms=details['validation_start_ms'],trade_end_ms=details['validation_end_ms'])
                report=dict(result.report)
                report.pop('equity_curve') # Per-fold metrics/trades retained; curve verified by simulator.
                report.pop('report_sha256')
                results.append({'fold':details,'result':report})
            metrics=[r['result']['metrics'] for r in results]
            mean=sum((Decimal(m['total_return']) for m in metrics),Decimal(0))/len(metrics)
            excess=sum((Decimal(m['total_return'])-Decimal('.1')*Decimal(m['buy_hold_benchmark_return_after_costs']) for m in metrics),Decimal(0))/len(metrics)
            scenarios[cost]={'folds':results,'trade_count':sum(m['trade_count'] for m in metrics),
                'mean_return':str(mean),'mean_excess_return_10pct_hold':str(excess),
                'positive_folds':sum(Decimal(m['total_return'])>0 for m in metrics),
                'max_drawdown':str(max(Decimal(m['maximum_drawdown']) for m in metrics)),
                'trade_pnl_interval':bootstrap([[t['net_pnl'] for t in r['result']['trades']] for r in results])}
            print(name,cost,'trades',scenarios[cost]['trade_count'],'mean return',mean,flush=True)
        checks={'data_integrity':all(d['eligible_for_strategy_qualification'] for d in plan['derived_integrity']),
            'coverage':len(plan['folds'])>=6,'sufficient_trades':all(s['trade_count']>=100 for s in scenarios.values()),
            'positive_folds':all(s['positive_folds']>=plan['acceptance']['minimum_positive_folds_each_cost'] for s in scenarios.values()),
            'returns':all(Decimal(s['mean_return'])>0 and Decimal(s['mean_excess_return_10pct_hold'])>0 for s in scenarios.values()),
            'drawdown':all(Decimal(s['max_drawdown'])<=Decimal('.10') for s in scenarios.values()),
            'interval':all(s['trade_pnl_interval'] and s['trade_pnl_interval']['lower']>0 for s in scenarios.values())}
        reports[name]={'scenarios':scenarios,'acceptance_checks':checks,'development_qualified':all(checks.values())}
    return {'schema':'native-five-minute-development-results/v1','plan_sha256':_hash(plan),'results':reports,
        'selected_candidates':[n for n,r in reports.items() if r['development_qualified']],
        'production_changed':False,'orders_executed':False,'confirmation_accessed':False,'final_test_accessed':False}


def _hash(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('prepare','run'));parser.add_argument('plan');parser.add_argument('--output')
    args=parser.parse_args(argv)
    raw=PARENT.read_bytes()
    if hashlib.sha256(raw).hexdigest()!=PARENT_HASH:parser.error('development source hash mismatch')
    derived=derive_complete_runs(json.loads(raw),PARENT_HASH,minimum_candles=45*288)
    if not derived:parser.error('insufficient complete development coverage')
    plan=specification(derived)
    if args.action=='prepare':
        folder=ROOT/'artifacts/native-5m-complete-runs';folder.mkdir(exist_ok=True)
        for d in derived:
            p=folder/f'run-{d["development_derivation"]["run_id"]}.json'
            if p.exists():
                if _hash(json.loads(p.read_text()))!=_hash(d):parser.error('existing complete derivative changed')
            else:write_new(p,d)
        write_new(args.plan,plan)
    else:
        if not args.output or Path(args.output).exists():parser.error('new output path required')
        if json.loads(Path(args.plan).read_text())!=plan:parser.error('frozen code/data/protocol changed')
        for d in derived:
            p=ROOT/f'artifacts/native-5m-complete-runs/run-{d["development_derivation"]["run_id"]}.json'
            if _hash(json.loads(p.read_text()))!=_hash(d):parser.error('derived artifact hash mismatch')
        result=evaluate(derived,plan);result['result_sha256']=_hash(result);write_new(args.output,result)
    return 0


if __name__=='__main__':raise SystemExit(main())
