"""Dependency-independent safety core.

Every module in this package MUST import only from the Python standard library,
from other modules in ``trading.core``, and from :mod:`trading.ports` -- the
port interfaces are part of the kernel, not infrastructure, and the kernel calls
outward through them. Nothing here may import :mod:`trading.adapters` or
:mod:`trading.strategy`; the dependency arrow points one way. These rules are
mechanically enforced by ``tests/test_core_purity.py``.

The rule exists so that adding FastAPI, PostgreSQL, React, or an exchange
adapter in a later stage cannot change the behaviour of the safety core.
"""
