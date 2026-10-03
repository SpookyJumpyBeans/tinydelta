from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

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


TEMP_SUFFIX = ".tmp"


def write_commit(log_directory: Path, version: int, actions: list[dict]) -> None:
    """Publish a commit so it appears complete or not at all.

    Two properties have to hold together:

      * exclusivity: if two writers race for the same version, exactly one
        wins and the other gets ConcurrentWriteError;
      * atomic visibility: a reader that sees the version file sees all of it.

    Creating the version file directly with O_EXCL gets the first but not the
    second. The file exists, empty, between creating it and writing to it, and
    a reader listing the log in that window treats an empty commit as the
    latest version. Once the writer finishes, the same version number then
    returns different data, so a version is no longer immutable once visible.

    So the bytes go to a uniquely named temp file first, are flushed to disk,
    and only then is the temp file hard-linked to the version name. link() is
    atomic and fails if the target already exists, which keeps exclusivity,
    and the version name can only ever appear pointing at complete contents.
    Temp names do not match list_versions(), so readers never see them.
    """
    path = version_path(log_directory, version)
    payload = "".join(json.dumps(action, separators=(",", ":")) + "\n" for action in actions)
    temp = log_directory / f".{version:020d}.{uuid4().hex}{TEMP_SUFFIX}"
    try:
        # A buffered file object writes every byte; a bare os.write may not.
        with open(temp, "xb") as handle:
            handle.write(payload.encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temp, path)
        except FileExistsError as exc:
            raise ConcurrentWriteError(version) from exc
        _fsync_directory(log_directory)
    finally:
        temp.unlink(missing_ok=True)


def _fsync_directory(directory: Path) -> None:
    """Make the new directory entry durable, where the platform allows it.

    On POSIX a crash after link() but before the directory is flushed can lose
    the new name even though the file's bytes are on disk. Windows cannot open
    a directory this way, and NTFS journals the entry anyway, so skip it there.
    """
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def stale_temp_files(log_directory: Path) -> list[Path]:
    """Temp files a crashed writer left behind; never part of any version."""
    return sorted(log_directory.glob(f".*{TEMP_SUFFIX}"))


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
