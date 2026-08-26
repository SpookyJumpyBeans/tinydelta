from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from tinydelta.errors import TinyDeltaError
from tinydelta.schema import Schema
from tinydelta.table import DeltaTable


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="tinydelta",
        description="Create, append, overwrite, and time-travel a local table.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    create = sub.add_parser("create", help="create an empty table")
    create.add_argument("table")
    create.add_argument("columns", nargs="+", help="name:type, e.g. id:int region:str")

    append = sub.add_parser("append", help="add rows from a JSONL file")
    append.add_argument("table")
    append.add_argument("--file", required=True, help="JSONL of row objects")

    overwrite = sub.add_parser("overwrite", help="replace current files, keep history")
    overwrite.add_argument("table")
    overwrite.add_argument("--file", required=True, help="JSONL of row objects")

    read = sub.add_parser("read", help="print rows as JSONL")
    read.add_argument("table")
    read.add_argument("--version", type=int, default=None)

    history = sub.add_parser("history", help="print commits")
    history.add_argument("table")

    restore = sub.add_parser("restore", help="make latest match an older version")
    restore.add_argument("table")
    restore.add_argument("--version", type=int, required=True)

    vacuum = sub.add_parser("vacuum", help="delete unpublished data files")
    vacuum.add_argument("table")
    vacuum.add_argument(
        "--older-than",
        type=int,
        default=0,
        metavar="MS",
        help="only delete orphans older than this many milliseconds",
    )

    args = parser.parse_args(argv)
    try:
        if args.command == "create":
            table = DeltaTable.create(args.table, Schema.parse(args.columns))
            print(f"created {table.path} version 0")
            return 0
        table = DeltaTable.open(args.table)
        if args.command == "append":
            version = table.append(_load_rows(args.file))
            print(f"appended as version {version}")
            return 0
        if args.command == "overwrite":
            version = table.overwrite(_load_rows(args.file))
            print(f"overwrote as version {version}")
            return 0
        if args.command == "read":
            for row in table.read(args.version):
                print(json.dumps(row, separators=(",", ":")))
            return 0
        if args.command == "restore":
            version = table.restore(args.version)
            print(f"restored version {args.version} as version {version}")
            return 0
        if args.command == "vacuum":
            deleted = table.vacuum(older_than_ms=args.older_than)
            if not deleted:
                print("no orphan files")
                return 0
            print(f"deleted {len(deleted)} orphan file{'s' if len(deleted) != 1 else ''}")
            for name in deleted:
                print(name)
            return 0
        for commit in table.history():
            print(
                f"v{commit.version}  {commit.operation}  "
                f"readVersion={commit.read_version}  ts={commit.timestamp}"
            )
        return 0
    except TinyDeltaError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def _load_rows(path: str) -> list[dict[str, object]]:
    file_path = Path(path)
    if not file_path.is_file():
        raise TinyDeltaError(f"file not found: {file_path}")
    rows: list[dict[str, object]] = []
    for line in file_path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


if __name__ == "__main__":
    raise SystemExit(main())
