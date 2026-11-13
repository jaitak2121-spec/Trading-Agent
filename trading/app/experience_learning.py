"""Bounded, chronological learning from durable public candles; never deploys."""
import datetime as dt
import hashlib
import json
from pathlib import Path
import time

from .historical_candles import validate_candle_dataset
from .model_training import train_direction_model,evaluate_direction_model
from ..adapters.observation_evidence import write_json_atomically
from ..adapters.persistence.sqlite import read_experience_rows,read_paper_origin


def verified_completion(path):
    record=json.loads(path.read_text())
    for item in record['artifacts']:
        if hashlib.sha256(Path(item['path']).read_bytes()).hexdigest()!=item['sha256']:
            raise ValueError('completed experience artifact changed')
    return record


def snapshot_experience(database,*,symbol,exchange,maximum_candles=10080,minimum_candles=720):
    if type(maximum_candles) is not int or type(minimum_candles) is not int or not 720<=minimum_candles<=maximum_candles<=43200:
        raise ValueError('experience requires 720..43200 bounded candles')
    if symbol!='BTC/USDT' or exchange not in ('c2c1','c2c2'):
        raise ValueError('explicit supported public BTC market required')
    path=Path(database).resolve(strict=True)
    if any(s in str(path).lower() for s in ('confirmation','segment7','segment-7','segment_7')):
        raise ValueError('protected observations cannot be experience inputs')
    expected={'kind':'real_market_paper','candle_policy':'complete-connected-minutes/v1','provider':'CoinSwitch public feed','symbol':symbol,'exchange':exchange}
    if read_paper_origin(path)!=expected:raise ValueError('experience requires a bound genuine-market paper database')
    rows=read_experience_rows(path,symbol,maximum_candles)
    # Use only the latest contiguous segment. Preserve the full parent in SQLite.
    start=0
    for i,row in enumerate(rows):
        if (row.get('symbol')!=symbol or row.get('source')!='coinswitch-'+exchange
                or row.get('interval_seconds')!=60 or type(row.get('open_time')) is not int
                or row['open_time']+60000>int(dt.datetime.now(dt.timezone.utc).timestamp()*1000)):
            raise ValueError('invalid, incomplete or mismatched experience provenance')
        if i:
            delta=row['open_time']-rows[i-1]['open_time']
            if delta<=0 or delta%60000:raise ValueError('invalid experience chronology')
            if delta!=60000:start=i
    if len(rows)-start<minimum_candles:raise ValueError('insufficient latest contiguous genuine experience; collect more, never fill gaps')
    selected=rows[start:]
    dataset={'schema':'coinswitch-public-candles/v1','symbol':symbol,'exchange':exchange,
        'public_data_only':True,'credentials_used':False,'live_trading':False,'training_only':True,
        'candles':selected,'experience':{'database':str(path),'selection':'latest contiguous run; coverage only',
            'discarded_pre_gap_rows':start,'snapshot_rows_sha256':hashlib.sha256(json.dumps(rows,sort_keys=True).encode()).hexdigest()}}
    integrity=validate_candle_dataset(dataset)
    if not integrity.passed:raise ValueError('experience integrity failed: '+'; '.join(integrity.failures))
    dataset['integrity']=integrity.as_details()
    return dataset


def run_experience_job(specification):
    dataset=snapshot_experience(specification['state_db'],symbol=specification['symbol'],exchange=specification['exchange'],
        maximum_candles=specification.get('maximum_candles',10080),minimum_candles=specification.get('minimum_candles',720))
    identity=hashlib.sha256(json.dumps(dataset,sort_keys=True).encode()).hexdigest()
    folder=Path(specification['output_directory']);folder.mkdir(parents=True,exist_ok=True)
    completed=folder/f'experience-{identity}-complete.json'
    if completed.exists():
        verified_completion(completed)
        return completed
    prior=sorted(folder.glob('experience-*-complete.json'),key=lambda p:p.stat().st_mtime_ns)
    minimum_new=specification.get('minimum_new_candles',1440)
    if type(minimum_new) is not int or not 1<=minimum_new<=43200:raise ValueError('bounded minimum new experience required')
    if prior:
        previous=verified_completion(prior[-1])
        if sum(row['open_time']>previous['last_open_ms'] for row in dataset['candles'])<minimum_new:
            return prior[-1]  # Persistent cooldown survives scheduler restarts.
    # An interrupted attempt is retained. A retry writes a new immutable attempt.
    attempt=folder/f'experience-{identity}-{time.time_ns()}';attempt.mkdir()
    cut=len(dataset['candles'])*2//3
    development={**dataset,'candles':dataset['candles'][:cut]};development.pop('integrity')
    evaluation={**dataset,'candles':dataset['candles'][cut:]};evaluation.pop('integrity')
    for value in (development,evaluation):
        check=validate_candle_dataset(value)
        if not check.passed:raise ValueError('chronological experience split failed')
        value['integrity']=check.as_details()
    trained=train_direction_model(development)
    model=dict(trained.model)
    # Classification screening is NOT an independent net-expectancy qualification.
    model['deployment_hold']='independent cost-aware qualification and explicit paper approval required'
    report=evaluate_direction_model(model,evaluation)
    status='awaiting_independent_qualification' if trained.eligible_for_paper_validation and report.eligible_for_paper_trial else 'rejected'
    artifacts=[]
    for name,value in (('dataset',dataset),('development',development),('evaluation-data',evaluation),
                        ('model',model),('evaluation',report.evidence)):
        path=write_json_atomically(attempt/f'{name}.json',value)
        artifacts.append({'path':str(path.resolve()),'sha256':hashlib.sha256(path.read_bytes()).hexdigest()})
    registry_identity=None
    if specification.get('registry_db'):
        from .model_registry import ModelRegistry
        from .model_deployment import model_identity
        from ..adapters.persistence import SQLiteState,SQLiteAuditSink
        from ..core.audit import AuditLog
        from ..core.authz import Principal,Role
        state=SQLiteState(specification['registry_db'])
        try:
            sink=SQLiteAuditSink(state);registry=ModelRegistry(state,AuditLog(sink,initial_records=sink.records))
            operator=Principal('scheduled-experience-learning',Role.OPERATOR)
            registry_identity=model_identity(model)
            existing={e['identity']:e for e in registry.entries()}.get(registry_identity)
            if existing is None:
                registry.register(attempt/'model.json',attempt/'evaluation.json',operator,reason='unapproved chronological experience screen')
            if status=='rejected' and (existing is None or existing['state']=='candidate'):
                registry.transition(registry_identity,'rejected',operator,reason='failed predefined chronological classification screen')
        finally:state.close()
    return write_json_atomically(completed,{'schema':'experience-learning-run/v1','dataset_sha256':identity,
        'last_open_ms':dataset['candles'][-1]['open_time'],'candle_count':len(dataset['candles']),
        'status':status,'training':trained.as_details(),'evaluation':report.evidence,'artifacts':artifacts,
        'trained_at':dt.datetime.now(dt.timezone.utc).isoformat(),'fit_source':'development only; internal chronological validation',
        'evaluation_role':'future development screen; not sealed confirmation','registry_identity':registry_identity,
        'deployed':False,'live_orders':False})


def learning_status(directory):
    """Bounded read-only operational summary; no model/dataset observations read."""
    root=Path(directory).resolve()
    if any(s in str(root).lower() for s in ('confirmation','segment7','segment-7','segment_7')):
        raise ValueError('protected observations are not learning reports')
    reports=sorted(root.glob('experience-*-complete.json'),key=lambda p:p.stat().st_mtime_ns)
    scheduler_path=root/'scheduler-status.json'
    scheduler=json.loads(scheduler_path.read_text()) if scheduler_path.exists() else {'phase':'not_started'}
    if scheduler.get('at'):
        age=(dt.datetime.now(dt.timezone.utc)-dt.datetime.fromisoformat(scheduler['at'])).total_seconds()
        if age>scheduler['interval_seconds']+scheduler['timeout_seconds']+60:scheduler['phase']='stale_or_stopped'
    if not reports:return {'status':'awaiting_experience','minimum_contiguous_candles':720,'deployed':False,'scheduler':scheduler}
    path=reports[-1]
    if path.resolve().parent!=root:raise ValueError('learning report cannot escape its directory')
    report=json.loads(path.read_text())
    if report.get('schema')!='experience-learning-run/v1' or report.get('deployed') is not False or report.get('live_orders') is not False:
        raise ValueError('invalid experience-learning status')
    return {**{name:report.get(name) for name in ('status','trained_at','dataset_sha256','registry_identity','candle_count','last_open_ms','training','evaluation','deployed')},'scheduler':scheduler}
