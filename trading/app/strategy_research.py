"""Compare fixed strategies on explicitly bounded development folds only."""
import argparse
import hashlib
import json
import platform
from pathlib import Path

from .backtest import (BacktestConfig, BacktestFold, ChronologicalBacktestPlan,
                       load_historical_candles, run_development_walk_forward, write_backtest_report)
from ..strategy.examples import MovingAverageCrossover
from ..strategy.research import HistoricalRuleStrategy
from ..core.config import RiskConfig
from ..core.money import Money, USDT


def compare_development(candles, hashes, folds, plan):
    candidates = {'ma-crossover': lambda: MovingAverageCrossover(),
                  **{name: (lambda rule=name: HistoricalRuleStrategy(rule)) for name in
                     ('momentum', 'mean-reversion', 'volatility-filtered-momentum')}}
    risk = RiskConfig(base_currency=USDT, max_order_notional=Money('1000', USDT),
                      max_position_notional=Money('1000', USDT), max_gross_exposure=Money('1000', USDT),
                      max_daily_loss=Money('100', USDT))
    results = {name: run_development_walk_forward(candles, strategy_factory=factory,
               folds=folds, config=BacktestConfig(), risk=risk, dataset_hashes=hashes, plan=plan)
               for name, factory in candidates.items()}
    implementations = [Path(__file__), Path(__file__).with_name('backtest.py'),
                       Path(__file__).parents[1]/'strategy/research.py',
                       Path(__file__).parents[1]/'strategy/examples.py']
    return {'schema':'fixed-strategy-development-comparison/v1', 'results':results,
            'python_version':platform.python_version(),
            'implementation_sha256':{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in implementations},
            'parameter_optimization':False, 'approved_strategy':None,
            'confirmation_accessed':False, 'test_accessed':False,
            'limitations':['development comparisons do not establish profitability',
                          'separate preregistered confirmation and forward paper evidence required']}


def load_dashboard_summary(path):
    source = Path(path).resolve(strict=True)
    if any(token in str(source).lower() for token in ('confirmation', 'segment7', 'segment-7', 'segment_7')):
        raise ValueError('protected artifacts are not dashboard reports')
    raw = source.read_bytes()
    report = json.loads(raw)
    schema=report.get('schema')
    if schema=='net-expectancy-experiment-registry/v1':
        expected=hashlib.sha256(json.dumps({k:v for k,v in report.items() if k!='registry_sha256'},
            sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()
        if report.get('registry_sha256')!=expected:raise ValueError('experiment registry hash mismatch')
        if any(report.get(flag) is not False for flag in
               ('actual_orders_executed','protected_observations_accessed','model_promoted')):
            raise ValueError('dashboard requires inactive development-only evidence')
        if len(report['experiments'])>100:raise ValueError('bounded registry summary required')
        return {'schema':schema,'source_file_sha256':hashlib.sha256(raw).hexdigest(),
            'approved_strategy':None,'scope':'development ML research only; no candidate deployed',
            'saved_model_artifacts':report['total_saved_model_artifacts'],
            'outer_fold_evaluations':report['total_outer_fold_evaluations'],
            'experiments':{entry['id']:{name:{key:value for key,value in comparison.items()
                if key in ('summary','checks','development_qualified')}
                for name,comparison in entry['comparisons'].items()} for entry in report['experiments']},
            'limitations':['positive development averages do not establish an edge or approve paper/live deployment']}
    if schema in ('native-five-minute-development-results/v1','frozen-fill-cost-diagnostics/v1'):
        field='result_sha256' if schema=='native-five-minute-development-results/v1' else 'report_sha256'
        expected=hashlib.sha256(json.dumps({k:v for k,v in report.items() if k!=field},sort_keys=True,separators=(',',':')).encode()).hexdigest()
        if report.get(field)!=expected:raise ValueError('research/cost report hash mismatch')
        summary={'source_file_sha256':hashlib.sha256(raw).hexdigest(),'approved_strategy':None,
                 'scope':'development diagnostics only','schema':schema}
        if schema=='frozen-fill-cost-diagnostics/v1':
            if report.get('strategy_changed') is not False or report.get('model_promoted') is not False:
                raise ValueError('cost reports cannot authorize deployment')
            summary['diagnostics']={name:{cost:value['pooled_frozen_fills'] for cost,value in scenarios.items()}
                                    for name,scenarios in report['diagnostics'].items()}
            summary['limitations']=report['limitations']
        else:
            if report.get('production_changed') is not False or report.get('confirmation_accessed') is not False or report.get('final_test_accessed') is not False:
                raise ValueError('dashboard requires development-only research')
            summary['plan_sha256']=report['plan_sha256']
            summary['strategies']={name:{'development_qualified':value['development_qualified'],
                'scenarios':{cost:{k:v for k,v in result.items() if k!='folds'}
                    for cost,result in value['scenarios'].items()}}
                for name,value in report['results'].items()}
            summary['limitations']=['development qualification is not deployment approval or live-profit evidence']
        return summary
    if report.get('schema') != 'fixed-strategy-development-comparison/v1':
        raise ValueError('dashboard requires a development comparison report')
    return {'source_file_sha256':hashlib.sha256(raw).hexdigest(), 'approved_strategy':None,
            'plan_sha256':report.get('plan_sha256'), 'strategies':{
                name:[{'fold':fold['fold']['fold_id'],
                       'status':fold.get('status', 'evaluated'),
                       'metrics':None if fold['result'] is None else fold['result']['metrics']}
                      for fold in value['folds']] for name,value in report['results'].items()}}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('plan', help='JSON plan containing exact development source hash and folds')
    parser.add_argument('output')
    args = parser.parse_args(argv)
    specification = json.loads(Path(args.plan).read_text())
    # Only the previously authorized backtest development source may be opened.
    source = Path(args.plan).resolve().parent.parent/'artifacts/backtest-btc-usdt-1m-2026-07-05_2026-09-03-development-recovered.json'
    if hashlib.sha256(source.read_bytes()).hexdigest() != specification['source_file_sha256']:
        parser.error('development source hash mismatch')
    plan = ChronologicalBacktestPlan(**specification['chronology'])
    folds = [BacktestFold(**fold) for fold in specification['folds']]
    for fold in folds:
        plan.require_development_fold(fold)
    candles, hashes = load_historical_candles(source)
    if hashes['source_file_sha256'] != specification['source_file_sha256']:
        parser.error('development source changed during loading')
    if any(not plan.development_start_ms <= int(c.open_time.timestamp()*1000) < plan.development_end_ms for c in candles):
        parser.error('source contains rows outside the declared development period')
    report = compare_development(candles, hashes, folds, plan)
    report['plan_sha256'] = hashlib.sha256(Path(args.plan).read_bytes()).hexdigest()
    write_backtest_report(args.output, report)
    print(args.output)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
