"""Public visible-depth diagnostics; no execution or strategy qualification."""
import argparse
import datetime as dt
from decimal import Decimal
import hashlib
import json
import math
from pathlib import Path

from .execution_costs import _number, canonical_hash, capture_public_spreads, summarize_spreads


def assess_capture_quality(evidence):
    """Strict research eligibility, never trading readiness or an API SLA.

    Fixed limits are declared before subsequent collection. Old captures with
    insufficient metadata fail closed while remaining available for diagnosis.
    """
    failures = []
    policy = {'maximum_receive_gap_ms':5000, 'maximum_clock_divergence_seconds':2,
              'minimum_fresh_unique_snapshots':2, 'maximum_feed_error_polls':0,
              'maximum_reconnect_attempts':0}
    timing = {}
    try:
        start = dt.datetime.fromisoformat(evidence['started_at'])
        finish = dt.datetime.fromisoformat(evidence['finished_at'])
        if start.tzinfo is None or finish.tzinfo is None:
            raise ValueError('timezone required')
        capture_wall = (finish-start).total_seconds()
        wall = capture_wall
        requested = evidence['duration_requested_seconds']
        observation = evidence['observation']
        start_ms=start.timestamp()*1000; finish_ms=finish.timestamp()*1000
        basis='legacy capture boundaries including cleanup'
        if ('observation_started_at_ns' in observation or 'observation_finished_at_ns' in observation):
            wall_start=observation['observation_started_at_ns']
            wall_finish=observation['observation_finished_at_ns']
            if (type(wall_start) is not int or type(wall_finish) is not int or
                    not start_ms<=wall_start/1000000<=wall_finish/1000000<=finish_ms):
                raise ValueError('invalid observation boundaries')
            wall=(wall_finish-wall_start)/1000000000
            start_ms=wall_start/1000000; finish_ms=wall_finish/1000000
            basis='explicit observer boundaries before transport cleanup'
        elapsed = observation['duration_elapsed_seconds']
        if any(isinstance(value,bool) or not isinstance(value,(int,float)) or
               not math.isfinite(value) or value<=0 for value in (requested,elapsed,wall)):
            raise ValueError('positive finite durations required')
        timing = {'wall_span_seconds':wall,'capture_wall_span_seconds':capture_wall,
                  'boundary_basis':basis,'monotonic_observation_seconds':elapsed,
                  'clock_divergence_seconds':abs(wall-elapsed)}
        if abs(wall-elapsed)>policy['maximum_clock_divergence_seconds']:
            failures.append('wall and monotonic clocks diverged')
        if min(wall,elapsed)<requested or observation['duration_requested_seconds']!=requested:
            failures.append('requested observation duration not covered')
        if (observation['symbol'],observation['exchange'])!=(evidence['symbol'],evidence['exchange']):
            failures.append('observer market identity mismatch')
        for field in ('feed_error_polls','reconnect_attempts'):
            value=observation[field]
            if type(value) is not int or value!=0:
                failures.append('nonzero or invalid '+field)
        if type(observation['quote_messages']) is not int or observation['quote_messages']!=len(evidence['quotes']):
            failures.append('snapshot count differs from observer')
        times=[row['received_at_ms'] for row in evidence['quotes']]
        if len(times)<2 or any(type(value) is not int for value in times):
            raise ValueError('at least two integer receive timestamps required')
        if times!=sorted(times):
            failures.append('receive timestamps reversed')
        source_times=[row['timestamp'] for row in evidence['quotes']]
        if any(type(value) is not int for value in source_times) or source_times!=sorted(source_times):
            failures.append('invalid or reversed exchange timestamps')
        if min(times)<start_ms or max(times)>finish_ms:
            failures.append('receive timestamps outside capture boundaries')
        # Include startup and tail silence: a few clustered updates cannot
        # qualify an otherwise silent observation window.
        gaps=[times[0]-start_ms,finish_ms-times[-1]]+[b-a for a,b in zip(times,times[1:])]
        timing['maximum_receive_gap_ms']=max(gaps)
        timing['receive_span_ms']=max(times)-min(times)
        if max(gaps)>policy['maximum_receive_gap_ms']:
            failures.append('receive gap exceeds fixed research limit')
    except (KeyError,ValueError,TypeError,OverflowError):
        failures.append('missing or invalid timing/observer metadata')
    observation=evidence.get('observation')
    if (evidence.get('completed') is not True or not isinstance(observation,dict)
            or observation.get('passed') is not True or observation.get('failures')):
        failures.append('combined observation did not pass')
    return {'schema':'public-depth-quality/v2','policy':policy,'timing':timing,
            'eligible':not failures,'failures':failures,
            'scope':'continuous cost-observation research only; not strategy or live qualification'}


