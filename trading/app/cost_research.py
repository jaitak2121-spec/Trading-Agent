"""Bounded development-only hypotheses; no broker or production activation."""
import argparse
import hashlib
import json
import platform
import random
from decimal import Decimal
from pathlib import Path

from .backtest import BacktestConfig, HistoricalBacktester, load_historical_candles
from ..core.config import RiskConfig
from ..core.money import Money, USDT
from ..strategy.cost_aware import SlowerResearchStrategy

ROOT=Path(__file__).resolve().parents[2]
SOURCE=ROOT/'artifacts/backtest-btc-usdt-1m-2026-07-05_2026-09-03-development-recovered.json'
SOURCE_HASH='2c36a429413621ca33788fd627999658c9afccc98c4f184b994c05d9cbd5a871'
WARMUP=1560
MIN_BARS=4320
CANDIDATES=[(rule,frame) for rule in ('breakout','trend-pullback') for frame in (5,15,30)]
COSTS={'base':('10','2','2'),'stress':('20','5','4')}


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def implementation_hashes():
    paths=['trading/app/cost_research.py','trading/app/backtest.py',
           'trading/strategy/cost_aware.py','trading/strategy/indicators.py',
           'trading/strategy/context.py','trading/core/config.py','trading/core/marketdata.py',
           'trading/core/money.py','trading/strategy/signals.py']
    return {p:hashlib.sha256((ROOT/p).read_bytes()).hexdigest() for p in paths}


def contiguous(candles):
    runs=[]
    for candle in candles:
        if candle.interval_seconds != 60:
            raise ValueError('one-minute execution required')
        if runs and candle.open_time <= runs[-1][-1].open_time:
            raise ValueError('unordered or duplicate candle')
        if not runs or candle.open_time != runs[-1][-1].close_time:
            runs.append([])
        runs[-1].append(candle)
    return runs


def periods(candles):
    return [{'development_run':i,'candle_count':len(run),
             'start':run[0].open_time.isoformat(),'end_exclusive':run[-1].close_time.isoformat(),
             'trade_start':run[WARMUP].open_time.isoformat()}
            for i,run in enumerate(contiguous(candles),1) if len(run)>=MIN_BARS]


def bootstrap(runs,replicates=2000):
    """Trade blocks never bridge independent periods; selection-adjusted interval."""
    runs=[[float(v) for v in run] for run in runs if run]
    n=sum(map(len,runs))
    if n<30:
        return None
    rng=random.Random(0)
    means=[]
    for _ in range(replicates):
        sample=[]
        while len(sample)<n:
            run=rng.choices(runs,weights=[len(r) for r in runs])[0]
            size=min(10,len(run))
            start=rng.randrange(len(run)-size+1)
            sample.extend(run[start:start+size])
        means.append(sum(sample[:n])/n)
    means.sort()
    alpha=.05/6
    return {'method':'moving_trade_blocks_within_period','block_trades':10,'seed':0,
            'replicates':replicates,'familywise_alpha':.05,'candidate_count':6,
            'lower':means[int(replicates*alpha/2)],
            'upper':means[min(replicates-1,int(replicates*(1-alpha/2)))],
            'caveat':'development diagnostic; stationary trade blocks and exchangeable periods assumed'}


def preregister(candles,hashes):
    return {'schema':'cost-aware-development-plan/v1','scope':'development_only',
            'source_file_sha256':hashes['source_file_sha256'],
            'implementation_sha256':implementation_hashes(),'python':platform.python_version(),
            'candidates':[SlowerResearchStrategy(*c).configuration for c in CANDIDATES],
            'costs_bps':{k:dict(zip(('fee','slippage','spread'),v)) for k,v in COSTS.items()},
            'minimum_period_minutes':MIN_BARS,'warmup_minutes':WARMUP,
            'periods':periods(candles),'risk':{'initial_equity':'10000','max_notional':'1000',
                'max_daily_loss':'100','risk_fraction_per_trade':'0.005'},
            'acceptance':{'minimum_periods':5,'minimum_trades_each_cost':30,
                'minimum_positive_base_periods':4,'positive_mean_return_both_costs':True,
                'positive_mean_base_excess_over_full_hold':True,'max_drawdown_each_period':'.10',
                'positive_selection_adjusted_trade_pnl_interval_both_costs':True},
            'selection':'no tuning; all six disclosed; no automatic promotion',
            'confirmation_accessed':False,'protected_ml_segment_7_accessed':False,
            'limitations':['previously explored development source, not fresh out-of-sample evidence',
                'incomplete source; independent complete runs only; short and selectively available periods',
                'cost assumptions are not verified account fees; taxes/TDS and latency not modelled',
                'fresh independent confirmation and forward paper testing still required']}


