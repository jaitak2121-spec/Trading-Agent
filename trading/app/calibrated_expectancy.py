"""Development-only payoff calibration of one preregistered frozen learner family."""
import argparse
import hashlib
import json
import math
from pathlib import Path
import random
import statistics

from . import adaptive_expectancy as adaptive
from . import net_expectancy as r
from .backtest import BacktestConfig, HistoricalBacktester, load_historical_candles
from .cost_research import write_new


def line(x, y):
    if len(x) != len(y) or len(x) < 2 or not all(math.isfinite(v) for v in x+y):
        raise ValueError('aligned finite calibration pairs required')
    mx, my = statistics.fmean(x), statistics.fmean(y)
    variance = sum((v-mx)**2 for v in x)
    slope = max(0., sum((a-mx)*(b-my) for a,b in zip(x,y))/variance) if variance else 0.
    return [my-slope*mx, slope]


def calibrate(rows, scores, cutoff, replicates=1000):
    if len(rows) != len(scores) or len(rows) < 60:
        raise ValueError('at least 60 independent calibration outcomes required')
    if any(row['label_end_ms'] >= cutoff for row in rows):
        raise ValueError('calibration label reaches evaluation period')
    if any(a['label_end_ms'] > b['decision_ms'] for a,b in zip(rows, rows[1:])):
        raise ValueError('calibration labels overlap or are unordered')
    # Blocks never span a missing daily outcome. Gaps are boundaries, not fills.
    blocks = [list(range(i,i+3)) for i in range(len(rows)-2)
              if rows[i+2]['decision_ms']-rows[i]['decision_ms'] == 2*r.DAY]
    if not blocks:raise ValueError('no contiguous calibration blocks')
    targets = [row['net_return_target'] for row in rows]
    rng = random.Random(0); fits = []
    for _ in range(replicates):
        indices = []
        while len(indices) < len(rows):indices.extend(rng.choice(blocks))
        indices = indices[:len(rows)]
        fits.append(line([scores[i] for i in indices],[targets[i] for i in indices]))
    return {'schema':'payoff-calibration/v1','method':'nonnegative affine; 3-day moving-block bootstrap',
            'point_fit':line(scores,targets),'bootstrap_fits':fits,'replicates':replicates,'seed':0,
            'calibration_rows_sha256':r.digest(rows),'calibration_scores_sha256':r.digest(scores),
            'last_label_ms':max(row['label_end_ms'] for row in rows),'samples':len(rows),
            'score_min':min(scores),'score_max':max(scores),'fit_cutoff_ms':cutoff}


def lower_prediction(calibration, score):
    if not math.isfinite(score):raise ValueError('finite score required')
    if not calibration['score_min'] <= score <= calibration['score_max']:
        return None  # Never extrapolate beyond observed out-of-sample calibration.
    predictions=sorted(a+b*score for a,b in calibration['bootstrap_fits'])
    return predictions[int(.05*len(predictions))]


def prepare():
    paths=json.loads(Path('artifacts/net-expectancy-cycle-complete-runs/source-index.json').read_text())['paths']
    plan=adaptive.prepare(paths)
    diagnostic=Path('artifacts/frozen-signal-information-results/results.json')
    evidence=json.loads(diagnostic.read_text())
    if evidence['result_sha256'] != r.digest({k:v for k,v in evidence.items() if k!='result_sha256'}):
        raise ValueError('diagnostic evidence changed')
    plan.update(schema='calibrated-expectancy-plan/v1',stage=9,outer_candidates=1,
        hypothesis='24h logistic payoff ranks net outcomes but is miscalibrated; independent prior payoff calibration may filter unreliable forecasts',
        kind='logistic_payoff',horizon_bars=24,decision_stride_hours=24,
        diagnostic_file_sha256=hashlib.sha256(diagnostic.read_bytes()).hexdigest(),
        model_training='prior 365-day window excluding last 90 days; no refit after calibration',
        calibration='last 90 days: at least 60 nonoverlapping purged daily outcomes; nonnegative affine; 1000 three-day moving-block bootstrap fits',
        entry='in calibration score range; 5th percentile conditional mean > .0028 extra stress-cost allowance; full existing capped risk',
        matched_control='same learner, training rows and decision times; original uncalibrated buffer; diagnostic only, no selection',
        bootstrap_replicates=100000,
        alpha_spending={'stage':9,'allocation':.00001953125,'total_budget':.01},
        development_reuse='exploratory continuation; even a pass requires independent prospective evidence and new confirmation authorization')
    plan['implementation_sha256']['trading/app/calibrated_expectancy.py']=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    return plan


