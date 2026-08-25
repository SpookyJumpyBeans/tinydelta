from __future__ import annotations

import json
import time
from pathlib import Path
from uuid import uuid4

from tinydelta.errors import TinyDeltaError
from tinydelta.log import (
    AddFile,
    Commit,
    Snapshot,
    all_added_paths,
    load_history,
    load_snapshot,
    log_dir,
    write_commit,
)
from tinydelta.schema import Schema, coerce_row


class DeltaTable:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.log_dir = log_dir(path)

    @classmethod
    def create(cls, path: str | Path, schema: Schema) -> DeltaTable:
        table_path = Path(path)
        if table_path.exists():
            raise TinyDeltaError(f"path already exists: {table_path}")
        table_path.mkdir(parents=True)
        log_dir(table_path).mkdir()
        table = cls(table_path)
        table._commit(
            version=0,
            read_version=None,
            operation="CREATE",
            actions=[
                {
                    "metaData": {
                        "id": str(uuid4()),
                        "schema": schema.to_json(),
                    }
                }
            ],
        )
        return table

    @classmethod
    def open(cls, path: str | Path) -> DeltaTable:
        table_path = Path(path)
        if not log_dir(table_path).is_dir():
            raise TinyDeltaError(f"not a tinydelta table: {table_path}")
        table = cls(table_path)
        load_snapshot(table.log_dir)
        return table

    def snapshot(self, version: int | None = None) -> Snapshot:
        return load_snapshot(self.log_dir, version)

    def append(self, rows: list[dict[str, object]], *, read_version: int | None = None) -> int:
        snapshot = self._base_snapshot(read_version)
        added = self._write_data(snapshot.schema, rows)
        return self._commit(
            version=snapshot.version + 1,
            read_version=snapshot.version,
            operation="APPEND",
            actions=[{"add": _add_action(added)}],
        )

    def overwrite(self, rows: list[dict[str, object]], *, read_version: int | None = None) -> int:
        snapshot = self._base_snapshot(read_version)
        added = self._write_data(snapshot.schema, rows)
        actions: list[dict] = [{"remove": {"path": file.path}} for file in snapshot.files]
        actions.append({"add": _add_action(added)})
        return self._commit(
            version=snapshot.version + 1,
            read_version=snapshot.version,
            operation="OVERWRITE",
            actions=actions,
        )

    def read(self, version: int | None = None) -> list[dict[str, object]]:
        snapshot = self.snapshot(version)
        rows: list[dict[str, object]] = []
        for file in snapshot.files:
            rows.extend(_read_jsonl(self.path / file.path))
        return rows

    def history(self) -> list[Commit]:
        return load_history(self.log_dir)

    def vacuum(self, *, older_than_ms: int = 0) -> list[str]:
        """Delete data files the log never published.

        Files that appear in any `add` action are kept, even after overwrite,
        so `read(version=k)` still works. In-flight writes (data file on disk,
        commit not created yet) look like orphans; pass older_than_ms if another
        writer may still be committing.
        """
        if older_than_ms < 0:
            raise TinyDeltaError("older_than_ms must be >= 0")
        referenced = all_added_paths(self.log_dir)
        now_ms = time.time() * 1000
        deleted: list[str] = []
        for path in sorted(self.path.glob("part-*.jsonl")):
            if path.name in referenced:
                continue
            age_ms = now_ms - path.stat().st_mtime * 1000
            if age_ms < older_than_ms:
                continue
            path.unlink()
            deleted.append(path.name)
        return deleted

    def _base_snapshot(self, read_version: int | None) -> Snapshot:
        latest = self.snapshot()
        if read_version is None:
            return latest
        return self.snapshot(read_version)

    def _write_data(self, schema: Schema, rows: list[dict[str, object]]) -> AddFile:
        if not rows:
            raise TinyDeltaError("write requires at least one row")
        coerced = [coerce_row(schema, row) for row in rows]
        name = f"part-{uuid4().hex}.jsonl"
        path = self.path / name
        payload = "".join(json.dumps(row, separators=(",", ":")) + "\n" for row in coerced)
        path.write_text(payload, encoding="utf-8")
        return AddFile(path=name, size=path.stat().st_size, num_records=len(coerced))

    def _commit(
        self,
        version: int,
        read_version: int | None,
        operation: str,
        actions: list[dict],
    ) -> int:
        header = {
            "commitInfo": {
                "timestamp": int(time.time() * 1000),
                "operation": operation,
                "readVersion": read_version,
            }
        }
        write_commit(self.log_dir, version, [header, *actions])
        return version


def _add_action(file: AddFile) -> dict:
    return {"path": file.path, "size": file.size, "numRecords": file.num_records}


def _read_jsonl(path: Path) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows
