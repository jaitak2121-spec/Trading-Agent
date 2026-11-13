"""Opt-in, resource-bounded development research; never confirms or deploys."""
import argparse
import hashlib
import json
import time
import os
import sys
from pathlib import Path

from ..adapters.process import GracefulShutdown, run_bounded_research_job


def checked_config(path):
    configuration = json.loads(Path(path).read_text())
    if configuration.get('enabled') is not True:
        return configuration
    for name, low, high in [('interval_seconds', 60, 31536000), ('timeout_seconds', 1, 86400),
                            ('cpu_seconds', 1, 86400), ('memory_mb', 128, 16384)]:
        value = configuration.get(name)
        if type(value) is not int or not low <= value <= high:
            raise ValueError('invalid bounded research setting: '+name)
    if configuration.get('operation') not in {'development_candidate', 'public_observation', 'model_monitor','experience_retraining'}:
        raise ValueError('only development research, public observation or monitoring is allowed')
    if configuration['operation'] == 'model_monitor':
        from .model_monitor import validate_monitor_policy
        validate_monitor_policy(configuration['policy'])
    if configuration['operation'] == 'public_observation':
        duration = configuration.get('observation_seconds')
        if type(duration) is not int or not 60 <= duration < configuration['timeout_seconds']:
            raise ValueError('observation duration must fit within the worker timeout')
    return configuration


def _load_bound(specification, name):
    item = specification[name]
    path = Path(item['path']).resolve(strict=True)
    lowered = str(path).lower()
    if any(token in lowered for token in ('confirmation', 'segment-7', 'segment_7', 'segment7', '/test.')):
        raise ValueError('protected artifact paths are not research inputs')
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != item['file_sha256']:
        raise ValueError('research input file hash mismatch: '+name)
    return json.loads(raw)


def run_development_job(specification):
    from .ml_research import (run_walk_forward_candidate, _sha256, _validate_development_dataset,
                              preregistered_experiment_manifest)
    development = _load_bound(specification, 'development')
    _validate_development_dataset(development)
    walk_forward = _load_bound(specification, 'walk_forward')
    preregistration = _load_bound(specification, 'preregistration')
    digest = preregistration['manifest_sha256']
    if digest != _sha256({k:v for k,v in preregistration.items() if k != 'manifest_sha256'}):
        raise ValueError('preregistration integrity failed')
    expected = preregistered_experiment_manifest(
        source_dataset_sha256=preregistration['source_dataset_sha256'],
        walk_forward_manifest_sha256=preregistration['walk_forward_manifest_sha256'])
    if preregistration != expected or development['source_dataset_sha256'] != preregistration['source_dataset_sha256']:
        raise ValueError('research protocol or source provenance changed')
    candidate = specification['candidate']
    if candidate not in preregistration['candidates']:
        raise ValueError('candidate was not preregistered')
    if walk_forward['manifest_sha256'] not in preregistration['walk_forward_manifest_sha256']:
        raise ValueError('fold manifest was not preregistered')
    result = run_walk_forward_candidate(development, walk_forward, candidate,
                                        preregistration_sha256=digest)
    destination = Path(specification['output_directory'])
    destination.mkdir(parents=True, exist_ok=True)
    # Never overwrite an experiment, even if scheduling repeats identical inputs.
    output = destination / f'development-{time.time_ns()}.json'
    with output.open('x') as handle:
        json.dump(result, handle, indent=2, sort_keys=True)
    return output


def run_collection_job(specification):
    import datetime as dt
    from ..adapters.coinswitch import CoinSwitchSpotStream
    from ..adapters.observation_evidence import write_observation_dataset, write_observation_evidence
    from .coinswitch_observation import run_coinswitch_observation
    stream = CoinSwitchSpotStream(symbol=specification['symbol'], exchange=specification['exchange'])
    rows = []
    started = dt.datetime.now(dt.timezone.utc)
    folder = Path(specification['output_directory'])
    folder.mkdir(parents=True, exist_ok=True)
    prefix = folder/f'public-observation-{time.time_ns()}'
    def persist_bar(bar):
        value = dict(bar)
        for previous in rows:
            if previous['open_time'] == value['open_time']:
                if previous != value:
                    raise ValueError('conflicting public candle replay')
                return
        rows.append(value)
        write_observation_dataset(str(prefix)+'-candles.json', symbol=stream.symbol, exchange=stream.exchange,
            candles=rows, started_at=started, finished_at=dt.datetime.now(dt.timezone.utc))
    report = run_coinswitch_observation(stream, duration_seconds=specification['observation_seconds'],
                                        poll_interval_seconds=1, candle_sink=persist_bar)
    finished = dt.datetime.now(dt.timezone.utc)
    write_observation_evidence(str(prefix)+'-report.json', report=report.as_details(),
                               started_at=started, finished_at=finished)
    write_observation_dataset(str(prefix)+'-candles.json', symbol=stream.symbol, exchange=stream.exchange,
                               candles=rows, started_at=started, finished_at=finished)
    if not report.passed:
        raise ValueError('public observation qualification failed; preserve its report')


