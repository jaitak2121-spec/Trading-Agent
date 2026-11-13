"""Verify and compare explicitly registered development experiments offline."""
import argparse
import datetime as dt
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import platform

from .cost_research import write_new
from .dataset_eligibility import assess_dataset,require_qualification_eligibility
from .net_expectancy import digest


def checked_path(value):
    path=Path(value).resolve()
    if any(token in str(path).lower() for token in ('confirmation','segment7','segment-7','segment_7')):
        raise ValueError('protected observations are not registry inputs')
    return path


def verify_ledger(entry,plan):
    count=0;maximum_error=Decimal(0);risk_breaches=[]
    fold=entry['fold']
    for scenario,details in entry['scenarios'].items():
        report=details['report'];metrics=report['metrics'];pnl=fees=Decimal(0);by_day={}
        costs=plan['costs']['base' if scenario=='base' else 'stress']
        fee_rate=Decimal(costs[0])/10000
        for trade in report['trades']:
            q,opening,closing,entry_fee,exit_fee,net=map(Decimal,
                (trade[k] for k in ('quantity','entry_price','exit_price','entry_fee','exit_fee','net_pnl')))
            if min(q,opening,closing)<=0 or min(entry_fee,exit_fee)<0:raise ValueError('invalid trade amounts')
            error=max(abs(q*(closing-opening)-entry_fee-exit_fee-net),
                      abs(q*opening*fee_rate-entry_fee),abs(q*closing*fee_rate-exit_fee))
            if error>Decimal('.00000001'):raise ValueError('trade fees/PnL do not reconcile')
            maximum_error=max(maximum_error,error)
            if q*opening>Decimal('1000.00000001'):raise ValueError('order notional exceeds existing cap')
            start=int(dt.datetime.fromisoformat(trade['entry_time']).timestamp()*1000)
            end=int(dt.datetime.fromisoformat(trade['exit_time']).timestamp()*1000)
            decision=int(trade['signal_id'].rsplit(':',2)[1])
            if not fold['validation_start_ms']<=decision<=start<=end<=fold['validation_end_ms']:
                raise ValueError('trade chronology or evaluation boundary violation')
            if start<decision:raise ValueError('entry precedes closed-bar decision')
            pnl+=net;fees+=entry_fee+exit_fee;count+=1
            day=trade['exit_time'][:10];by_day[day]=by_day.get(day,Decimal(0))+net
        if len(report['trades'])!=metrics['trade_count']:raise ValueError('trade count mismatch')
        if abs(pnl-Decimal(metrics['realized_pnl']))>Decimal('.00000001'):raise ValueError('reported PnL mismatch')
        if abs(fees-Decimal(metrics['fee_total']))>Decimal('.00000001'):raise ValueError('reported fees mismatch')
        if abs(Decimal(metrics['ending_equity'])-Decimal(metrics['starting_equity'])-pnl)>Decimal('.00000001'):
            raise ValueError('account equity does not reconcile to closed ledger')
        cumulative=1.
        for value in details['daily_returns']:cumulative*=1+value
        if abs(cumulative-float(metrics['ending_equity'])/float(metrics['starting_equity']))>1e-9:
            raise ValueError('daily returns do not reconcile to final equity')
        risk_breaches.extend({'scenario':scenario,'day':day,'net_pnl':str(value)} for day,value in by_day.items() if value< -100)
    return {'trade_scenario_records':count,'maximum_decimal_reconciliation_error':str(maximum_error),
            'realized_daily_loss_over_100':risk_breaches}


