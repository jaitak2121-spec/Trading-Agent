"""Cost attribution and market-spread diagnostics; never selects or places trades."""
import argparse
import datetime as dt
from decimal import Decimal
import hashlib
import json
import os
import time
from pathlib import Path

from ..core.money import to_decimal


def canonical_hash(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def _number(value):
    return to_decimal(value,field='cost evidence')


def trade_cost_diagnostic(trades):
    net=fees=turnover=slippage=Decimal(0)
    wins=[];losses=[]
    for trade in trades:
        qty,entry,exit_price=[_number(trade[k]) for k in ('quantity','entry_price','exit_price')]
        entry_fee,exit_fee,slip=[_number(trade[k]) for k in ('entry_fee','exit_fee','slippage_cost')]
        pnl=_number(trade['net_pnl'])
        if min(qty,entry,exit_price)<=0 or min(entry_fee,exit_fee,slip)<0:
            raise ValueError('invalid executed quantity/price/cost')
        if abs(qty*(exit_price-entry)-entry_fee-exit_fee-pnl)>Decimal('.00000001'):
            raise ValueError('trade accounting does not reconcile')
        net+=pnl;fees+=entry_fee+exit_fee;turnover+=qty*(entry+exit_price);slippage+=slip
        if pnl>0:wins.append(pnl)
        elif pnl<0:losses.append(-pnl)
    before_fees=net+fees
    average_win=sum(wins)/len(wins) if wins else None
    average_loss=sum(losses)/len(losses) if losses else None
    return {'closed_trades':len(trades),'net_pnl':str(net),'fee_total':str(fees),
        'reported_slippage_total':str(slippage),'executed_notional_turnover':str(turnover),
        'same_fill_pnl_before_fees':str(before_fees),
        'loses_at_zero_fees_on_recorded_fills':before_fees<0 if trades else None,
        'maximum_flat_fee_bps_at_break_even':str(before_fees/turnover*10000) if turnover and before_fees>=0 else None,
        'additional_net_edge_needed_bps_per_turnover':str(max(-net,Decimal(0))/turnover*10000) if turnover else None,
        'observed_win_rate':str(Decimal(len(wins))/len(trades)) if trades else None,
        'observed_average_win':None if average_win is None else str(average_win),
        'observed_average_loss':None if average_loss is None else str(average_loss),
        'break_even_win_rate_with_fixed_observed_payoffs':str(average_loss/(average_loss+average_win)) if average_win and average_loss else None,
        'interpretation':'accounting on frozen fills, not a zero-cost counterfactual backtest or a forecast'}


def analyze_research_costs(report):
    if report.get('schema')!='native-five-minute-development-results/v1':
        raise ValueError('expected the explicit native development report')
    if report.get('result_sha256')!=canonical_hash({k:v for k,v in report.items() if k!='result_sha256'}):
        raise ValueError('development result hash mismatch')
    output={}
    for name,candidate in report['results'].items():
        output[name]={}
        for scenario,details in candidate['scenarios'].items():
            all_trades=[];folds=[]
            for entry in details['folds']:
                result=entry['result'];m=result['metrics'];diagnostic=trade_cost_diagnostic(result['trades'])
                if (diagnostic['closed_trades']!=m['trade_count']
                        or abs(_number(diagnostic['net_pnl'])-_number(m['realized_pnl']))>Decimal('.00000001')
                        or abs(_number(diagnostic['fee_total'])-_number(m['fee_total']))>Decimal('.00000001')):
                    raise ValueError('fold accounting does not reconcile')
                folds.append({'fold':entry['fold'],'diagnostic':diagnostic})
                all_trades.extend(result['trades'])
            output[name][scenario]={'folds':folds,'pooled_frozen_fills':trade_cost_diagnostic(all_trades)}
    result={'schema':'frozen-fill-cost-diagnostics/v1','source_result_sha256':report['result_sha256'],
        'diagnostics':output,'strategy_changed':False,'thresholds_tuned':False,'model_promoted':False,
        'actual_orders_sent':False,'limitations':[
            'pre-fee P&L retains recorded spread/slippage; no counterfactual executions were simulated',
            'different fees can change sizing, risk refusals, later trades and prices in a full replay',
            'observed payoff-based break-even rates are descriptive and not predictive',
            'tax/TDS, depth, latency, and actual venue fill-fee settlement remain unmodeled']}
    result['report_sha256']=canonical_hash(result)
    return result


def summarize_spreads(samples,*,symbol,maximum_age_ms=5000):
    spreads=[];seen=set();duplicates=0;stale=0
    first=last=None
    for row in samples:
        if row.get('symbol')!=symbol:raise ValueError('mixed market in quote evidence')
        at,received=row['timestamp'],row['received_at_ms']
        if type(at) is not int or type(received) is not int or at<0 or received<0:
            raise ValueError('quote timestamps must be integer milliseconds')
        if at>received+maximum_age_ms:raise ValueError('future quote timestamp')
        bid,ask=_number(row['bid']),_number(row['ask'])
        if not 0<bid<=ask:raise ValueError('invalid/crossed public quote')
        if received-at>maximum_age_ms:stale+=1;continue
        key=(at,bid,ask)
        if key in seen:duplicates+=1;continue
        seen.add(key)
        spreads.append((ask-bid)/((ask+bid)/2)*10000)
        first=received if first is None else min(first,received)
        last=received if last is None else max(last,received)
    spreads.sort()
    def quantile(percent):
        if not spreads:return None
        position=Decimal(len(spreads)-1)*Decimal(percent)/100
        lower=int(position);upper=min(lower+1,len(spreads)-1)
        return str(spreads[lower]+(spreads[upper]-spreads[lower])*(position-lower))
    return {'schema':'public-top-spread-summary/v1','symbol':symbol,'samples_received':len(samples),
        'fresh_unique_samples':len(spreads),'stale_samples_excluded':stale,'duplicates_excluded':duplicates,
        'observed_receive_span_ms':None if first is None else last-first,
        'spread_bps':{'minimum':None if not spreads else str(spreads[0]),'median':quantile(50),
            'p90':quantile(90),'p95':quantile(95),'p99':quantile(99),'maximum':None if not spreads else str(spreads[-1])},
        'live_cost_qualification':False,'slippage_or_market_impact_estimated':False,
        'limitation':'descriptive top-of-book sample; no depth, queue, latency or future-spread guarantee'}


def capture_public_spreads(output,*,symbol='BTC/USDT',exchange='c2c1',seconds=120,include_depth=False):
    from ..adapters.coinswitch import CoinSwitchSpotStream
    from ..adapters.observation_evidence import write_json_atomically
    from .coinswitch_observation import run_coinswitch_observation
    if type(seconds) is not int or not 60<=seconds<=3600:
        raise ValueError('capture duration must be 60 to 3600 seconds')
    target=Path(output)
    if target.exists():raise FileExistsError('capture output exists; refusing overwrite')
    samples=[]
    evidence={'schema':'public-depth-capture/v1' if include_depth else 'public-spread-capture/v1','symbol':symbol,'exchange':exchange,
        'duration_requested_seconds':seconds,'started_at':dt.datetime.now(dt.timezone.utc).isoformat(),
        'quotes':samples,'credentials_used':False,'orders_executed':False,'completed':False}
    last_saved=[0.0]
    def sink(quote):
        if len(samples)>=(10000 if include_depth else 200000):raise ValueError('bounded public quote capture exceeded sample budget')
        value=dict(quote)
        raw=value['timestamp']
        if isinstance(raw,str) and raw.isdigit():value['timestamp']=int(raw)
        value['received_at_ms']=time.time_ns()//1000000
        samples.append(value)
        now=time.monotonic()
        if now-last_saved[0]>=5:
            write_json_atomically(target,evidence)
            last_saved[0]=now
    # Preserve a partial record even when feed startup fails. This finite
    # observation never automatically resumes or starts a trading process.
    target.parent.mkdir(parents=True,exist_ok=True)
    with target.open('x') as initial:json.dump(evidence,initial,sort_keys=True)
    try:
        stream_options={'symbol':symbol,'exchange':exchange}
        if include_depth:stream_options['include_depth']=True
        observation=run_coinswitch_observation(CoinSwitchSpotStream(**stream_options),
            duration_seconds=seconds,quote_sink=sink)
        evidence['observation']=observation.as_details()
        evidence['spread_summary']=summarize_spreads(samples,symbol=symbol)
        evidence['completed']=True
    finally:
        evidence['finished_at']=dt.datetime.now(dt.timezone.utc).isoformat()
        write_json_atomically(target,evidence)
    return evidence


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    commands=parser.add_subparsers(dest='operation',required=True)
    analyze=commands.add_parser('analyze')
    analyze.add_argument('input');analyze.add_argument('output')
    analyze.add_argument('--sha256',required=True,help='exact frozen development report file hash')
    observe=commands.add_parser('observe')
    observe.add_argument('output');observe.add_argument('--seconds',type=int,default=120)
    observe.add_argument('--symbol',default='BTC/USDT');observe.add_argument('--exchange',default='c2c1')
    fees=commands.add_parser('capture-fees',help='explicit account-specific fee GET; no balances or orders')
    fees.add_argument('output');fees.add_argument('--exchange',choices=('c2c1','c2c2','coinswitchx'),default='c2c1')
    fees.add_argument('--base-asset',default='BTC')
    fees.add_argument('--authorize-read-only-fee-lookup',action='store_true')
    args=parser.parse_args(argv)
    if args.operation=='capture-fees':
        if not args.authorize_read_only_fee_lookup:parser.error('explicit fee-read authorization flag required')
        target=Path(args.output)
        if target.exists():parser.error('fee output exists; refusing overwrite')
        key,secret=os.environ.get('COINSWITCH_API_KEY'),os.environ.get('COINSWITCH_API_SECRET')
        if not key or not secret:parser.error('both credentials must be supplied only to the fee-reader process')
        target.parent.mkdir(parents=True,exist_ok=True)
        from ..adapters.coinswitch.trading_fees import CoinSwitchFeeReader,parse_trading_fee,CoinSwitchCandleTransportError
        from ..core.secrets import Secret
        try:
            response=CoinSwitchFeeReader(api_key=Secret(key),api_secret=Secret(secret)).fetch(args.exchange)
            profile=parse_trading_fee(response.body,exchange=args.exchange,base_asset=args.base_asset,
                                      captured_at_ms=time.time_ns()//1000000)
        except (CoinSwitchCandleTransportError,ValueError) as exc:
            statuses=getattr(exc,'statuses',())
            parser.error('fee lookup failed ('+type(exc).__name__+', statuses '+str(list(statuses))+'); no profile or orders created')
        profile['credentials_used']=True
        profile['http_statuses']=list(response.statuses)
        profile.pop('snapshot_sha256')
        profile['snapshot_sha256']=canonical_hash(profile)
        descriptor=os.open(target,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
        with os.fdopen(descriptor,'w') as output:json.dump(profile,output,sort_keys=True,indent=2);output.write('\n')
        print(json.dumps({'profile':str(target),'maker_bps':profile['effective_maker_bps'],
            'taker_bps':profile['effective_taker_bps'],'orders_executed':False},sort_keys=True))
        return 0
    if args.operation=='observe':
        evidence=capture_public_spreads(args.output,symbol=args.symbol,exchange=args.exchange,seconds=args.seconds)
        print(json.dumps({'output':args.output,'observation':evidence['observation'],
            'spread_summary':evidence['spread_summary']},indent=2,sort_keys=True))
        return 0 if evidence['observation']['passed'] else 2
    source=Path(args.input).resolve(strict=True)
    if any(s in str(source).lower() for s in ('confirmation','segment-7','segment_7','segment7')):
        parser.error('protected observation paths are not cost-diagnostic inputs')
    raw=source.read_bytes()
    if hashlib.sha256(raw).hexdigest()!=args.sha256:parser.error('source file hash mismatch')
    result=analyze_research_costs(json.loads(raw))
    result['source_file_sha256']=args.sha256
    result.pop('report_sha256')
    result['report_sha256']=canonical_hash(result)
    with Path(args.output).open('x') as output:json.dump(result,output,sort_keys=True,indent=2);output.write('\n')
    print(args.output)
    return 0


if __name__=='__main__':raise SystemExit(main())
