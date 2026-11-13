"""Independently hash and reconcile the fixed post-0.3.0 development studies."""
import hashlib
import json
from pathlib import Path
import shutil
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from trading.app import net_expectancy as r
from trading.app.cost_research import write_new
from trading.app.expectancy_registry import checked_path, verify_ledger
from trading.app.backtest import load_historical_candles

STUDIES=(('frozen-signal-information','frozen-signal-information-plan.json','frozen-signal-information-results'),
         ('calibrated-expectancy-stage9','calibrated-expectancy-stage9-plan.json','calibrated-expectancy-stage9-results'),
         ('event-information-stage10','event-information-stage10-plan.json','event-information-stage10-results'))


def main(output):
    entries=[];root=Path('artifacts/research-registry')
    for identity,plan_name,folder_name in STUDIES:
        plan_path=root/plan_name;plan=json.loads(plan_path.read_text())
        folder=Path('artifacts')/folder_name;result=json.loads((folder/'results.json').read_text())
        if result['plan_sha256']!=r.digest(plan) or result['result_sha256']!=r.digest({k:v for k,v in result.items() if k!='result_sha256'}):
            raise ValueError('plan/result digest mismatch')
        source_plan=plan.get('source_plan',plan)
        for source in source_plan['sources']:
            _,hashes=load_historical_candles(checked_path(source['path']))
            if hashes!=source['hashes'] or hashes['qualification_eligible']!='true':raise ValueError('development source changed or incomplete')
        snapshot=root/f'{identity}-source'
        for name,expected in plan['implementation_sha256'].items():
            path=checked_path(name)
            if hashlib.sha256(path.read_bytes()).hexdigest()!=expected:raise ValueError('research implementation changed')
            target=snapshot/name;target.parent.mkdir(parents=True,exist_ok=True)
            if target.exists():
                if hashlib.sha256(target.read_bytes()).hexdigest()!=expected:raise ValueError('frozen source changed')
            else:shutil.copyfile(path,target)
        model_count=ledger_count=trade_count=0
        for path in folder.glob('*-model.json'):
            model=json.loads(path.read_text())
            if model['model_sha256']!=r.digest({k:v for k,v in model.items() if k!='model_sha256'}):raise ValueError('model self hash mismatch')
            if identity=='calibrated-expectancy-stage9':
                number=int(path.name.split('-')[1]);fold=plan['folds'][number]
                if model['last_training_label_ms']>=fold['inner_start_ms']:raise ValueError('learner reaches calibration')
                c=json.loads((folder/f'fold-{number}-calibration.json').read_text())
                if c['last_label_ms']>=fold['validation_start_ms']:raise ValueError('calibration reaches validation')
            elif identity=='event-information-stage10':
                number=int(path.stem.rsplit('-',2)[1])
                if model['last_training_label_ms']>=plan['folds'][number]['validation_start_ms']:raise ValueError('event learner leaks')
            model_count+=1
        if identity=='calibrated-expectancy-stage9':
            for path in folder.glob('*-evaluation.json'):
                entry=json.loads(path.read_text())
                for scenarios in entry['policies'].values():
                    audit=verify_ledger({'fold':entry['fold'],'scenarios':scenarios},plan)
                    if audit['realized_daily_loss_over_100']:raise ValueError('daily risk breach')
                    ledger_count+=len(scenarios);trade_count+=audit['trade_scenario_records']
        files={str(path):hashlib.sha256(path.read_bytes()).hexdigest() for path in sorted(folder.glob('*.json'))}
        files[str(plan_path)]=hashlib.sha256(plan_path.read_bytes()).hexdigest()
        entries.append({'id':identity,'file_sha256':files,'plan_sha256':r.digest(plan),'result_sha256':result['result_sha256'],
            'models':model_count,'execution_ledgers':ledger_count,'reconciled_trade_scenario_records':trade_count,
            'development_sources_verified':True,'source_snapshot':str(snapshot),
            'qualified':result.get('development_qualified',False)})
    previous=root/'net-expectancy-registry-v4.json'
    if hashlib.sha256(previous.read_bytes()).hexdigest()!='a1cf2860add0f308ab19942df2cb0df7456b1e7e1b2b901de0a30aae7b456e46':
        raise ValueError('original registry changed')
    registry={'schema':'research-continuation-registry/v1','previous_registry_file_sha256':hashlib.sha256(previous.read_bytes()).hexdigest(),
        'studies':entries,'actual_orders_executed':False,'protected_observations_accessed':False,'model_promoted':False}
    registry['registry_sha256']=r.digest(registry);write_new(output,registry)
    print(json.dumps({e['id']:{k:e[k] for k in ('models','execution_ledgers','reconciled_trade_scenario_records')} for e in entries},indent=2))


if __name__=='__main__':
    if len(sys.argv)!=2:raise SystemExit('usage: verify_research_continuation.py NEW_OUTPUT.json')
    main(sys.argv[1])