def snapshot_costs(row, *, symbol, exchange, notionals, fee_bps, maximum_age_ms=5000):
    """Sweep each side independently for base quantity = quote notional / midpoint.

    This is a static displayed-liquidity calculation, not a fill simulation.
    Incomplete sweeps deliberately have no full-size VWAP or cost estimate.
    """
    if row.get('exchange') != exchange:
        raise ValueError('mixed exchange in depth evidence')
    # Shared quote validation includes market, crossed prices and clock checks.
    spread = summarize_spreads([row], symbol=symbol, maximum_age_ms=maximum_age_ms)
    sides = {}
    for name, descending in (('bids', True), ('asks', False)):
        raw = row.get(name)
        if not isinstance(raw, list) or not 1 <= len(raw) <= 1000:
            raise ValueError('empty or excessive depth levels')
        levels = []
        for level in raw:
            if not isinstance(level, list) or len(level) != 2:
                raise ValueError('depth levels require price and quantity')
            price, quantity = map(_number, level)
            if min(price, quantity) <= 0:
                raise ValueError('nonpositive price or quantity')
            if levels and not (price < levels[-1][0] if descending else price > levels[-1][0]):
                raise ValueError('unsorted or duplicate depth price')
            levels.append((price, quantity))
        sides[name] = levels
    bid, ask = _number(row['bid']), _number(row['ask'])
    if bid != sides['bids'][0][0] or ask != sides['asks'][0][0]:
        raise ValueError('depth and quote best prices differ')
    fee = _number(fee_bps)
    sizes = [_number(value) for value in notionals]
    if not 0 <= fee < 10000 or not sizes or min(sizes) <= 0 or len(set(sizes)) != len(sizes):
        raise ValueError('invalid fee or quote notionals')
    if not spread['fresh_unique_samples']:
        return None
    mid = (bid + ask) / 2
    results = []
    for size in sizes:
        quantity = size / mid
        for side, levels, best in (('buy', sides['asks'], ask), ('sell', sides['bids'], bid)):
            remaining, quote = quantity, Decimal(0)
            for price, available in levels:
                taken = min(remaining, available)
                quote += taken * price
                remaining -= taken
                if not remaining:
                    break
            full = remaining == 0
            vwap = quote / quantity if full else None
            direction = 1 if side == 'buy' else -1
            results.append({'quote_notional_at_mid': str(size), 'side': side,
                'requested_base_quantity': str(quantity), 'visible_base_filled': str(quantity-remaining),
                'sufficient_visible_depth': full, 'vwap': None if vwap is None else str(vwap),
                'impact_from_best_bps': None if not full else str(direction*(vwap/best-1)*10000),
                'cost_from_mid_including_fee_bps': None if not full else
                    str((direction*(vwap-mid)+vwap*fee/10000)/mid*10000)})
    return results


