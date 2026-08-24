# tinydelta

Single-node table format. A directory of data files plus a JSON commit log. Readers only see files that a commit published.

This is the same shape as Delta Lake (log as source of truth, atomic commit, time travel), on one machine, with JSONL instead of Parquet.

Sister of [tinyquery](https://github.com/SpookyJumpyBeans/tinyquery): that repo is the query engine, this one is the table format.

## What works

- `create` an empty table with a schema
- `append` / `overwrite` rows from JSONL
- `read` the latest snapshot, or a past version
- Optimistic concurrency: two writers that both read version *n* cannot both commit *n+1*

## What it does not do

No Parquet, no checkpoints, no vacuum, no MERGE, no cloud object store. Removed files stay on disk. There is no retry loop; a conflict is an error.

## Run

```bash
python -m tinydelta create ./orders id:int region:str year:int
python -m tinydelta append ./orders --file examples/orders.jsonl
python -m tinydelta read ./orders
python -m tinydelta overwrite ./orders --file examples/orders.jsonl
python -m tinydelta read ./orders --version 1
python -m tinydelta history ./orders
```

```bash
pip install -e ".[dev]"
pytest
```

## How a write becomes visible

```text
rows
  → write part-<uuid>.jsonl  (not readable yet)
  → create _delta_log/<version>.json with O_CREAT|O_EXCL
  → fsync that file
```

The commit file is the publish point. `os.open(..., O_CREAT|O_EXCL)` fails if that version already exists, so the second writer loses instead of overwriting the first. Data files are written first so a crash during the log create does not publish a half-written JSONL.

A reader reconstructs a version by replaying `add` / `remove` from 0 to *n*. A JSONL sitting in the directory with no `add` action is garbage, not a row.

`overwrite` removes every file in the current snapshot and adds a new one. Version *n-1* still reads the old files; nothing is deleted from disk.

## Example

After create, append, overwrite:

```text
v0  CREATE
v1  APPEND     (3 orders)
v2  OVERWRITE  (3 orders, new data file)

read()           → version 2
read(version=1)  → the first append
```
