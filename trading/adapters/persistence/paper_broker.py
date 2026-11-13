"""Durable simulated venue inventory, independent of the local accounting ledger."""
import json

from ..paper.broker import PaperBroker
from ...core.money import Price, Quantity
from ...core.orders import Order
from ...ports.broker import AckOutcome, BrokerAck, BrokerOrderStatus
from .sqlite import _currency
from .durable import PersistenceError


class SQLitePaperBroker(PaperBroker):
    def __init__(self, *, state, **kwargs):
        super().__init__(**kwargs)
        self._state = state
        with state.read() as connection:
            row = connection.execute("SELECT value FROM metadata WHERE key='paper_venue'").fetchone()
        if row is None:
            # Legacy portfolios deliberately retain the reconciliation block.
            self._save()
            return
        try:
            data = json.loads(row[0])
            self._seq = data['sequence']
            if type(self._seq) is not int or self._seq < 0:
                raise ValueError('invalid venue sequence')
            self._positions = {s: Quantity(q['amount'], q['asset']) for s, q in data['positions'].items()}
            for entry in data['orders']:
                order = Order.from_dict(entry['order'], clock=self._clock)
                ack = entry['ack']
                self._order_objects[order.idempotency_key] = order
                self._acks[order.idempotency_key] = BrokerAck(
                    outcome=AckOutcome(ack['outcome']), broker_order_id=ack['broker_order_id'],
                    filled_quantity=None if ack['quantity'] is None else Quantity(ack['quantity'], order.intent.quantity.asset),
                    fill_price=None if ack['price'] is None else Price(ack['price'], _currency(ack['currency'])),
                    terminal_status=None if ack['terminal'] is None else BrokerOrderStatus(ack['terminal']),
                )
            reconstructed = {}
            for key, ack in self._acks.items():
                if ack.filled_quantity is not None:
                    order = self._order_objects[key]
                    amount = ack.filled_quantity.amount * order.side.sign
                    reconstructed[order.symbol] = reconstructed.get(order.symbol, 0) + amount
            expected = {s: q.amount for s, q in self._positions.items() if not q.is_zero}
            if {s: q for s, q in reconstructed.items() if q != 0} != expected:
                raise ValueError('venue fills and positions disagree')
        except (ValueError, KeyError, TypeError) as exc:
            state.close()
            raise PersistenceError('invalid persisted paper venue') from exc

    def _save(self):
        entries = []
        for key, ack in self._acks.items():
            entries.append({'order': self._order_objects[key].to_dict(), 'ack': {
                'outcome': ack.outcome.value, 'broker_order_id': ack.broker_order_id,
                'quantity': None if ack.filled_quantity is None else str(ack.filled_quantity.amount),
                'price': None if ack.fill_price is None else str(ack.fill_price.amount),
                'currency': None if ack.fill_price is None else ack.fill_price.currency.code,
                'terminal': None if ack.terminal_status is None else ack.terminal_status.value,
            }})
        data = {'sequence': self._seq, 'orders': entries,
                'positions': {s: {'amount': str(q.amount), 'asset': q.asset} for s, q in self._positions.items()}}
        with self._state.transaction():
            self._state.connection.execute(
                "INSERT INTO metadata(key,value) VALUES('paper_venue',?) "
                'ON CONFLICT(key) DO UPDATE SET value=excluded.value', (json.dumps(data),))

    def place_order(self, order, *, token):
        with self._lock:
            result = super().place_order(order, token=token)
            self._save()
            return result

    def fetch_order_state(self, order):
        with self._lock:
            result = super().fetch_order_state(order)
            self._save()
            return result

    def cancel_order(self, order):
        with self._lock:
            result = super().cancel_order(order)
            self._save()
            return result