def run_monitor_job(specification):
    from .model_monitor import prediction_monitor
    from ..adapters.persistence.maintenance import load_audit_records
    reference=_load_bound(specification,'reference_model')
    report=prediction_monitor(load_audit_records(specification['state_db']),
                              specification['model_sha256'],reference,specification['policy'])
    folder=Path(specification['output_directory'])
    folder.mkdir(parents=True,exist_ok=True)
    with (folder/f'monitor-{time.time_ns()}.json').open('x') as handle:
        json.dump(report,handle,indent=2,sort_keys=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('config')
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--worker', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    configuration = checked_config(args.config)
    if configuration.get('enabled') is not True:
        print('research scheduler disabled')
        return 0
    if args.worker:
        import resource
        cpu = configuration['cpu_seconds']
        memory = configuration['memory_mb'] * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))
        if sys.platform == 'darwin':
            if os.environ.get('TRADING_RESEARCH_PARENT_PID') != str(os.getppid()):
                raise ValueError('macOS research requires the supervising memory watchdog')
        else:
            resource.setrlimit(resource.RLIMIT_AS, (memory, memory))
        if configuration['operation'] == 'development_candidate':
            run_development_job(configuration)
        elif configuration['operation'] == 'public_observation':
            run_collection_job(configuration)
        elif configuration['operation']=='experience_retraining':
            from .experience_learning import run_experience_job
            try:run_experience_job(configuration)
            except ValueError as exc:
                if not str(exc).startswith('insufficient latest contiguous genuine experience'):raise
                from ..adapters.observation_evidence import write_json_atomically
                write_json_atomically(Path(configuration['output_directory'])/'learning-readiness.json',
                    {'status':'awaiting_experience','minimum_candles':configuration.get('minimum_candles',720),
                     'model_fitted':False,'deployed':False})
        else:
            run_monitor_job(configuration)
        return 0
    with GracefulShutdown() as shutdown:
        from ..adapters.observation_evidence import write_json_atomically
        import datetime as dt
        status_path=Path(configuration['output_directory'])/'scheduler-status.json'
        def status(phase,**details):
            return write_json_atomically(status_path,{'schema':'research-scheduler-status/v1','phase':phase,
                'operation':configuration['operation'],'pid':os.getpid(),'at':dt.datetime.now(dt.timezone.utc).isoformat(),
                'interval_seconds':configuration['interval_seconds'],'timeout_seconds':configuration['timeout_seconds'],
                'deployment_enabled':False,**details})
        while not shutdown.requested:
            started = time.monotonic()
            status('running')
            try:
                resources=run_bounded_research_job(Path(args.config).resolve(),
                    timeout_seconds=configuration['timeout_seconds'],memory_mb=configuration['memory_mb'],
                    cancelled=lambda: shutdown.requested)
                folder=Path(configuration['output_directory'])
                folder.mkdir(parents=True,exist_ok=True)
                with (folder/f'worker-{time.time_ns()}.json').open('x') as handle:
                    json.dump({'operation':configuration['operation'],'resources':resources,
                               'cpu_seconds':configuration['cpu_seconds'],'model_deployed':False},handle,indent=2)
                print(configuration['operation']+' completed; model deployment unchanged', flush=True)
                status('waiting',next_run_at=(dt.datetime.now(dt.timezone.utc)+dt.timedelta(seconds=configuration['interval_seconds'])).isoformat())
            except InterruptedError:
                status('stopped')
                break
            except (RuntimeError, TimeoutError, OSError) as exc:
                status('failed',error_type=type(exc).__name__)
                print(configuration['operation']+' failed; '+type(exc).__name__, flush=True)
                if args.once:
                    return 2
            if args.once:
                status('stopped')
                return 0
            shutdown.wait(max(0, configuration['interval_seconds']-(time.monotonic()-started)))
        status('stopped')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
