"""Create a separate paused paper account without changing an existing account."""
import argparse
from dataclasses import replace
import json
import os
from pathlib import Path
import tempfile

from .composition import build_paper_stack
from ..adapters.persistence.sqlite import inspect_database
from ..core.audit import AuditCategory, AuditOutcome
from ..core.clock import SystemClock
from ..core.config import KNOWN_CURRENCIES, RiskConfig
from ..core.errors import TradingError
from ..core.money import Money


def create_paper_account(destination, *, cash, currency='USD'):
    """Initialize privately, verify, then atomically publish with no overwrite."""
    if currency not in ('USD','INR','USDT'):
        raise ValueError('paper quote currency must be USD, INR or USDT')
    capital=Money(cash,KNOWN_CURRENCIES[currency])
    if capital.amount<=0:
        raise ValueError('starting virtual cash must be positive')
    target=Path(destination).absolute()
    # lexists also rejects dangling symlinks; never follow them during creation.
    if os.path.lexists(target):
        raise FileExistsError('account path already exists; choose a new database')
    if any(os.path.lexists(str(target)+suffix) for suffix in ('-wal','-shm','.lock')):
        raise FileExistsError('account sidecar exists; choose a new database')
    target.parent.mkdir(parents=True,exist_ok=True)
    descriptor,temporary=tempfile.mkstemp(prefix='.new-paper-',suffix='.sqlite3',dir=target.parent)
    os.close(descriptor)
    try:
        defaults=RiskConfig()
        risk=replace(defaults,base_currency=capital.currency,**{
            field:Money(getattr(defaults,field).amount,capital.currency)
            for field in ('max_order_notional','max_position_notional','max_gross_exposure','max_daily_loss')})
        stack=build_paper_stack(starting_cash=capital,risk=risk,clock=SystemClock(),
                                sqlite_path=temporary,run_startup=False)
        try:
            stack.bot.transition('paused',stack.operator_id,reason='new account requires explicit resume')
            stack.audit.record(AuditCategory.SYSTEM,'paper.account_created',outcome=AuditOutcome.INFO,
                actor=stack.operator_id.principal_id,
                details={'starting_cash':str(capital.amount),'currency':currency,'virtual_funds':True})
            if stack.orders.all_orders() or stack.portfolio.open_positions() or stack.portfolio.cash!=capital:
                raise ValueError('new account initialization inconsistent')
        finally:
            stack.persistence.close()
        checked=inspect_database(temporary)
        if checked['orders']!=0:
            raise ValueError('new account unexpectedly contains orders')
        # A same-filesystem hard link publishes the closed SQLite file atomically.
        # Unlike replace/rename, it refuses a target created concurrently.
        os.link(temporary,target)
        return {'database':str(target),'starting_cash':str(capital.amount),'currency':currency,
                'state':'paused','virtual_funds':True,'live_trading':False,'integrity':checked['integrity']}
    finally:
        for suffix in ('','-wal','-shm','.lock'):
            Path(temporary+suffix).unlink(missing_ok=True)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation',choices=('create',))
    parser.add_argument('--state-db',required=True,help='new SQLite path; existing paths are refused')
    parser.add_argument('--cash',required=True,help='positive starting virtual balance')
    parser.add_argument('--currency',choices=('USD','INR','USDT'),default='USD')
    args=parser.parse_args(argv)
    try:
        result=create_paper_account(args.state_db,cash=args.cash,currency=args.currency)
    except (ValueError,TypeError,OSError,TradingError) as exc:
        parser.error(str(exc))
    print(json.dumps(result,indent=2,sort_keys=True))
    return 0


if __name__=='__main__':
    raise SystemExit(main())
