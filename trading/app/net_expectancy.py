"""Cost-targeted ML research over explicit complete development runs, never orders."""
import argparse
import datetime as dt
from decimal import Decimal
import hashlib
import json
import math
from pathlib import Path
import platform
import random
import statistics

from .backtest import BacktestConfig, HistoricalBacktester, load_historical_candles
from .cost_research import write_new
from .model_training import fit_logistic_classifier, predict_logistic_probabilities
from ..core.money import Price
from ..strategy.signals import SignalStrategy, Signal, SignalDirection

DAY=86400000
STEP=300000
FEATURES=tuple([f'return_{n}' for n in (1,3,12,48,288)] +
    [f'volatility_{n}' for n in (12,48,288)] +
    ['volume_12_48','volume_48_288','sma_12_48','sma_48_288',
     'range_fraction','signed_body','upper_wick','lower_wick','trend_strength','volatility_ratio'])
FEATURE_SCHEMA='causal-native-bar-net-expectancy-features/v2'
COSTS={'base':('10','2','2'),'stress':('20','5','4')}


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()


def features(bars,interval_seconds=300):
    """Only completed, consecutive native 5m bars; 24-hour causal context."""
    if len(bars)<289:raise ValueError('289 closed candles required')
    bars=bars[-289:]
    if any(a.close_time!=b.open_time for a,b in zip(bars,bars[1:])):
        raise ValueError('feature window crosses a gap')
    if interval_seconds not in (300,3600) or any((b.close_time-b.open_time).total_seconds()!=interval_seconds for b in bars):
        raise ValueError('matching supported native candle cadence required')
    close=[float(b.close.amount) for b in bars]
    volume=[float(b.volume.amount) for b in bars]
    returns=[b/a-1 for a,b in zip(close,close[1:])]
    vol={n:statistics.pstdev(returns[-n:]) for n in (12,48,288)}
    sma={n:statistics.fmean(close[-n:]) for n in (12,48,288)}
    v={n:statistics.fmean(volume[-n:]) for n in (12,48,288)}
    last=bars[-1];o,h,l,c=map(float,(last.open.amount,last.high.amount,last.low.amount,last.close.amount))
    width=h-l
    result=[close[-1]/close[-n-1]-1 for n in (1,3,12,48,288)]
    result += [vol[n] for n in (12,48,288)]
    result += [v[12]/v[48]-1 if v[48] else 0,v[48]/v[288]-1 if v[288] else 0,
        sma[12]/sma[48]-1,sma[48]/sma[288]-1,(h-l)/c,
        (c-o)/width if width else 0,(h-max(o,c))/width if width else 0,
        (min(o,c)-l)/width if width else 0,
        (close[-1]/close[-49]-1)/(vol[48]*math.sqrt(48)) if vol[48] else 0,
        vol[48]/vol[288] if vol[288] else 0]
    if not all(math.isfinite(x) for x in result):raise ValueError('nonfinite features')
    return result


def stop_fraction(vector,horizon_bars):
    return min(.10,max(.01,3*vector[6]*math.sqrt(horizon_bars)))


def trade_target(bars,index,horizon,stop, costs=COSTS['base']):
    """Per-unit outcome matches simulator next-open/stop rules, including both fees.

    Purge uses the full potential horizon even when a stop occurs earlier.
    Labels are not allowed to determine inference eligibility.
    """
    if horizon<1 or index+horizon+1>=len(bars):raise ValueError('label exceeds segment')
    window=bars[index:index+horizon+2]
    if any(a.close_time!=b.open_time for a,b in zip(window,window[1:])):
        raise ValueError('label crosses a gap')
    fee,slip,spread=map(Decimal,costs)
    entry=bars[index+1].open.amount*(1+spread/20000)*(1+slip/10000)
    if entry<=stop:return 0.0
    exit_price=bars[index+horizon+1].open.amount
    for bar in bars[index+1:index+horizon+1]:
        if bar.open.amount<=stop:exit_price=bar.open.amount;break
        if bar.low.amount<=stop:exit_price=stop;break
    exit_price=exit_price*(1-spread/20000)*(1-slip/10000)
    return float(exit_price*(1-fee/10000)/(entry*(1+fee/10000))-1)


