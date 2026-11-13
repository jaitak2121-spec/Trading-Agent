"""Audited local paper controls; audit records are the durable state authority."""
from __future__ import annotations

import threading
from contextlib import nullcontext

from ..core.audit import AuditCategory, AuditOutcome
from ..core.authz import Action, authorize
from ..core.errors import SafetyViolation


class PaperBotState:
    def __init__(self, stack):
        self.stack = stack
        self.lock = threading.RLock()
        self.state = 'running'
        prior = [r for r in stack.audit.records() if r.action == 'paper.bot_state']
        if prior:
            previous = prior[-1].details['state']
            if previous not in {'running', 'paused', 'stopped', 'emergency_stopped'}:
                raise SafetyViolation('invalid persisted paper bot state')
            self.state = previous
            if previous == 'running':
                self.transition('paused', stack.operator_id, reason='restart requires explicit resume')
        else:
            self.transition('running', stack.operator_id, reason='initial paper startup')

    def transition(self, state, principal, *, reason):
        authorize(principal, Action.RELEASE_KILL_SWITCH)
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError('bot transition requires a reason')
        allowed = {
            'running': {'running', 'paused', 'stopped', 'emergency_stopped'},
            'paused': {'running', 'stopped', 'emergency_stopped'},
            'stopped': {'running', 'stopped', 'emergency_stopped'},
            'emergency_stopped': {'paused'},
        }
        with self.lock, self.stack.gateway._lock:
            if state not in allowed[self.state]:
                raise SafetyViolation('invalid paper bot transition')
            if state == 'running' and self.stack.kill_switch.is_engaged:
                raise SafetyViolation('release kill switch before resuming')
            transaction = (nullcontext() if self.stack.persistence is None
                           else self.stack.persistence.transaction())
            with transaction:
                self.stack.audit.record(
                    AuditCategory.SYSTEM, 'paper.bot_state',
                    outcome=AuditOutcome.INFO, actor=principal.principal_id,
                    details={'previous': self.state, 'state': state, 'reason': reason},
                )
                if state == 'emergency_stopped':
                    self.stack.kill_switch.engage(principal, reason=reason)
            self.state = state

    def require_running(self):
        if self.state != 'running':
            raise SafetyViolation('paper bot is ' + self.state)
