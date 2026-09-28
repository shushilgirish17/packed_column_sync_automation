"""
Run the sync against the real tenant from your machine, without AWS (files + state go to ./out).

    python -m tools.run_local bfi_abc123 bfi_def456
    python -m tools.run_local --entry etr_xyz          # every Packed Column registered in an entry
    python -m tools.run_local --all                    # every Packed Column

Needs BENCHLING_URL / BENCHLING_CLIENT_ID / BENCHLING_CLIENT_SECRET, PACKED_COLUMN_SCHEMA_ID,
RESIN_LIBRARY_SCHEMA_ID. Leave SQS_QUEUE_URL unset: jobs are written to ./out/incoming.
"""
import argparse
import json
import logging

from auth_utils.authentication import initialize_benchling
from sync.benchling_reader import BenchlingReader
from sync.packed_column_sync import sync_columns


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("column_ids", nargs="*")
    ap.add_argument("--entry")
    ap.add_argument("--all", action="store_true")
    args = ap.parse_args()

    reader = BenchlingReader(initialize_benchling())
    if args.all:
        ids = reader.list_all_column_ids()
    elif args.entry:
        ids = reader.list_column_ids_in_entry(args.entry)
    else:
        ids = args.column_ids
    outcomes = sync_columns(reader, ids, {"type": "manual", "user": "local", "entry_id": args.entry})
    print(json.dumps([o.__dict__ for o in outcomes], indent=2, default=str))


if __name__ == "__main__":
    main()