def samples(bars,horizon,interval_seconds=300,decision_stride_hours=1):
    if decision_stride_hours not in (1,24):raise ValueError('unsupported decision stride')
    result=[]
    for index in range(288,len(bars)-horizon-1):
        bar=bars[index]
        if int(bar.close_time.timestamp())%(3600*decision_stride_hours):continue
        vector=features(bars[index-288:index+1],interval_seconds)
        stop=Price.rounded(bar.close.amount*(1-Decimal(str(stop_fraction(vector,horizon)))),bar.close.currency)
        result.append({'decision_ms':int(bar.close_time.timestamp()*1000),
            'label_end_ms':int(bars[index+horizon+1].open_time.timestamp()*1000),
            'features':vector,'net_return_target':trade_target(bars,index,horizon,stop.amount)})
    return result


def split_samples(rows,fold):
    train=[r for r in rows if fold['training_start_ms']<=r['decision_ms']<fold['training_end_ms']
           and r['label_end_ms']<fold['training_end_ms']]
    validation=[r for r in rows if fold['validation_start_ms']<=r['decision_ms']
                and r['label_end_ms']<fold['validation_end_ms']]
    if fold['training_end_ms']>fold['validation_start_ms']:
        raise ValueError('training and outer validation overlap')
    return train,validation


def _solve(matrix,target):
    augmented=[list(row)+[y] for row,y in zip(matrix,target)]
    for k in range(len(target)):
        pivot=max(range(k,len(target)),key=lambda i:abs(augmented[i][k]))
        augmented[k],augmented[pivot]=augmented[pivot],augmented[k]
        scale=augmented[k][k]
        if abs(scale)<1e-14:raise ValueError('singular ridge system')
        augmented[k]=[x/scale for x in augmented[k]]
        for i in range(len(target)):
            if i==k:continue
            factor=augmented[i][k]
            augmented[i]=[a-factor*b for a,b in zip(augmented[i],augmented[k])]
    return [row[-1] for row in augmented]


