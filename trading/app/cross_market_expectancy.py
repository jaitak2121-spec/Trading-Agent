"""Stage 3: causal ETH context for BTC expectancy, sharing the stage-2 executor."""
import argparse
import hashlib
import json
import math
from pathlib import Path
import statistics

from . import hourly_expectancy as hourly
from . import net_expectancy as research
from .backtest import load_historical_candles
from .cost_research import write_new

AUX_FEATURES=('eth_return_24','eth_return_72','btc_minus_eth_return_24',
              'btc_eth_correlation_48','eth_relative_volatility_48','eth_volume_24_168','positive_breadth_24')


def auxiliary_features(btc,eth):
    """Both completed hourly windows end at the same timestamp; never fill gaps."""
    if len(btc)<289 or len(eth)<289:raise ValueError('cross-market context incomplete')
    btc=btc[-289:];eth=eth[-289:]
    if any(b.symbol!='BTC/USDT' for b in btc) or any(b.symbol!='ETH/USDT' for b in eth):
        raise ValueError('cross-market instrument identity mismatch')
    bv=research.features(btc,3600);ev=research.features(eth,3600)
    if btc[-1].close_time!=eth[-1].close_time:raise ValueError('cross-market timestamp mismatch')
    bc=[float(b.close.amount) for b in btc];ec=[float(b.close.amount) for b in eth]
    br=[b/a-1 for a,b in zip(bc[-49:],bc[-48:])]
    er=[b/a-1 for a,b in zip(ec[-49:],ec[-48:])]
    bm=statistics.fmean(br);em=statistics.fmean(er)
    denom=math.sqrt(sum((v-bm)**2 for v in br)*sum((v-em)**2 for v in er))
    correlation=sum((b-bm)*(e-em) for b,e in zip(br,er))/denom if denom else 0.
    b24=bc[-1]/bc[-25]-1;e24=ec[-1]/ec[-25]-1
    volumes=[float(b.volume.amount) for b in eth]
    average=statistics.fmean(volumes[-168:])
    return [e24,ec[-1]/ec[-73]-1,b24-e24,correlation,
            ev[6]/bv[6] if bv[6] else 0.,statistics.fmean(volumes[-24:])/average-1 if average else 0.,
            (int(b24>0)+int(e24>0))/2]


def prepare(paths,auxiliary_paths):
    plan=hourly.prepare(paths);sources=[]
    for path in auxiliary_paths:
        path=Path(path).resolve()
        if 'net-expectancy-eth-hourly-complete-runs' not in str(path) or any(
                name in str(path).lower() for name in ('confirmation','segment7','segment-7','segment_7')):
            raise ValueError('only explicit ETH hourly development derivatives allowed')
        bars,hashes=load_historical_candles(path,symbol='ETH/USDT')
        if hashes['qualification_eligible']!='true':raise ValueError('incomplete auxiliary source')
        research.features(bars[:289],3600)
        start=int(bars[0].open_time.timestamp()*1000);end=int(bars[-1].close_time.timestamp()*1000)
        if end>hourly.END:raise ValueError('auxiliary data exceeds development boundary')
        if sources and start<sources[-1]['end_ms']:raise ValueError('overlapping auxiliary sources')
        sources.append({'path':str(path),'hashes':hashes,'start_ms':start,'end_ms':end})
    if not sources:raise ValueError('genuine auxiliary sources required')
    plan.update(schema='cross-market-expectancy-plan/v1',stage=3,auxiliary_sources=sources,
        feature_names=list(research.FEATURES+AUX_FEATURES),auxiliary_features=list(AUX_FEATURES),
        auxiliary_policy='join by exact closed-hour timestamp; 289 consecutive hours in both markets; never fill; minimum 95% validation feature coverage',
        selection_scope='only pre-March-2025 hourly development; no prior future-period or confirmation results used',
        alpha_spending={'total_budget':.01,'stage_allocation':.00125,'candidate_count':12})
    plan['implementation_sha256']['trading/app/cross_market_expectancy.py']=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    return plan


def run(plan,folder):
    eth=[]
    for source in plan['auxiliary_sources']:
        bars,_=load_historical_candles(source['path'],symbol='ETH/USDT');eth.extend(bars)
    eth_index={int(b.close_time.timestamp()*1000):i for i,b in enumerate(eth)}
    def build(series,reference):
        indexes=[{int(b.close_time.timestamp()*1000):i for i,b in enumerate(bars)} for bars in series]
        cache={};output={}
        for (source_id,horizon),rows in reference.items():
            output[source_id,horizon]=[]
            for row in rows:
                when=row['decision_ms'];key=(source_id,when)
                if key not in cache:
                    auxiliary=None;i=eth_index.get(when);j=indexes[source_id][when]
                    if i is not None and i>=288:
                        try:auxiliary=auxiliary_features(series[source_id][j-288:j+1],eth[i-288:i+1])
                        except ValueError:pass # Explicit no-trade on unavailable causal context; coverage gate enforces it.
                    cache[key]=auxiliary
                if cache[key] is not None:
                    output[source_id,horizon].append({**row,'features':row['features']+cache[key]})
        return output
    return hourly.execute(plan,folder,sample_builder=build,validator=lambda value:
        prepare([s['path'] for s in value['sources']],[s['path'] for s in value['auxiliary_sources']]))


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('prepare','run'));parser.add_argument('plan')
    parser.add_argument('--source',action='append');parser.add_argument('--auxiliary-source',action='append')
    parser.add_argument('--output');args=parser.parse_args(argv)
    if args.action=='prepare':
        if not args.source or not args.auxiliary_source:parser.error('explicit BTC and ETH sources required')
        write_new(args.plan,prepare(args.source,args.auxiliary_source))
    else:
        if not args.output:parser.error('new output directory required')
        run(json.loads(Path(args.plan).read_text()),args.output)
    return 0


if __name__=='__main__':raise SystemExit(main())
