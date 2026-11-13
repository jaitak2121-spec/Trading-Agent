"""Stage 8: ETH spot target with lagged BTC flow/funding context, never BTC prices."""
import argparse
import hashlib
import json
from pathlib import Path

from . import flow_expectancy as flow
from .cost_research import write_new


def prepare(paths,funding_path,flow_paths):
    plan=flow.prepare(paths,funding_path,flow_paths,symbol='ETH/USDT')
    plan.update(schema='eth-expectancy-plan/v1',stage=8,
        alpha_spending={'stage':8,'allocation':.0000390625,'total_budget':.01},bootstrap_replicates=200000,
        fee_provenance='10bp base / 20bp stressed scenarios; account-specific ETH fees have NOT been requested or verified')
    plan['reserved_new_confirmation']['symbol']='ETH/USDT'
    plan['implementation_sha256']['trading/app/eth_expectancy.py']=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    plan['limitations']+=['Trading market is CoinSwitch ETH/USDT; BTC funding/flow are lagged information only',
        'no BTC funding income or Binance execution prices are credited to ETH positions']
    return plan


def run(plan,folder):
    return flow.execute(plan,folder,validator=lambda value:prepare(
        [s['path'] for s in value['sources']],value['funding']['path'],
        [s['path'] for s in value['flow']['sources']]))


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('prepare','run'));parser.add_argument('plan')
    parser.add_argument('--source',action='append');parser.add_argument('--funding-source');parser.add_argument('--flow-source',action='append')
    parser.add_argument('--output');args=parser.parse_args(argv)
    if args.action=='prepare':
        if not args.source or not args.funding_source or not args.flow_source:parser.error('explicit ETH development sources required')
        write_new(args.plan,prepare(args.source,args.funding_source,args.flow_source))
    else:
        if not args.output:parser.error('new output directory required')
        run(json.loads(Path(args.plan).read_text()),args.output)
    return 0


if __name__=='__main__':raise SystemExit(main())