def _tree(x,y,depth=2):
    mean=statistics.fmean(y);node={'mean':mean,'count':len(y)}
    minimum=max(30,len(y)//10)
    if depth<=0 or len(y)<2*minimum:return node
    best=None
    for column in range(len(x[0])):
        values=sorted(set(row[column] for row in x))
        if len(values)<2:continue
        for quantile in range(1,8):
            cut=values[min(len(values)-2,len(values)*quantile//8)]
            left=[i for i,row in enumerate(x) if row[column]<=cut]
            right=[i for i,row in enumerate(x) if row[column]>cut]
            if min(len(left),len(right))<minimum:continue
            loss=sum(sum(y[i]*y[i] for i in side)-sum(y[i] for i in side)**2/len(side)
                     for side in (left,right))
            if best is None or loss<best[0]:best=(loss,column,cut,left,right)
    if best is not None:
        _,column,cut,left,right=best
        node.update(column=column,cut=cut,left=_tree([x[i] for i in left],[y[i] for i in left],depth-1),
                    right=_tree([x[i] for i in right],[y[i] for i in right],depth-1))
    return node


def fit_model(rows,kind,interval_seconds=300,feature_names=FEATURES):
    if interval_seconds not in (300,3600):raise ValueError('unsupported native model cadence')
    if not feature_names or len(set(feature_names))!=len(feature_names):raise ValueError('unique named features required')
    if len(rows)<100:raise ValueError('at least 100 purged training examples required')
    x=[r['features'] for r in rows];y=[r['net_return_target'] for r in rows]
    if any(len(row)!=len(feature_names) or not all(math.isfinite(v) for v in row) for row in x) or not all(math.isfinite(v) for v in y):
        raise ValueError('finite training vectors matching feature schema required')
    model={'schema':'net-expectancy-model/v1','kind':kind,'feature_schema':FEATURE_SCHEMA,
           'features':list(feature_names),'interval_seconds':interval_seconds,
           'training_rows_sha256':digest(rows),'training_samples':len(rows),
           'last_training_label_ms':max(r['label_end_ms'] for r in rows),
           'target_mean':statistics.fmean(y),'target_std':statistics.pstdev(y),
           'preprocessing_fit':'training_only','random_seed':0}
    if kind=='ridge':
        means=list(map(statistics.fmean,zip(*x)));scales=[statistics.pstdev(c) or 1 for c in zip(*x)]
        z=[[(v-m)/s for v,m,s in zip(row,means,scales)] for row in x]
        n=len(x);d=len(feature_names);bias=statistics.fmean(y)
        gram=[[sum(row[i]*row[j] for row in z)/n+(1.0 if i==j else 0) for j in range(d)] for i in range(d)]
        rhs=[sum(row[i]*(target-bias) for row,target in zip(z,y))/n for i in range(d)]
        model.update(means=means,scales=scales,weights=_solve(gram,rhs),bias=bias,l2_penalty=1.0)
    elif kind=='tree':model.update(tree=_tree(x,y),maximum_depth=2)
    elif kind=='boosted':
        bias=statistics.fmean(y);current=[bias]*len(y);trees=[]
        for _ in range(12):
            tree=_tree(x,[target-value for target,value in zip(y,current)],depth=2);trees.append(tree)
            for i,row in enumerate(x):
                node=tree
                while 'column' in node:node=node['left'] if row[node['column']]<=node['cut'] else node['right']
                current[i]+=.1*node['mean']
        model.update(trees=trees,bias=bias,learning_rate=.1,boosting_rounds=12,maximum_depth=2)
    elif kind=='regime':
        columns=(3,6,9,10,16,17)
        points=[[row[j] for j in columns] for row in x]
        means=list(map(statistics.fmean,zip(*points)));scales=[statistics.pstdev(c) or 1 for c in zip(*points)]
        z=[[(v-m)/s for v,m,s in zip(row,means,scales)] for row in points]
        def distance(a,b):return sum((u-v)**2 for u,v in zip(a,b))
        centers=[list(z[0])]
        for _ in range(2):centers.append(list(max(z,key=lambda point:min(distance(point,c) for c in centers))))
        for _ in range(25):
            groups=[[] for c in centers]
            for i,point in enumerate(z):groups[min(range(3),key=lambda j:distance(point,centers[j]))].append(i)
            updated=[[statistics.fmean(z[i][j] for i in group) for j in range(len(columns))] if group else centers[k]
                     for k,group in enumerate(groups)]
            if updated==centers:break
            centers=updated
        groups=[[] for c in centers]
        for i,point in enumerate(z):groups[min(range(3),key=lambda j:distance(point,centers[j]))].append(i)
        prior=statistics.fmean(y)
        model.update(regime_columns=list(columns),means=means,scales=scales,centers=centers,
            regime_training_counts=list(map(len,groups)),
            regime_net_expectancies=[(sum(y[i] for i in group)+20*prior)/(len(group)+20) for group in groups],
            regime_prior_strength=20,regime_iterations=25)
    elif kind=='logistic_payoff':
        labels=[int(value>0) for value in y]
        if len(set(labels))!=2:raise ValueError('payoff classifier requires both outcomes')
        model.update(classifier=fit_logistic_classifier(x,labels,feature_names=tuple(feature_names),l2_penalty=.01,iterations=150),
            mean_win=statistics.fmean(v for v in y if v>0),mean_loss=-statistics.fmean(v for v in y if v<=0))
    else:raise ValueError('unknown expectancy model')
    model['model_sha256']=digest(model)
    return model


def predict(model,vector,interval_seconds=300,feature_names=FEATURES):
    if model.get('model_sha256')!=digest({k:v for k,v in model.items() if k!='model_sha256'}):
        raise ValueError('model integrity hash mismatch')
    if model.get('feature_schema')!=FEATURE_SCHEMA or model.get('features')!=list(feature_names) or len(vector)!=len(feature_names) or model.get('interval_seconds')!=interval_seconds:
        raise ValueError('incompatible expectancy features')
    if not all(math.isfinite(value) for value in vector):raise ValueError('nonfinite prediction input')
    if model['kind']=='ridge':
        return model['bias']+sum(w*(v-m)/s for w,v,m,s in zip(model['weights'],vector,model['means'],model['scales']))
    if model['kind']=='tree':
        node=model['tree']
        while 'column' in node:node=node['left'] if vector[node['column']]<=node['cut'] else node['right']
        return node['mean']
    if model['kind']=='boosted':
        result=model['bias']
        for tree in model['trees']:
            node=tree
            while 'column' in node:node=node['left'] if vector[node['column']]<=node['cut'] else node['right']
            result+=model['learning_rate']*node['mean']
        return result
    if model['kind']=='regime':
        point=[(vector[j]-m)/s for j,m,s in zip(model['regime_columns'],model['means'],model['scales'])]
        index=min(range(3),key=lambda i:sum((u-v)**2 for u,v in zip(point,model['centers'][i])))
        return model['regime_net_expectancies'][index]
    if model['kind']=='logistic_payoff':
        p=predict_logistic_probabilities(model['classifier'],[vector])[0]
        return p*model['mean_win']-(1-p)*model['mean_loss']
    raise ValueError('unknown expectancy model')


class ExpectancyStrategy(SignalStrategy):
    """Frozen forecast schedule through the existing historical risk/fill engine."""
    required_bars=289
    def __init__(self,model,horizon,predictions,policy='selective'):
        self.name=f'net-{model["kind"]}-h{horizon*model["interval_seconds"]//3600}-{policy}'
        self.model=model;self.horizon=horizon;self.predictions=predictions;self.policy=policy
        self.symbol=model.get('symbol','BTC/USDT')
        self.expiry=None;self.opportunities=0;self.filtered=0

    def decision_due(self,candle):
        return (int(candle.close_time.timestamp()*1000) in self.predictions or
                self.expiry is not None and candle.close_time==self.expiry)

    def generate(self,context):
        symbol=self.symbol;bars=context.candles(symbol)
        if not context.is_flat(symbol):
            if self.expiry is not None and context.as_of>=self.expiry:
                return [Signal(strategy_name=self.name,signal_id=f'{self.name}:{context.as_of}:exit',
                    symbol=symbol,direction=SignalDirection.EXIT,reference_price=context.price(symbol),
                    as_of=context.as_of,rationale='fixed learned-outcome holding horizon')]
            return []
        when=int(context.as_of.timestamp()*1000)
        if when not in self.predictions:return []
        score=self.predictions[when];self.opportunities+=1
        if self.policy=='cash':self.filtered+=1;return []
        interval=self.model['interval_seconds']
        vector=features(bars,interval)
        # Fixed no-trade buffer plus uncertainty margin, learned only from train.
        floor=self.model.get('research_entry_floor',.001+.1*self.model['target_std'])
        allowed=(score>floor if self.policy=='selective' else
                 score>floor and vector[3]>0 if self.policy=='trend_selective' else
                 vector[3]>0 if self.policy=='momentum' else
                 vector[3]<0 if self.policy=='contrarian' else True)
        if not allowed:self.filtered+=1;return []
        self.expiry=context.as_of+dt.timedelta(seconds=self.horizon*interval)
        reference=context.price(symbol)
        stop=Price.rounded(reference.amount*(1-Decimal(str(stop_fraction(vector,self.horizon)))),reference.currency)
        conviction=min(1.,max(.25,score/max(self.model['target_std'],.000001))) if self.policy in ('selective','trend_selective') else 1.
        if self.model.get('research_full_risk') is True:conviction=1.
        return [Signal(strategy_name=self.name,signal_id=f'{self.name}:{when}:entry',symbol=symbol,
            direction=SignalDirection.LONG,reference_price=reference,as_of=context.as_of,
            rationale='frozen cost-adjusted expectancy above training-only uncertainty margin',
            conviction=Decimal(str(conviction)),stop_loss=stop,
            evidence={'predicted_net_return':str(score),'entry_floor':str(floor)})]


def daily_returns(curve):
    last={}
    for row in curve:last[row['time'][:10]]=float(row['equity'])
    previous=10000.;result=[]
    for value in last.values():result.append(value/previous-1);previous=value
    return result


def confidence(runs,candidate_count,replicates=10000,familywise_alpha=.005):
    if len(runs)<3 or sum(map(len,runs))<30:return None
    rng=random.Random(0);means=[]
    for _ in range(replicates):
        draw=[]
        for run in runs:
            selected=[];block=min(3,len(run))
            while len(selected)<len(run):
                start=rng.randrange(len(run)-block+1);selected.extend(run[start:start+block])
            draw.extend(selected[:len(run)])
        means.append(statistics.fmean(draw))
    means.sort();tail=familywise_alpha/candidate_count/2
    return {'lower':means[int(replicates*tail)],'upper':means[min(replicates-1,int(replicates*(1-tail)))],
        'method':'moving_calendar_day_blocks_within_validation_fold','block_days':3,
        'replicates':replicates,'seed':0,'familywise_alpha':familywise_alpha,'candidate_count':candidate_count,
        'caveat':'development diagnostic; previous experiments consumed other evidence; unseen confirmation still required'}


def prepare(paths):
    sources=[];folds=[]
    for path in paths:
        resolved=Path(path).resolve()
        if any(s in str(resolved).lower() for s in ('confirmation','segment7','segment-7','segment_7','causal-','ml-research-v2','2025-11-02_2026')):
            raise ValueError('protected paths forbidden')
        bars,hashes=load_historical_candles(resolved)
        if hashes['qualification_eligible']!='true':raise ValueError('complete independently validated sources required')
        if int(bars[-1].close_time.timestamp()*1000)>1762041600000:
            raise ValueError('only development before 2025-11-02 allowed; protected periods never loaded')
        features(bars[:289])
        source_id=len(sources);start=int(bars[0].open_time.timestamp()*1000);end=int(bars[-1].close_time.timestamp()*1000)
        if sources and start<sources[-1]['end_ms']:
            raise ValueError('sources must be chronological and non-overlapping')
        sources.append({'path':str(resolved),'hashes':hashes,'start_ms':start,'end_ms':end})
        validation=start+31*DAY
        while validation+15*DAY<=end:
            folds.append({'source_id':source_id,'training_start_ms':validation-30*DAY,
                'training_end_ms':validation,'validation_start_ms':validation,'validation_end_ms':validation+15*DAY})
            validation+=15*DAY
    if not folds:raise ValueError('no complete walk-forward folds')
    return {'schema':'net-expectancy-preregistration/v1','sources':sources,'folds':folds,
        'candidates':[{'kind':kind,'horizon_bars':hours*12} for hours in (1,6,24) for kind in ('ridge','tree','logistic_payoff')],
        'feature_schema':FEATURE_SCHEMA,'features':list(FEATURES),'costs':{k:list(v) for k,v in COSTS.items()},
        'sampling':'hourly; overlapping training outcomes purged by full label end; no random split',
        'target':'next-open to stop or fixed-horizon next-open net return, including base fees, spread, slippage',
        'entry_floor':'.001 + .1 * training_target_std','regime_analysis':'causal 4h trend and volatility',
        'acceptance':{'minimum_folds':6,'minimum_trades_per_cost':60,'positive_fold_fraction':2/3,
            'max_drawdown':.10,'positive_mean_net_and_10pct_hold_excess':True,'daily_return_CI_lower_positive':True},
        'alpha_spending':{'stage':1,'total_future_research_budget':.01,'stage_allocation':.005,
            'policy':'stage k spends .01 / 2**k; Bonferroni across all preregistered candidates within stage'},
        'implementation_sha256':{p:hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in
            ('trading/app/net_expectancy.py','trading/app/backtest.py','trading/app/model_training.py',
             'trading/app/dataset_eligibility.py','trading/app/historical_candles.py',
             'trading/core/money.py','trading/core/config.py','trading/strategy/context.py',
             'trading/strategy/signals.py')},
        'python':platform.python_version(),'production_changed':False,'confirmation_accessed':False,
        'segment_7_accessed':False,'orders_executed':False}


def run(plan,folder):
    if prepare([s['path'] for s in plan['sources']])!=plan:raise ValueError('frozen plan/code/data mismatch')
    folder=Path(folder);folder.mkdir(exist_ok=False)
    series=[load_historical_candles(s['path'])[0] for s in plan['sources']]
    cached={(i,h):samples(bars,h) for i,bars in enumerate(series) for h in (12,72,288)}
    results={}
    for candidate in plan['candidates']:
        kind,h=candidate['kind'],candidate['horizon_bars'];name=f'{kind}-h{h//12}'
        folds=[]
        for number,fold in enumerate(plan['folds']):
            rows=cached[fold['source_id'],h]
            train,validation=split_samples(rows,fold)
            model=fit_model(train,kind)
            model.pop('model_sha256')
            model.update(horizon_bars=h,interval_seconds=300,target_costs_bps=list(COSTS['base']),
                         stop_policy='clip(3 * causal volatility_48 * sqrt(horizon_bars), .01, .10)',
                         source_hashes=plan['sources'][fold['source_id']]['hashes'],fold=fold)
            model['model_sha256']=digest(model)
            assert model['last_training_label_ms']<fold['validation_start_ms']
            write_new(folder/f'{name}-fold-{number}-model.json',model)
            predictions={r['decision_ms']:predict(model,r['features']) for r in validation}
            bars=[b for b in series[fold['source_id']] if
                fold['validation_start_ms']-DAY-STEP<=int(b.open_time.timestamp()*1000)<fold['validation_end_ms']]
            entry={'fold':fold,'training_samples':len(train),'validation_samples':len(validation),
                   'model_sha256':model['model_sha256'],'prediction_sha256':digest(predictions),'scenarios':{}}
            entry['predictions']=[{'decision_ms':when,'expected_net_return':score} for when,score in predictions.items()]
            for scenario,cost in plan['costs'].items():
                strategy=ExpectancyStrategy(model,h,predictions)
                report=dict(HistoricalBacktester(strategy,config=BacktestConfig(fee_bps=cost[0],slippage_bps=cost[1],spread_bps=cost[2])).run(
                    bars,dataset_hashes=plan['sources'][fold['source_id']]['hashes'],
                    trade_start_ms=fold['validation_start_ms'],trade_end_ms=fold['validation_end_ms']).report)
                returns=daily_returns(report.pop('equity_curve'));report.pop('report_sha256')
                entry['scenarios'][scenario]={'report':report,'daily_returns':returns,
                    'opportunities':strategy.opportunities,'no_trade_decisions':strategy.filtered}
            # Same horizon, risk, costs and entry calendar: causal baselines.
            entry['baselines']={}
            for policy in ('always','momentum','contrarian','cash'):
                base=ExpectancyStrategy(model,h,predictions,policy)
                entry['baselines'][policy]=dict(HistoricalBacktester(base).run(bars,
                    dataset_hashes=plan['sources'][fold['source_id']]['hashes'],
                    trade_start_ms=fold['validation_start_ms'],trade_end_ms=fold['validation_end_ms']).metrics)
            write_new(folder/f'{name}-fold-{number}-evaluation.json',entry);folds.append(entry)
            print(name,'fold',number,'net',entry['scenarios']['base']['report']['metrics']['total_return'],flush=True)
        summary={}
        for scenario in plan['costs']:
            metrics=[f['scenarios'][scenario]['report']['metrics'] for f in folds]
            summary[scenario]={'mean_return':statistics.fmean(float(m['total_return']) for m in metrics),
                'mean_excess_10pct_hold':statistics.fmean(float(m['total_return'])-.1*float(m['buy_hold_benchmark_return_after_costs']) for m in metrics),
                'trades':sum(m['trade_count'] for m in metrics),'positive_folds':sum(float(m['total_return'])>0 for m in metrics),
                'max_drawdown':max(float(m['maximum_drawdown']) for m in metrics),
                'daily_return_interval':confidence([f['scenarios'][scenario]['daily_returns'] for f in folds],len(plan['candidates']))}
        checks={'coverage':len(folds)>=6,'trades':all(s['trades']>=60 for s in summary.values()),
            'positive_periods':all(s['positive_folds']>=math.ceil(2*len(folds)/3) for s in summary.values()),
            'return_and_excess':all(s['mean_return']>0 and s['mean_excess_10pct_hold']>0 for s in summary.values()),
            'drawdown':all(s['max_drawdown']<=.10 for s in summary.values()),
            'intervals':all(s['daily_return_interval'] and s['daily_return_interval']['lower']>0 for s in summary.values())}
        results[name]={'summary':summary,'checks':checks,'development_qualified':all(checks.values()),
                       'fold_evidence':[f'{name}-fold-{n}-evaluation.json' for n in range(len(folds))]}
        write_new(folder/f'{name}-decision.json',results[name])
    result={'schema':'net-expectancy-development-results/v1','plan_sha256':digest(plan),'results':results,
        'selected_candidates':[name for name,r in results.items() if r['development_qualified']],
        'confirmation_accessed':False,'segment_7_accessed':False,'production_changed':False,'orders_executed':False}
    result['result_sha256']=digest(result);write_new(folder/'results.json',result)
    return result


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('prepare','run'));parser.add_argument('plan')
    parser.add_argument('--source',action='append');parser.add_argument('--output')
    args=parser.parse_args(argv)
    if args.action=='prepare':
        if not args.source:parser.error('explicit complete development sources required')
        write_new(args.plan,prepare(args.source))
    else:
        if not args.output:parser.error('new experiment output directory required')
        run(json.loads(Path(args.plan).read_text()),args.output)
    return 0


if __name__=='__main__':raise SystemExit(main())
