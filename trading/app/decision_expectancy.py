"""Stage 6: inner-only entry-buffer, sizing, learner and holding-period selection."""
import argparse
import hashlib
import json
from pathlib import Path

from . import funding_expectancy as funding
from .cost_research import write_new


def prepare(paths,funding_path,symbol='BTC/USDT'):
    plan=funding.prepare(paths,funding_path,symbol)
    plan.update(schema='decision-expectancy-plan/v1',stage=6,
        inner_candidates=[{'kind':kind,'horizon_bars':h,'entry_floor':floor,'full_risk':full}
            for h in (24,72,168) for kind in ('ridge','tree','logistic_payoff','boosted')
            for floor in (None,0.) for full in (False,True)],
        entry_policy='inner selects fixed positive-net-forecast buffer (0 or original training-std margin)',
        sizing_policy='inner selects original conviction scaling or full existing .5% risk budget; all production caps enforced unchanged',
        boosted_configuration={'rounds':12,'learning_rate':.1,'maximum_depth':2},
        alpha_spending={'stage':6,'allocation':.00015625,'total_budget':.01},bootstrap_replicates=100000)
    plan['implementation_sha256']['trading/app/decision_expectancy.py']=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    plan['limitations']+=['48 internal choices are selected on inner simulation only; the entire adaptive algorithm is the sole outer candidate',
        'new algorithms can still overfit reused development evidence; genuine sealed confirmation remains mandatory']
    return plan


def run(plan,folder):
    return funding.execute(plan,folder,validator=lambda value:
        prepare([s['path'] for s in value['sources']],value['funding']['path'],value.get('symbol','BTC/USDT')))


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('prepare','run'));parser.add_argument('plan')
    parser.add_argument('--source',action='append');parser.add_argument('--funding-source');parser.add_argument('--output')
    args=parser.parse_args(argv)
    if args.action=='prepare':
        if not args.source or not args.funding_source:parser.error('explicit development inputs required')
        write_new(args.plan,prepare(args.source,args.funding_source))
    else:
        if not args.output:parser.error('new output directory required')
        run(json.loads(Path(args.plan).read_text()),args.output)
    return 0


if __name__=='__main__':raise SystemExit(main())
