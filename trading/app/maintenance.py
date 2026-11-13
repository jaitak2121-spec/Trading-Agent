"""CLI for the SQLite backup and inspection adapter."""
from ..adapters.persistence.maintenance import copy_database, inspect_database, main

if __name__ == '__main__':
    raise SystemExit(main())
