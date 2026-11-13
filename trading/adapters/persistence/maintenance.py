"""Read-only SQLite inspection and verified, non-overwriting backup/restore."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from .sqlite import inspect_database, copy_database, load_audit_records




def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=('inspect', 'backup', 'restore'))
    parser.add_argument('source')
    parser.add_argument('destination', nargs='?')
    parser.add_argument('--sha256', help='required verified backup hash for restore')
    args = parser.parse_args(argv)
    if args.operation == 'inspect':
        result = inspect_database(args.source)
    else:
        if args.destination is None:
            parser.error('destination is required')
        if args.operation == 'restore':
            actual = hashlib.sha256(Path(args.source).read_bytes()).hexdigest()
            if args.sha256 is None or actual != args.sha256:
                parser.error('restore requires the exact backup SHA-256')
        result = copy_database(args.source, args.destination)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
