"""Transactional SQLite persistence for one-process paper/sandbox operation.

All repositories share one :class:`SQLiteState` connection.  A caller may open
``state.transaction()`` around several repository writes; nested writes join the
same transaction, so orders, reservations, positions, portfolio accounting and
audit records become visible together or not at all.

This is deliberately an adapter.  The kernel remains stdlib-only and knows only
about the optional transaction context handed to the execution gateway.
"""

from __future__ import annotations

import json
import hashlib
import os
import tempfile
import sqlite3
try:
    import fcntl
except ImportError:  # read-only tooling remains usable on non-Unix platforms
    fcntl = None
import threading
import datetime as dt
from collections import deque
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from typing import Iterator

from ...core.audit import AuditRecord, AuditSink, InMemoryAuditSink
from ...core.audit import AuditLog
from ...core.authz import Principal
from ...core.clock import Clock
from ...core.config import RiskConfig
from ...core.dedupe import Reservation, ReservationState, ReservationStore
from ...core.money import BTC, INR, USD, USDT, Currency, Money, Price, Quantity
from ...core.orders import Order, OrderSide, OrderStore
from ...core.portfolio import FillEffect, Portfolio
from ...core.reconciliation import PositionLedger
from ...core.risk import PnlLedger, RiskEngine
from ...ports.repository import OrderRepositoryPort, PositionRepositoryPort
from ...ports.reservation_repository import ReservationRepositoryPort
from .durable import PersistenceError

__all__ = [
    "SQLiteAuditSink",
    "SQLiteOrderStore",
    "SQLitePortfolio",
    "SQLitePositionLedger",
    "SQLiteReservationRepository",
    "SQLitePnlLedger",
    "SQLiteRiskEngine",
    "SQLiteState",
]

_SCHEMA_VERSION = 2


def read_model_registry_entry(database, identity):
    connection = sqlite3.connect(Path(database).resolve(strict=True).as_uri() + '?mode=ro', uri=True)
    try:
        row = connection.execute('SELECT payload FROM model_registry WHERE identity=?', (identity,)).fetchone()
        return None if row is None else json.loads(row[0])
    finally:
        connection.close()


def check_paper_origin(database, expected):
    """Reject mixing data domains before opening a writer or changing records."""
    path=Path(database).resolve()
    if not path.exists():return
    connection=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)
    try:
        tables={row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'metadata' not in tables:raise ValueError('existing database has no paper provenance')
        prior=connection.execute("SELECT value FROM metadata WHERE key='data_origin'").fetchone()
        if prior:
            if json.loads(prior[0])!=expected:raise ValueError('paper database data origin mismatch; use a separate database')
            return
        orders=[json.loads(row[0]) for row in connection.execute('SELECT payload FROM orders')] if 'orders' in tables else []
        if expected['kind']=='real_market_paper':
            snapshots=connection.execute('SELECT COUNT(*) FROM equity_snapshots').fetchone()[0] if 'equity_snapshots' in tables else 0
            if orders or snapshots:
                raise ValueError('legacy paper state has unverified provenance; preserve it and use a fresh real-market database')
        elif any(order.get('symbol')!='DEMOUSD' for order in orders):
            raise ValueError('non-demo orders cannot be opened as synthetic demo state')
    finally:connection.close()


def read_paper_origin(database):
    connection=sqlite3.connect(Path(database).resolve(strict=True).as_uri()+'?mode=ro',uri=True)
    try:
        row=connection.execute("SELECT value FROM metadata WHERE key='data_origin'").fetchone()
        return json.loads(row[0]) if row else None
    finally:connection.close()


def bind_paper_origin(state, expected):
    with state.transaction():
        row=state.connection.execute("SELECT value FROM metadata WHERE key='data_origin'").fetchone()
        if row is not None and json.loads(row[0])!=expected:raise ValueError('paper data origin changed')
        state.connection.execute("INSERT OR IGNORE INTO metadata VALUES('data_origin',?)",(json.dumps(expected,sort_keys=True),))

def read_experience_rows(database,symbol,maximum_candles):
    path=Path(database).resolve(strict=True)
    connection=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)
    try:
        stored=connection.execute('SELECT open_ms,payload,payload_sha256 FROM market_candles WHERE symbol=? ORDER BY open_ms DESC LIMIT ?',
                                  (symbol,maximum_candles)).fetchall()
    finally:connection.close()
    rows=[]
    for key,payload,expected in reversed(stored):
        if hashlib.sha256(payload.encode()).hexdigest()!=expected:raise ValueError('experience row hash mismatch')
        row=json.loads(payload)
        if row['open_time']!=key:raise ValueError('experience key mismatch')
        rows.append(row)
    return rows


