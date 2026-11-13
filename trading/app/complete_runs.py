"""Explicit complete derivatives of incomplete development captures; no filling."""
import hashlib
import json
from .historical_candles import validate_candle_dataset
from .dataset_eligibility import assess_dataset,require_qualification_eligibility


def derive_complete_runs(dataset,source_file_sha256,*,minimum_candles):
    if (not isinstance(source_file_sha256,str) or len(source_file_sha256)!=64
            or any(c not in '0123456789abcdef' for c in source_file_sha256)):
        raise ValueError('valid parent file hash required')
    if type(minimum_candles) is not int or minimum_candles<1:
        raise ValueError('positive minimum coverage required')
    source=dataset['source']
    if source.get('provider')!='CoinSwitch' or source.get('access')!='authenticated-read-only':
        raise ValueError('only genuine single-source CoinSwitch development captures are supported')
    checked={k:v for k,v in dataset.items() if k!='integrity'}
    result=validate_candle_dataset(checked)
    if result.duplicate_count or result.reordered or any(
        not (f.startswith('dataset contains ') and f.endswith(' candle gap(s)')) for f in result.failures):
        raise ValueError('invalid parent rows cannot be repaired by segmentation')
    interval=source['interval_minutes']*60
    runs=[]
    for row in dataset['candles']:
        if row['interval_seconds']!=interval:raise ValueError('mixed native intervals')
        if not runs or runs[-1][-1]['close_time']!=row['open_time']:runs.append([])
        runs[-1].append(row)
    outputs=[]
    for index,rows in enumerate(runs,1):
        if len(rows)<minimum_candles:continue
        output={k:v for k,v in dataset.items() if k not in ('candles','integrity')}
        output['candles']=[dict(row) for row in rows]
        output['source']={**source,'requested_start_time':rows[0]['open_time'],
            'requested_end_time':rows[-1]['close_time']}
        output['development_derivation']={'schema':'complete-development-run/v1',
            'parent_file_sha256':source_file_sha256,'parent_candle_sha256':result.candle_sha256,
            'parent_requested_start_time':source['requested_start_time'],
            'parent_requested_end_time':source['requested_end_time'],
            'parent_integrity_passed':dataset.get('integrity',{}).get('passed') is True,
            'run_id':index,'selection':'coverage only; no price/performance inspection',
            'minimum_candles':minimum_candles,'scope':'development_only','cross_gap_windows':False}
        output['integrity']=validate_candle_dataset(output).as_details()
        require_qualification_eligibility(assess_dataset(output))
        outputs.append(output)
    return outputs