def analyze_depth(evidence, *, notionals=('100','500','1000'), fee_bps='10'):
    if evidence.get('schema') != 'public-depth-capture/v1' or not evidence.get('completed'):
        raise ValueError('completed explicit public depth evidence required')
    if evidence.get('credentials_used') is not False or evidence.get('orders_executed') is not False:
        raise ValueError('expected credential-free observation only')
    observation = evidence.get('observation')
    if not isinstance(observation, dict) or type(observation.get('passed')) is not bool:
        raise ValueError('observation status required, including failed captures')
    buckets = {}; seen = set(); stale = duplicates = 0
    for row in evidence['quotes']:
        results = snapshot_costs(row, symbol=evidence['symbol'], exchange=evidence['exchange'],
                                 notionals=notionals, fee_bps=fee_bps)
        if results is None:
            stale += 1
            continue
        identity = canonical_hash({k: row[k] for k in ('timestamp','bids','asks')})
        if identity in seen:
            duplicates += 1
            continue
        seen.add(identity)
        for item in results:
            key = item['side'] + '-' + item['quote_notional_at_mid']
            buckets.setdefault(key, []).append(item)
    summary = {}
    for key, rows in buckets.items():
        values = sorted(_number(row['cost_from_mid_including_fee_bps']) for row in rows
                        if row['sufficient_visible_depth'])
        def percentile(fraction):
            if not values:
                return None
            position = Decimal(len(values)-1)*fraction
            lower = int(position); upper = min(lower+1,len(values)-1)
            return str(values[lower]+(values[upper]-values[lower])*(position-lower))
        summary[key] = {'snapshots':len(rows), 'insufficient_depth':len(rows)-len(values),
            'cost_from_mid_including_fee_bps': {'minimum':percentile(Decimal(0)),
                'median':percentile(Decimal('.5')), 'p95':percentile(Decimal('.95')),
                'maximum':percentile(Decimal(1))}}
    quality=assess_capture_quality(evidence)
    if len(seen)<quality['policy']['minimum_fresh_unique_snapshots']:
        quality['failures'].append('insufficient fresh unique snapshots')
    if stale:
        quality['failures'].append('stale snapshots excluded')
    quality['eligible']=not quality['failures']
    report = {'schema':'public-visible-depth-costs/v3', 'source_sha256':canonical_hash(evidence),
        'symbol':evidence['symbol'], 'exchange':evidence['exchange'],
        'fee_bps':str(_number(fee_bps)), 'quote_notionals_at_mid':list(notionals),
        'fresh_unique_snapshots':len(seen),'stale_excluded':stale,'duplicates_excluded':duplicates,
        'capture_observation_passed':observation['passed'],
        'capture_observation_failures':list(observation.get('failures',[])),
        'research_observation_quality':quality,
        'sweeps':summary, 'live_cost_qualification':False, 'orders_executed':False,
        'limitations':['Independent buy/sell sweeps use equal midpoint notional, not a round trip.',
            'Visible liquidity can cancel; no queue, latency, hidden liquidity or realized fill guarantee.',
            'Receive times are polling times, not measured exchange-to-client latency.',
            'Fee is an explicit scenario; no assumption about historical fees or settlement assets.',
            'Short event-weighted sample is not trading profitability evidence.']}
    report['report_sha256'] = canonical_hash(report)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', help='new public capture path; existing files refused')
    parser.add_argument('--seconds', type=int, default=120)
    parser.add_argument('--input', help='offline assessment of an existing public depth capture')
    parser.add_argument('--sha256', help='required exact input file hash for offline assessment')
    args = parser.parse_args(argv)
    target = Path(args.output)
    if args.input:
        if target.exists():parser.error('report output exists; refusing overwrite')
        source=Path(args.input).absolute()
        if any(part in str(source).lower() for part in ('confirmation','segment7','segment-7','segment_7')):
            parser.error('protected observation paths are not public capture inputs')
        if not args.sha256:parser.error('--sha256 is required with --input')
        # Resolve symlinks before any read; a harmless filename cannot bypass
        # the protected-observation guard.
        source=source.resolve(strict=True)
        if any(part in str(source).lower() for part in ('confirmation','segment7','segment-7','segment_7')):
            parser.error('protected observation paths are not public capture inputs')
        raw=source.read_bytes()
        if hashlib.sha256(raw).hexdigest()!=args.sha256:parser.error('input file hash mismatch')
        report=analyze_depth(json.loads(raw))
        report['source_file_sha256']=args.sha256
        report.pop('report_sha256');report['report_sha256']=canonical_hash(report)
        with target.open('x') as output:
            json.dump(report,output,sort_keys=True,indent=2);output.write('\n')
        print(json.dumps({'report':str(target),'quality':report['research_observation_quality']},indent=2))
        return 0 if report['research_observation_quality']['eligible'] else 2
    if args.sha256:parser.error('--sha256 requires --input')
    report_path = target.with_suffix('.costs.json')
    if report_path == target or report_path.exists():
        parser.error('derived report path already exists or conflicts')
    evidence = capture_public_spreads(target, seconds=args.seconds, include_depth=True)
    report = analyze_depth(evidence)
    report['source_file_sha256'] = hashlib.sha256(target.read_bytes()).hexdigest()
    report.pop('report_sha256')
    report['report_sha256'] = canonical_hash(report)
    with report_path.open('x') as output:
        json.dump(report, output, sort_keys=True, indent=2)
        output.write('\n')
    print(json.dumps({'capture':str(target),'report':str(report_path),
                      'observation':evidence['observation'],'costs':report},sort_keys=True,indent=2))
    return 0 if report['research_observation_quality']['eligible'] else 2


if __name__ == '__main__':
    raise SystemExit(main())