class SQLiteState:
    """One SQLite connection and its re-entrant transaction boundary."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._depth = 0
        self._rollback_only = False
        self._ownership = None
        try:
            if fcntl is None:
                raise PersistenceError('exclusive application writer ownership requires Unix file locking')
            self._ownership = os.fdopen(os.open(str(self.path)+'.lock', os.O_CREAT | os.O_RDWR, 0o600), 'a+')
            try:
                fcntl.flock(self._ownership.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                self._ownership.close()
                self._ownership = None
                raise PersistenceError('SQLite paper state already has a writer; stop the other application first') from exc
            self._connection = sqlite3.connect(
                self.path, isolation_level=None, check_same_thread=False
            )
            self._connection.execute("PRAGMA foreign_keys = ON")
            self._connection.execute("PRAGMA journal_mode = WAL")
            self._connection.execute("PRAGMA synchronous = FULL")
            self._create_schema()
        except (sqlite3.Error, PersistenceError) as exc:
            if hasattr(self, '_connection'):
                self._connection.close()
            if self._ownership is not None:
                self._ownership.close()
                self._ownership = None
            if isinstance(exc, PersistenceError):
                raise
            raise PersistenceError(f"could not open SQLite state at {self.path}: {exc}") from exc

    def _create_schema(self) -> None:
        with self.transaction():
            existing = self._connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='metadata'").fetchone()
            if existing:
                version = self._connection.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()
                if version is None or version[0] not in {'1', '2'}:
                    raise PersistenceError('unsupported SQLite schema; migration refused')
            schema = (
                """
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS equity_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS paper_fills (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS orders (
                    order_id TEXT PRIMARY KEY,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS reservations (
                    key TEXT PRIMARY KEY,
                    order_id TEXT NOT NULL,
                    state TEXT NOT NULL,
                    reserved_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    note TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS positions (
                    symbol TEXT PRIMARY KEY,
                    amount TEXT NOT NULL,
                    asset TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS portfolio (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    currency TEXT NOT NULL,
                    cash TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS portfolio_symbols (
                    symbol TEXT PRIMARY KEY,
                    basis_amount TEXT,
                    basis_currency TEXT,
                    realized_amount TEXT NOT NULL,
                    accounted_amount TEXT,
                    accounted_asset TEXT
                );
                CREATE TABLE IF NOT EXISTS audit_records (
                    seq INTEGER PRIMARY KEY,
                    payload TEXT NOT NULL,
                    record_hash TEXT NOT NULL UNIQUE
                );
                CREATE TABLE IF NOT EXISTS daily_pnl (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    day TEXT NOT NULL,
                    currency TEXT NOT NULL,
                    realized TEXT NOT NULL,
                    unattributed INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS risk_submissions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    observed_at TEXT NOT NULL
                );
                """
            )
            for statement in schema.split(';'):
                if statement.strip():
                    self._connection.execute(statement)
            row = self._connection.execute(
                "SELECT value FROM metadata WHERE key = 'schema_version'"
            ).fetchone()
            if row is None:
                self._connection.execute(
                    "INSERT INTO metadata(key, value) VALUES('schema_version', ?)",
                    (str(_SCHEMA_VERSION),),
                )
            elif row[0] == '1':
                self._connection.execute("UPDATE metadata SET value='2' WHERE key='schema_version'")
                self._connection.execute("INSERT OR IGNORE INTO metadata VALUES('migration_1_to_2','additive runtime accounting tables; original rows preserved')")
            elif row[0] != str(_SCHEMA_VERSION):
                raise PersistenceError(
                    f"SQLite state at {self.path} has schema {row[0]}, "
                    f"expected {_SCHEMA_VERSION}"
                )

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Join or create a transaction; the outermost context commits."""
        with self._lock:
            outer = self._depth == 0
            if outer:
                self._connection.execute("BEGIN IMMEDIATE")
                self._rollback_only = False
            self._depth += 1
            try:
                yield
            except BaseException:
                self._rollback_only = True
                raise
            finally:
                self._depth -= 1
                if outer:
                    try:
                        if self._rollback_only:
                            self._connection.rollback()
                        else:
                            self._connection.commit()
                    except sqlite3.Error as exc:
                        self._connection.rollback()
                        raise PersistenceError(
                            f"could not commit SQLite state at {self.path}: {exc}"
                        ) from exc

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            yield self._connection

    @property
    def connection(self) -> sqlite3.Connection:
        return self._connection

    def snapshot_revision(self) -> int:
        """Return a change counter only after any active transaction finishes."""
        with self._lock:
            return self._connection.total_changes

    def close(self) -> None:
        with self._lock:
            self._connection.close()
            if self._ownership is not None:
                self._ownership.close()
                self._ownership = None


class SQLiteOrderStore(OrderStore):
    def __init__(self, state: SQLiteState, *, clock: Clock) -> None:
        super().__init__()
        self._state = state
        self._clock = clock
        try:
            with state.read() as connection:
                rows = connection.execute("SELECT payload FROM orders ORDER BY rowid").fetchall()
            for (raw,) in rows:
                order = Order.from_dict(json.loads(raw), clock=clock)
                OrderStore.add(self, order)
        except (sqlite3.Error, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
            raise PersistenceError(f"could not restore orders from {state.path}: {exc}") from exc

    def add(self, order: Order) -> Order:
        payload = json.dumps(order.to_dict(), sort_keys=True, separators=(",", ":"))
        with self._state.transaction():
            self._state.connection.execute(
                "INSERT INTO orders(order_id, idempotency_key, payload) VALUES(?, ?, ?)",
                (order.order_id, order.idempotency_key, payload),
            )
            return super().add(order)

    def update(self, order: Order) -> Order:
        if order.order_id not in self._by_id:
            raise KeyError(f"no order with id {order.order_id!r}")
        payload = json.dumps(order.to_dict(), sort_keys=True, separators=(",", ":"))
        with self._state.transaction():
            cursor = self._state.connection.execute(
                "UPDATE orders SET idempotency_key = ?, payload = ? WHERE order_id = ?",
                (order.idempotency_key, payload, order.order_id),
            )
            if cursor.rowcount != 1:
                raise PersistenceError(f"order {order.order_id} disappeared from SQLite")
            return super().update(order)


class SQLiteReservationRepository(ReservationStore):
    def __init__(self, state: SQLiteState) -> None:
        super().__init__()
        self._state = state
        try:
            with state.read() as connection:
                rows = connection.execute(
                    "SELECT key, order_id, state, reserved_at, updated_at, note "
                    "FROM reservations ORDER BY rowid"
                ).fetchall()
            for row in rows:
                ReservationStore.add(
                    self,
                    Reservation(
                        key=row[0], order_id=row[1], state=ReservationState(row[2]),
                        reserved_at=row[3], updated_at=row[4], note=row[5],
                    ),
                )
        except (sqlite3.Error, ValueError, TypeError) as exc:
            raise PersistenceError(
                f"could not restore reservations from {state.path}: {exc}"
            ) from exc

    def add(self, reservation: Reservation) -> Reservation:
        with self._state.transaction():
            self._state.connection.execute(
                "INSERT INTO reservations VALUES(?, ?, ?, ?, ?, ?)",
                (
                    reservation.key, reservation.order_id, reservation.state.value,
                    reservation.reserved_at, reservation.updated_at, reservation.note,
                ),
            )
            return super().add(reservation)

    def update(self, reservation: Reservation) -> Reservation:
        with self._state.transaction():
            cursor = self._state.connection.execute(
                "UPDATE reservations SET order_id=?, state=?, reserved_at=?, "
                "updated_at=?, note=? WHERE key=?",
                (
                    reservation.order_id, reservation.state.value,
                    reservation.reserved_at, reservation.updated_at,
                    reservation.note, reservation.key,
                ),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"no reservation for key {reservation.key[:16]}...")
            return super().update(reservation)

    def delete(self, key: str) -> None:
        with self._state.transaction():
            self._state.connection.execute("DELETE FROM reservations WHERE key=?", (key,))
            super().delete(key)


class SQLitePositionLedger(PositionLedger):
    def __init__(self, state: SQLiteState) -> None:
        super().__init__()
        self._state = state
        try:
            with state.read() as connection:
                rows = connection.execute(
                    "SELECT symbol, amount, asset FROM positions ORDER BY symbol"
                ).fetchall()
            for symbol, amount, asset in rows:
                PositionLedger.set_position(self, symbol, Quantity(amount, asset))
        except (sqlite3.Error, ValueError, TypeError) as exc:
            raise PersistenceError(f"could not restore positions from {state.path}: {exc}") from exc

    def _persist(self, symbol: str, quantity: Quantity) -> None:
        self._state.connection.execute(
            "INSERT INTO positions(symbol, amount, asset) VALUES(?, ?, ?) "
            "ON CONFLICT(symbol) DO UPDATE SET amount=excluded.amount, asset=excluded.asset",
            (symbol, str(quantity.amount), quantity.asset),
        )

    def apply_fill(self, symbol: str, side: OrderSide, quantity: Quantity) -> Quantity:
        with self._state.transaction():
            result = super().apply_fill(symbol, side, quantity)
            self._persist(symbol, result)
            return result

    def set_position(self, symbol: str, quantity: Quantity) -> None:
        with self._state.transaction():
            super().set_position(symbol, quantity)
            self._persist(symbol, quantity)


def _currency(code: str) -> Currency:
    currencies = {item.code: item for item in (USD, INR, USDT, BTC)}
    try:
        return currencies[code]
    except KeyError as exc:
        raise PersistenceError(f"unknown persisted currency {code!r}") from exc


class SQLitePortfolio(Portfolio):
    """Portfolio whose cash, basis and realized P&L share the state transaction."""

    def __init__(self, starting_cash: Money, *, ledger: SQLitePositionLedger, state: SQLiteState) -> None:
        self._state = state
        with state.read() as connection:
            row = connection.execute(
                "SELECT currency, cash FROM portfolio WHERE singleton=1"
            ).fetchone()
        if row is None:
            super().__init__(starting_cash, ledger=ledger)
            with state.transaction():
                self._persist()
            return
        stored_cash = Money(row[1], _currency(row[0]))
        if stored_cash.currency != starting_cash.currency:
            raise PersistenceError(
                f"portfolio currency {stored_cash.currency.code} does not match "
                f"configured {starting_cash.currency.code}"
            )
        super().__init__(stored_cash, ledger=ledger)
        with state.read() as connection:
            accounting = connection.execute(
                "SELECT value FROM metadata WHERE key='paper_accounting'"
            ).fetchone()
        if accounting:
            data = json.loads(accounting[0])
            self.starting_cash = Money(data['starting_cash'], self._currency)
            self.fees = Money(data['fees'], self._currency)
            if data.get('cash', str(self.cash.amount)) != str(self.cash.amount):
                state.close()
                raise PersistenceError('paper accounting cash checkpoint mismatch')
        with state.read() as connection:
            rows = connection.execute(
                "SELECT symbol, basis_amount, basis_currency, realized_amount, "
                "accounted_amount, accounted_asset FROM portfolio_symbols"
            ).fetchall()
        for symbol, basis, basis_currency, realized, accounted, asset in rows:
            self._realized[symbol] = Money(realized, self._currency)
            if basis is not None:
                self._basis[symbol] = Price(basis, _currency(basis_currency))
            if accounted is not None:
                self._accounted[symbol] = Quantity(accounted, asset)

    def _persist(self) -> None:
        self._state.connection.execute(
            "INSERT INTO metadata(key,value) VALUES('paper_accounting',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (json.dumps({'starting_cash': str(self.starting_cash.amount),
                         'fees': str(self.fees.amount), 'cash': str(self._cash.amount)}),),
        )
        self._state.connection.execute(
            "INSERT INTO portfolio(singleton, currency, cash) VALUES(1, ?, ?) "
            "ON CONFLICT(singleton) DO UPDATE SET currency=excluded.currency, cash=excluded.cash",
            (self._currency.code, str(self._cash.amount)),
        )
        self._state.connection.execute("DELETE FROM portfolio_symbols")
        symbols = set(self._basis) | set(self._realized) | set(self._accounted)
        for symbol in sorted(symbols):
            basis = self._basis.get(symbol)
            realized = self._realized.get(symbol, Money.zero(self._currency))
            accounted = self._accounted.get(symbol)
            self._state.connection.execute(
                "INSERT INTO portfolio_symbols VALUES(?, ?, ?, ?, ?, ?)",
                (
                    symbol,
                    None if basis is None else str(basis.amount),
                    None if basis is None else basis.currency.code,
                    str(realized.amount),
                    None if accounted is None else str(accounted.amount),
                    None if accounted is None else accounted.asset,
                ),
            )

    def apply_fill(
        self, symbol: str, side: OrderSide, quantity: Quantity, price: Price
    ) -> FillEffect:
        with self._state.transaction():
            effect = super().apply_fill(symbol, side, quantity, price)
            self._persist()
            self._state.connection.execute(
                'INSERT INTO paper_fills(payload) VALUES(?)',
                (json.dumps(effect.as_details()),),
            )
            return effect


class SQLiteAuditSink(AuditSink):
    def __init__(self, state: SQLiteState) -> None:
        self._state = state

    def emit(self, record: AuditRecord) -> None:
        with self._state.transaction():
            self._state.connection.execute(
                "INSERT INTO audit_records(seq, payload, record_hash) VALUES(?, ?, ?)",
                (record.seq, record.to_json(), record.record_hash),
            )

    @property
    def records(self) -> list[AuditRecord]:
        with self._state.read() as connection:
            rows = connection.execute(
                "SELECT payload FROM audit_records ORDER BY seq"
            ).fetchall()
        records: list[AuditRecord] = []
        for (raw,) in rows:
            data = json.loads(raw)
            records.append(AuditRecord(**data))
        return records


class SQLitePnlLedger(PnlLedger):
    """Current-day realized P&L that survives process restart."""

    def __init__(self, state: SQLiteState, base_currency: Currency, *, clock: Clock) -> None:
        self._state = state
        super().__init__(base_currency, clock=clock)
        with state.read() as connection:
            row = connection.execute(
                "SELECT day, currency, realized, unattributed FROM daily_pnl "
                "WHERE singleton=1"
            ).fetchone()
        if row is None:
            with state.transaction():
                self._persist()
            return
        if _currency(row[1]) != base_currency:
            raise PersistenceError(
                f"daily P&L currency {row[1]} does not match {base_currency.code}"
            )
        try:
            self._day = dt.date.fromisoformat(row[0])
            self._realized = Money(row[2], base_currency)
            self._unattributed = int(row[3])
        except (ValueError, TypeError) as exc:
            raise PersistenceError(f"could not restore daily P&L: {exc}") from exc
        with self._lock:
            self._maybe_roll_over()

    def _persist(self) -> None:
        self._state.connection.execute(
            "INSERT INTO daily_pnl(singleton, day, currency, realized, unattributed) "
            "VALUES(1, ?, ?, ?, ?) ON CONFLICT(singleton) DO UPDATE SET "
            "day=excluded.day, currency=excluded.currency, "
            "realized=excluded.realized, unattributed=excluded.unattributed",
            (
                self._day.isoformat(), self._currency.code,
                str(self._realized.amount), self._unattributed,
            ),
        )

    def _maybe_roll_over(self) -> None:
        previous = self._day
        super()._maybe_roll_over()
        if self._day != previous:
            with self._state.transaction():
                self._persist()

    def record(self, pnl: Money, *, attributed: bool = True) -> Money:
        with self._state.transaction():
            result = super().record(pnl, attributed=attributed)
            self._persist()
            return result


class SQLiteRiskEngine(RiskEngine):
    """Risk engine with durable daily P&L and conservative rate history."""

    def __init__(
        self,
        state: SQLiteState,
        config: RiskConfig,
        *,
        identity: Principal,
        order_store: OrderStore,
        audit: AuditLog,
        clock: Clock,
        approval_ttl_seconds: int = 30,
    ) -> None:
        self._sqlite_state = state
        pnl = SQLitePnlLedger(state, config.base_currency, clock=clock)
        super().__init__(
            config,
            identity=identity,
            order_store=order_store,
            audit=audit,
            clock=clock,
            approval_ttl_seconds=approval_ttl_seconds,
            pnl_ledger=pnl,
        )
        self._restore_submissions()

    def _restore_submissions(self) -> None:
        now_wall = self._clock.now()
        now_mono = self._clock.monotonic_seconds()
        cutoff = now_wall - dt.timedelta(seconds=60)
        with self._sqlite_state.transaction():
            self._sqlite_state.connection.execute(
                "DELETE FROM risk_submissions WHERE observed_at < ?",
                (cutoff.isoformat(),),
            )
            rows = self._sqlite_state.connection.execute(
                "SELECT observed_at FROM risk_submissions ORDER BY observed_at"
            ).fetchall()
        restored: deque[float] = deque()
        for (raw,) in rows:
            try:
                observed = dt.datetime.fromisoformat(raw)
            except ValueError as exc:
                raise PersistenceError(f"invalid risk submission timestamp {raw!r}") from exc
            age = max(0.0, (now_wall - observed).total_seconds())
            restored.append(now_mono - min(age, 60.0))
        self._submissions = restored

    def record_submission(self) -> None:
        with self._sqlite_state.transaction():
            super().record_submission()
            observed = self._clock.now().isoformat()
            # Multiple submissions can share a deterministic test timestamp.
            self._sqlite_state.connection.execute(
                "INSERT INTO risk_submissions(observed_at) VALUES(?)", (observed,),
            )


OrderRepositoryPort.register(SQLiteOrderStore)
ReservationRepositoryPort.register(SQLiteReservationRepository)
PositionRepositoryPort.register(SQLitePositionLedger)
def inspect_database(path: str | Path) -> dict:
    source = Path(path).resolve(strict=True)
    connection = sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)
    try:
        if connection.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
            raise ValueError('SQLite integrity check failed')
        version = connection.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()
        if version is None or version[0] not in {'1', '2'}:
            raise ValueError('unsupported application database schema')
        records = [AuditRecord(**json.loads(row[0])) for row in connection.execute(
            'SELECT payload FROM audit_records ORDER BY seq')]
        AuditLog(InMemoryAuditSink(), initial_records=records)
        return {'schema_version': version[0], 'audit_records': len(records),
                'orders': connection.execute('SELECT count(*) FROM orders').fetchone()[0],
                'integrity': 'passed'}
    finally:
        connection.close()


def load_audit_records(path):
    connection = sqlite3.connect(Path(path).resolve(strict=True).as_uri() + '?mode=ro', uri=True)
    try:
        records = [AuditRecord(**json.loads(row[0])) for row in connection.execute(
            'SELECT payload FROM audit_records ORDER BY seq')]
        AuditLog(InMemoryAuditSink(), initial_records=records)
        return records
    finally:
        connection.close()


def copy_database(source: str | Path, destination: str | Path) -> dict:
    """SQLite backup API includes committed WAL pages; existing targets refuse."""
    source = Path(source).resolve(strict=True)
    destination = Path(destination).resolve()
    if destination.exists():
        raise FileExistsError(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix='.paper-backup-', dir=destination.parent)
    os.close(descriptor)
    try:
        reader = sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)
        writer = sqlite3.connect(temporary)
        try:
            reader.backup(writer)
        finally:
            writer.close()
            reader.close()
        details = inspect_database(temporary)
        with open(temporary, 'rb') as handle:
            os.fsync(handle.fileno())
        # Atomic publication with no overwrite, including concurrent creators.
        os.link(temporary, destination)
        return {**details, 'sha256': hashlib.sha256(destination.read_bytes()).hexdigest(),
                'bytes': destination.stat().st_size, 'path': str(destination)}
    finally:
        Path(temporary).unlink(missing_ok=True)
