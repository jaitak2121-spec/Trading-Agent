"""Dependency-independent safety core.

Every module in this package MUST import only from the Python standard library
and from other modules in ``trading.core``. This rule is mechanically enforced
by ``tests/test_core_purity.py``.

The rule exists so that adding FastAPI, PostgreSQL, React, or an exchange
adapter in a later stage cannot change the behaviour of the safety core.
"""