def evaluate(candles,hashes,plan):
    risk=RiskConfig(base_currency=USDT,max_order_notional=Money('1000',USDT),
        max_position_notional=Money('1000',USDT),max_gross_exposure=Money('1000',USDT),
        max_daily_loss=Money('100',USDT),risk_fraction_per_trade=Decimal('.005'))
    eligible=[run for run in contiguous(candles) if len(run)>=MIN_BARS]
    results={}
    for rule,frame in CANDIDATES:
        name=f'{rule}-{frame}m'
        scenarios={}
        for scenario,(fee,slip,spread) in COSTS.items():
            reports=[]
            for run,period in zip(eligible,plan['periods']):
                result=HistoricalBacktester(strategy=SlowerResearchStrategy(rule,frame),
                    config=BacktestConfig(fee_bps=Decimal(fee),slippage_bps=Decimal(slip),
                        spread_bps=Decimal(spread)),risk=risk).run(run,dataset_hashes=hashes,
                    trade_start_ms=int(run[WARMUP].open_time.timestamp()*1000))
                reports.append({'period':period,'result':result.report})
            metrics=[r['result']['metrics'] for r in reports]
            scenarios[scenario]={'periods':reports,'trade_count':sum(m['trade_count'] for m in metrics),
                'mean_return':str(sum((Decimal(m['total_return']) for m in metrics),Decimal(0))/len(metrics)) if metrics else None,
                'positive_periods':sum(Decimal(m['total_return'])>0 for m in metrics),
                'mean_excess_over_full_hold':str(sum((Decimal(m['total_return'])-Decimal(m['buy_hold_benchmark_return_after_costs']) for m in metrics),Decimal(0))/len(metrics)) if metrics else None,
                'maximum_drawdown':str(max((Decimal(m['maximum_drawdown']) for m in metrics),default=Decimal(0))),
                'trade_mean_pnl_interval':bootstrap([[t['net_pnl'] for t in r['result']['trades']] for r in reports])}
            print(f'{name} {scenario}: {scenarios[scenario]["trade_count"]} trades',flush=True)
        base,stress=scenarios['base'],scenarios['stress']
        checks={'dataset_integrity':hashes.get('qualification_eligible')=='true',
            'coverage':len(eligible)>=5,'trades':all(s['trade_count']>=30 for s in scenarios.values()),
            'positive_periods':base['positive_periods']>=4,
            'positive_returns':all(s['mean_return'] is not None and Decimal(s['mean_return'])>0 for s in scenarios.values()),
            'base_excess':base['mean_excess_over_full_hold'] is not None and Decimal(base['mean_excess_over_full_hold'])>0,
            'drawdown':all(Decimal(s['maximum_drawdown'])<=Decimal('.10') for s in scenarios.values()),
            'intervals':all(s['trade_mean_pnl_interval'] is not None and s['trade_mean_pnl_interval']['lower']>0 for s in scenarios.values())}
        results[name]={'scenarios':scenarios,'acceptance_checks':checks,'qualified':all(checks.values())}
    return {'schema':'cost-aware-development-results/v1','preregistration_sha256':digest(plan),
            'results':results,'qualified_candidates':[k for k,v in results.items() if v['qualified']],
            'production_changed':False,'orders_executed':False,'limitations':plan['limitations']}


def write_new(path,value):
    with Path(path).open('x') as output:
        json.dump(value,output,sort_keys=True,indent=2)
        output.write('\n')


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('prepare','run'))
    parser.add_argument('plan')
    parser.add_argument('--output')
    args=parser.parse_args(argv)
    if hashlib.sha256(SOURCE.read_bytes()).hexdigest()!=SOURCE_HASH:
        parser.error('authorized development source hash mismatch')
    candles,hashes=load_historical_candles(SOURCE)
    if hashes['source_file_sha256']!=SOURCE_HASH:
        parser.error('source changed during loading')
    plan=preregister(candles,hashes)
    if args.action=='prepare':
        write_new(args.plan,plan)
    else:
        if not args.output or Path(args.output).exists():
            parser.error('new output path required')
        frozen=json.loads(Path(args.plan).read_text())
        if frozen!=plan:
            parser.error('preregistered implementation/data/periods/configuration changed')
        write_new(args.output,evaluate(candles,hashes,frozen))
    return 0


if __name__=='__main__':
    raise SystemExit(main())
