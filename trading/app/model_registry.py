"""Explicit paper model lifecycle; registry changes never deploy a model."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from ..core.audit import AuditCategory, AuditOutcome
from ..core.authz import Action, authorize
from .model_deployment import model_identity, validate_paper_deployment


class ModelRegistry:
    def __init__(self, state, audit):
        self.state, self.audit = state, audit
        with state.transaction():
            state.connection.execute('CREATE TABLE IF NOT EXISTS model_registry '
                                     '(identity TEXT PRIMARY KEY, payload TEXT NOT NULL)')

    def entries(self):
        with self.state.read() as connection:
            return [json.loads(row[0]) for row in connection.execute(
                'SELECT payload FROM model_registry ORDER BY identity')]

    def register(self, model_path, evaluation_path, principal, *, reason):
        authorize(principal, Action.RELEASE_KILL_SWITCH)
        model_path, evaluation_path = Path(model_path).resolve(), Path(evaluation_path).resolve()
        model_raw, evaluation_raw = model_path.read_bytes(), evaluation_path.read_bytes()
        model, evaluation = json.loads(model_raw), json.loads(evaluation_raw)
        identity = model_identity(model)
        entry = {'identity': identity, 'state': 'candidate', 'model_path': str(model_path),
                 'evaluation_path': str(evaluation_path),
                 'model_file_sha256': hashlib.sha256(model_raw).hexdigest(),
                 'evaluation_file_sha256': hashlib.sha256(evaluation_raw).hexdigest(),
                 'paper_only': True, 'ever_approved': False}
        with self.state.transaction():
            if any(e['identity'] == identity for e in self.entries()):
                raise ValueError('model is already registered; history cannot be replaced')
            self._save(entry, principal, reason)
        return identity

    def transition(self, identity, target, principal, *, reason, confirmation=''):
        authorize(principal, Action.RELEASE_KILL_SWITCH)
        with self.state.transaction():
            entries = {e['identity']: e for e in self.entries()}
            entry = entries[identity]
            allowed = {'candidate': {'approved_for_paper', 'rejected', 'archived'},
                       'approved_for_paper': {'rejected', 'archived'},
                       'rejected': {'archived'}, 'archived': {'approved_for_paper'}}
            if target not in allowed[entry['state']]:
                raise ValueError('invalid model lifecycle transition')
            if target == 'approved_for_paper':
                if entry.get('rejected') or (entry['state'] == 'archived' and not entry['ever_approved']):
                    raise ValueError('rejected or never-approved archived model cannot be promoted')
                model_raw = Path(entry['model_path']).read_bytes()
                evaluation_raw = Path(entry['evaluation_path']).read_bytes()
                if (hashlib.sha256(model_raw).hexdigest() != entry['model_file_sha256']
                        or hashlib.sha256(evaluation_raw).hexdigest() != entry['evaluation_file_sha256']):
                    raise ValueError('registered artifact changed')
                validate_paper_deployment(json.loads(model_raw), json.loads(evaluation_raw), confirmation)
                entry['ever_approved'] = True
            entry['state'] = target
            if target == 'rejected':
                entry['rejected'] = True
            self._save(entry, principal, reason)

    def _save(self, entry, principal, reason):
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError('model lifecycle change requires a reason')
        self.audit.record(AuditCategory.SYSTEM, 'model.lifecycle', outcome=AuditOutcome.INFO,
                          actor=principal.principal_id,
                          details={'identity': entry['identity'], 'state': entry['state'], 'reason': reason})
        self.state.connection.execute('INSERT INTO model_registry VALUES(?,?) '
                                      'ON CONFLICT(identity) DO UPDATE SET payload=excluded.payload',
                                      (entry['identity'], json.dumps(entry, sort_keys=True)))


def require_registry_approval(database, model, model_path, evaluation_path):
    """Read approval afresh on every inference; revocation fails the cycle closed."""
    from ..adapters.persistence.sqlite import read_model_registry_entry
    identity = model_identity(model)
    entry = read_model_registry_entry(database, identity)
    if entry is None:
        raise ValueError('NO APPROVED ML MODEL: model is not registered')
    if entry['state'] != 'approved_for_paper' or entry.get('rejected'):
        raise ValueError('NO APPROVED ML MODEL: registry approval is absent or revoked')
    for path, key in ((model_path, 'model_file_sha256'), (evaluation_path, 'evaluation_file_sha256')):
        if hashlib.sha256(Path(path).read_bytes()).hexdigest() != entry[key]:
            raise ValueError('registered artifact integrity failed')


def main(argv=None):
    import argparse
    from ..adapters.persistence import SQLiteState, SQLiteAuditSink
    from ..core.audit import AuditLog
    from ..core.authz import Principal, Role
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-db', required=True)
    commands = parser.add_subparsers(dest='operation', required=True)
    commands.add_parser('list')
    register = commands.add_parser('register')
    register.add_argument('model')
    register.add_argument('evaluation')
    register.add_argument('--reason', required=True)
    transition = commands.add_parser('transition')
    transition.add_argument('identity')
    transition.add_argument('target', choices=('approved_for_paper', 'rejected', 'archived'))
    transition.add_argument('--reason', required=True)
    transition.add_argument('--confirmation', default='')
    args = parser.parse_args(argv)
    state = SQLiteState(args.state_db)
    try:
        sink = SQLiteAuditSink(state)
        audit = AuditLog(sink, initial_records=sink.records)
        registry = ModelRegistry(state, audit)
        operator = Principal('local-operator', Role.OPERATOR)
        if args.operation == 'register':
            registry.register(args.model, args.evaluation, operator, reason=args.reason)
        elif args.operation == 'transition':
            registry.transition(args.identity, args.target, operator,
                                reason=args.reason, confirmation=args.confirmation)
        print(json.dumps(registry.entries(), indent=2, sort_keys=True))
    finally:
        state.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
