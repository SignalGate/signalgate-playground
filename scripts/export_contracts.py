#!/usr/bin/env python3
"""Regenerate the contract files under ``contracts/`` from the specs in ``tests/contract/``.

    uv run python scripts/export_contracts.py           # rewrite every file
    uv run python scripts/export_contracts.py --check   # exit 1 if any is out of date

Dev-only. The specs are the source; never edit a contract file by hand.
"""

from __future__ import annotations

import argparse
import importlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="report stale files; write nothing")
    args = parser.parse_args(argv)

    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    contracts = importlib.import_module("tests.contract.contracts")

    stale = []
    for name, document in contracts.documents().items():
        path = contracts.CONTRACTS_DIR / name
        text = contracts.render(document)
        current = path.read_text(encoding="utf-8") if path.exists() else None
        if current == text:
            continue
        stale.append(name)
        if not args.check:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
    for name in stale:
        print(f"{'out of date' if args.check else 'wrote'}: contracts/{name}")
    return 1 if args.check and stale else 0


if __name__ == "__main__":
    sys.exit(main())