def build(specification):
    output=[]
    for item in specification['experiments']:
        plan_path=checked_path(item['plan']);plan=json.loads(plan_path.read_text())
        folder=checked_path(item['results']);result_path=checked_path(folder/'results.json')
        raw=result_path.read_bytes();result=json.loads(raw)
        if result['plan_sha256']!=digest(plan) or result['result_sha256']!=digest({k:v for k,v in result.items() if k!='result_sha256'}):
            raise ValueError('plan/result hash mismatch')
        snapshot=checked_path(item['source_snapshot'])
        for name,expected in plan['implementation_sha256'].items():
            if hashlib.sha256(checked_path(snapshot/name).read_bytes()).hexdigest()!=expected:
                raise ValueError('frozen implementation snapshot mismatch')
        for source in plan['sources']+plan.get('auxiliary_sources',[]):
            path=checked_path(source['path']);source_raw=path.read_bytes()
            if hashlib.sha256(source_raw).hexdigest()!=source['hashes']['source_file_sha256']:
                raise ValueError('development source hash changed')
            require_qualification_eligibility(assess_dataset(json.loads(source_raw)))
        if plan.get('funding'):
            from .funding_expectancy import validate_funding
            funding_raw=checked_path(plan['funding']['path']).read_bytes()
            if hashlib.sha256(funding_raw).hexdigest()!=plan['funding']['file_sha256']:
                raise ValueError('funding source hash changed')
            validate_funding(json.loads(funding_raw))
        if plan.get('flow'):
            from .flow_expectancy import validate_flow
            if digest(plan['flow']['sources'])!=plan['flow']['bundle_sha256']:
                raise ValueError('flow bundle hash mismatch')
            for source in plan['flow']['sources']:
                flow_raw=checked_path(source['path']).read_bytes()
                if hashlib.sha256(flow_raw).hexdigest()!=source['file_sha256']:
                    raise ValueError('flow source hash changed')
                validate_flow(json.loads(flow_raw))
        models=[];evaluations=[]
        for path in sorted(folder.glob('*-model.json')):
            path=checked_path(path);payload=path.read_bytes();model=json.loads(payload)
            if model['model_sha256']!=digest({k:v for k,v in model.items() if k!='model_sha256'}):
                raise ValueError('saved model hash mismatch')
            if model['last_training_label_ms']>=model['fold']['validation_start_ms']:
                raise ValueError('model training labels cross validation boundary')
            models.append({'path':str(path),'file_sha256':hashlib.sha256(payload).hexdigest(),
                'model_sha256':model['model_sha256'],'training_samples':model['training_samples'],
                'file_written_at_ns':path.stat().st_mtime_ns})
        for path in sorted(folder.glob('*-evaluation.json')):
            path=checked_path(path);payload=path.read_bytes();entry=json.loads(payload)
            audit=verify_ledger(entry,plan)
            evaluations.append({'path':str(path),'file_sha256':hashlib.sha256(payload).hexdigest(),'ledger_audit':audit})
        comparisons=result['results'] if 'results' in result else {'adaptive_algorithm':{
            'summary':result['summary'],'checks':result['checks'],'development_qualified':result['development_qualified']}}
        output.append({'id':item['id'],'plan_file_sha256':hashlib.sha256(plan_path.read_bytes()).hexdigest(),
            'results_file_sha256':hashlib.sha256(raw).hexdigest(),'comparisons':comparisons,
            'saved_models':models,'evaluations':evaluations,
            'qualified':result.get('selected_candidates',[item['id']] if result.get('development_qualified') else []),
            'source_integrity_verified':True,'frozen_implementation_verified':True})
    index={'schema':'net-expectancy-experiment-registry/v1','recorded_at':dt.datetime.now(dt.timezone.utc).isoformat(),
        'specification_sha256':digest(specification),
        'python':platform.python_version(),'runtime_dependencies':'standard library; existing cryptography used only by historical GET acquisition',
        'experiments':output,'total_saved_model_artifacts':sum(len(e['saved_models']) for e in output),
        'total_outer_fold_evaluations':sum(len(e['evaluations']) for e in output),
        'actual_orders_executed':False,'protected_observations_accessed':False,'model_promoted':False}
    index['registry_sha256']=digest(index)
    return index


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('specification');parser.add_argument('output');args=parser.parse_args(argv)
    result=build(json.loads(checked_path(args.specification).read_text()));write_new(args.output,result)
    print(json.dumps({'experiments':len(result['experiments']),'models':result['total_saved_model_artifacts'],
        'outer_fold_evaluations':result['total_outer_fold_evaluations'],
        'qualified':{e['id']:e['qualified'] for e in result['experiments']}},indent=2))
    return 0


if __name__=='__main__':raise SystemExit(main())