def run(plan,folder):
    if prepare()!=plan:raise ValueError('frozen calibration protocol changed')
    folder=Path(folder);folder.mkdir(exist_ok=False)
    series=[load_historical_candles(s['path'])[0] for s in plan['sources']]
    examples=[r.samples(bars,24,3600,24) for bars in series]
    pooled=sorted([row for rows in examples for row in rows],key=lambda row:row['decision_ms'])
    folds=[]
    for number,fold in enumerate(plan['folds']):
        train=adaptive.purged_rows(pooled,fold['training_start_ms'],fold['inner_start_ms'])
        calibration_rows=adaptive.purged_rows(pooled,fold['inner_start_ms'],fold['training_end_ms'])
        model=None;calibration=None;reason=None;predictions={};raw_predictions={};evidence=[]
        if len(train)>=100 and len(calibration_rows)>=60:
            model=r.fit_model(train,'logistic_payoff',3600)
            assert model['last_training_label_ms']<fold['inner_start_ms']
            scores=[r.predict(model,row['features'],3600) for row in calibration_rows]
            calibration=calibrate(calibration_rows,scores,fold['training_end_ms'])
            validation=[row for row in examples[fold['source_id']] if fold['validation_start_ms']<=row['decision_ms'] and row['label_end_ms']<fold['validation_end_ms']]
            for row in validation:
                score=r.predict(model,row['features'],3600);lower=lower_prediction(calibration,score)
                raw_predictions[row['decision_ms']]=score
                predictions[row['decision_ms']]=lower if lower is not None else -1.
                evidence.append({'decision_ms':row['decision_ms'],'raw_score':score,'lower_mean':lower})
            write_new(folder/f'fold-{number}-model.json',model)
            write_new(folder/f'fold-{number}-calibration.json',calibration)
        else:reason='insufficient independent calibration or training observations; cash'
        entry={'fold':fold,'reason':reason,'training_samples':len(train),'calibration_samples':len(calibration_rows),
               'model_sha256':model['model_sha256'] if model else None,'predictions':evidence,'policies':{}}
        for policy in ('calibrated','matched_control'):
            settings=dict(model) if model else {'kind':'cash','interval_seconds':3600,'target_std':0.}
            if policy=='calibrated':settings.update(research_entry_floor=.0028,research_full_risk=True)
            schedule=predictions if policy=='calibrated' else raw_predictions
            reports={}
            for scenario in ('base','stress','delayed_stress'):
                cost=plan['costs']['base' if scenario=='base' else 'stress']
                bars=[b for b in series[fold['source_id']] if fold['validation_start_ms']-289*3600000<=int(b.open_time.timestamp()*1000)<fold['validation_end_ms']]
                strategy=r.ExpectancyStrategy(settings,24,schedule)
                report=dict(HistoricalBacktester(strategy,config=BacktestConfig(fee_bps=cost[0],slippage_bps=cost[1],spread_bps=cost[2],
                    execution_timing='next_close' if scenario=='delayed_stress' else 'next_open')).run(bars,
                    dataset_hashes=plan['sources'][fold['source_id']]['hashes'],trade_start_ms=fold['validation_start_ms'],
                    trade_end_ms=fold['validation_end_ms']).report)
                daily=r.daily_returns(report.pop('equity_curve'));report.pop('report_sha256')
                reports[scenario]={'report':report,'daily_returns':daily,'opportunities':strategy.opportunities,'filtered':strategy.filtered}
            entry['policies'][policy]=reports
        write_new(folder/f'fold-{number}-evaluation.json',entry);folds.append(entry)
        print('calibrated fold',number,'base',entry['policies']['calibrated']['base']['report']['metrics']['total_return'],flush=True)
    summary={}
    for policy in ('calibrated','matched_control'):
        summary[policy]={}
        for scenario in ('base','stress','delayed_stress'):
            records=[fold['policies'][policy][scenario] for fold in folds];metrics=[v['report']['metrics'] for v in records]
            summary[policy][scenario]={'mean_return':statistics.fmean(float(m['total_return']) for m in metrics),
                'mean_excess_10pct_hold':statistics.fmean(float(m['total_return'])-.1*float(m['buy_hold_benchmark_return_after_costs']) for m in metrics),
                'trades':sum(m['trade_count'] for m in metrics),'positive_folds':sum(float(m['total_return'])>0 for m in metrics),
                'max_drawdown':max(float(m['maximum_drawdown']) for m in metrics),
                'interval':adaptive.interval([v['daily_returns'] for v in records],plan['bootstrap_replicates'],plan['alpha_spending']['allocation'])}
    target=summary['calibrated'];checks={'folds':len(folds)>=12,'trades':all(s['trades']>=60 for s in target.values()),
        'positive_folds':all(s['positive_folds']>=math.ceil(2*len(folds)/3) for s in target.values()),
        'net_and_benchmark_excess':all(s['mean_return']>0 and s['mean_excess_10pct_hold']>0 for s in target.values()),
        'drawdown':all(s['max_drawdown']<=.10 for s in target.values()),
        'intervals':all(s['interval'] and s['interval']['lower']>0 for s in target.values())}
    result={'schema':'calibrated-expectancy-results/v1','plan_sha256':r.digest(plan),'summary':summary,'checks':checks,
        'development_qualified':all(checks.values()),'folds':len(folds),'models_fitted':sum(f['model_sha256'] is not None for f in folds),
        'protected_observations_accessed':False,'orders_executed':False,'production_changed':False}
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
