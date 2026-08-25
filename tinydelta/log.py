from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from tinydelta.errors import ConcurrentWriteError, TinyDeltaError
from tinydelta.schema import Schema

LOG_DIR_NAME = "_delta_log"


@dataclass(frozen=True)
class AddFile:
    path: str
    size: int
    num_records: int


@dataclass(frozen=True)
class Snapshot:
    version: int
    schema: Schema
    files: tuple[AddFile, ...]
    table_id: str


@dataclass(frozen=True)
class Commit:
    version: int
    operation: str
    timestamp: int
    read_version: int | None


def log_dir(table_path: Path) -> Path:
    return table_path / LOG_DIR_NAME


def version_path(log_directory: Path, version: int) -> Path:
    return log_directory / f"{version:020d}.json"


def list_versions(log_directory: Path) -> list[int]:
    versions: list[int] = []
    for path in log_directory.glob("*.json"):
        if path.stem.isdigit() and len(path.stem) == 20:
            versions.append(int(path.stem))
    return sorted(versions)


def write_commit(log_directory: Path, version: int, actions: list[dict]) -> None:
    path = version_path(log_directory, version)
    payload = "".join(json.dumps(action, separators=(",", ":")) + "\n" for action in actions)
    data = payload.encode("utf-8")
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    try:
        fd = os.open(path, flags, 0o644)
    except FileExistsError as exc:
        raise ConcurrentWriteError(version) from exc
    try:
        os.write(fd, data)
        os.fsync(fd)
    except Exception:
        os.close(fd)
        try:
            path.unlink()
        except OSError:
            pass
        raise
    os.close(fd)


def read_actions(log_directory: Path, version: int) -> list[dict]:
    path = version_path(log_directory, version)
    if not path.is_file():
        raise TinyDeltaError(f"missing commit file for version {version}")
    actions: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            actions.append(json.loads(line))
    return actions


def load_snapshot(log_directory: Path, version: int | None = None) -> Snapshot:
    versions = list_versions(log_directory)
    if not versions:
        raise TinyDeltaError("not a tinydelta table (empty log)")
    target = versions[-1] if version is None else version
    if target < 0 or target > versions[-1]:
        raise TinyDeltaError(f"no such version: {target}")
    if target not in versions:
        raise TinyDeltaError(f"gap in log: missing version {target}")

    schema: Schema | None = None
    table_id: str | None = None
    files: dict[str, AddFile] = {}
    for current in range(target + 1):
        for action in read_actions(log_directory, current):
            if "metaData" in action:
                meta = action["metaData"]
                schema = Schema.from_json(meta["schema"])
                table_id = meta["id"]
            elif "add" in action:
                added = action["add"]
                files[added["path"]] = AddFile(
                    path=added["path"],
                    size=added["size"],
                    num_records=added["numRecords"],
                )
            elif "remove" in action:
                files.pop(action["remove"]["path"], None)
    if schema is None or table_id is None:
        raise TinyDeltaError("log is missing table metadata")
    return Snapshot(version=target, schema=schema, files=tuple(files.values()), table_id=table_id)


def all_added_paths(log_directory: Path) -> set[str]:
    """Every data file ever published, including ones later removed.

    Vacuum must not delete these: time travel still reads them.
    """
    paths: set[str] = set()
    for version in list_versions(log_directory):
        for action in read_actions(log_directory, version):
            if "add" in action:
                paths.add(action["add"]["path"])
    return paths


def load_history(log_directory: Path) -> list[Commit]:
    history: list[Commit] = []
    for version in list_versions(log_directory):
        info = None
        for action in read_actions(log_directory, version):
            if "commitInfo" in action:
                info = action["commitInfo"]
                break
        if info is None:
            raise TinyDeltaError(f"version {version} is missing commitInfo")
        history.append(
            Commit(
                version=version,
                operation=info["operation"],
                timestamp=info["timestamp"],
                read_version=info.get("readVersion"),
            )
        )
    return history
