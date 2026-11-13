"""Independent completeness evidence; diagnostic data is never approval evidence."""
import hashlib
import json

from .historical_candles import validate_candle_dataset


def assess_dataset(dataset):
    failures=[]
    try:
        validation=validate_candle_dataset(dataset)
        failures.extend(validation.failures)
        source=dataset['source']
        start,end=source['requested_start_time'],source['requested_end_time']
        minutes=source['interval_minutes']
        if any(type(v) is not int for v in (start,end,minutes)) or minutes<=0:
            raise ValueError('invalid requested range')
        step=minutes*60000
        if start<0 or start>=end or start%step or end%step:
            raise ValueError('unaligned requested range')
        expected=(end-start)//step
        rows=dataset['candles']
        times=[int(r['open_time']) for r in rows]
        if len(rows)!=expected or not times or times[0]!=start or times[-1]+step!=end:
            failures.append('requested timestamp coverage is incomplete')
        if any(t!=start+i*step for i,t in enumerate(times)):
            failures.append('requested timestamps are missing or unexpected')
        identities={(r.get('source'),r.get('symbol'),int(r['interval_seconds'])) for r in rows}
        if len(identities)!=1:
            failures.append('mixed candle provenance')
        if (source.get('provider')!='CoinSwitch' or source.get('access')!='authenticated-read-only'
                or dataset.get('credentials_used') is not True):
            failures.append('unverified market provenance')
        if not isinstance(dataset.get('integrity'),dict) or dataset['integrity'].get('passed') is not True:
            failures.append('missing or failed stored integrity')
        if dataset.get('ml_segment') is not None:
            # Old segment artifacts retain parent range metadata: never silently
            # promote them under a whole-range completeness contract.
            failures.append('derived segment requires its independent segment approval protocol')
        candle_hash=validation.candle_sha256
    except (ValueError,TypeError,KeyError) as exc:
        failures.append('invalid dataset structure: '+type(exc).__name__)
        expected=None
        candle_hash=None
    evidence={'schema':'research-dataset-eligibility/v1',
        'dataset_sha256':hashlib.sha256(json.dumps(dataset,sort_keys=True,separators=(',',':')).encode()).hexdigest(),
        'candle_sha256':candle_hash,'expected_candles':expected,
        'eligible_for_strategy_qualification':not failures,
        'failures':list(dict.fromkeys(failures)),
        'policy':'full requested range; no interpolation, cross-source patches or implicit gap waiver'}
    evidence['evidence_sha256']=hashlib.sha256(json.dumps(evidence,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    return evidence


def require_qualification_eligibility(evidence):
    if not isinstance(evidence,dict) or evidence.get('schema')!='research-dataset-eligibility/v1':
        raise ValueError('qualification requires independent dataset-integrity evidence')
    expected=hashlib.sha256(json.dumps({k:v for k,v in evidence.items() if k!='evidence_sha256'},sort_keys=True,separators=(',',':')).encode()).hexdigest()
    if evidence.get('evidence_sha256')!=expected:
        raise ValueError('dataset eligibility evidence hash mismatch')
    if evidence.get('eligible_for_strategy_qualification') is not True or evidence.get('failures')!=[]:
        raise ValueError('incomplete or unverified datasets cannot qualify strategies')
