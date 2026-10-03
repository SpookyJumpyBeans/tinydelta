"""Concurrency, with real threads rather than a simulated stale read_version.

The guarantees under test:

  * a commit is visible completely or not at all;
  * two writers racing for one version produce exactly one winner;
  * writers that retry after losing never lose or duplicate a row;
  * a version, once visible, never changes.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path

import pytest

from tinydelta.errors import ConcurrentWriteError
from tinydelta.log import list_versions, stale_temp_files
from tinydelta.schema import Schema
from tinydelta.table import DeltaTable

SCHEMA = Schema.parse(["writer:int", "seq:int"])


def new_table(path: Path) -> DeltaTable:
    table = DeltaTable.create(path / "t", SCHEMA)
    table.append([{"writer": -1, "seq": 0}])  # version 1
    return table


class _PausingFile:
    """A file handle whose first write blocks until the test releases it."""

    def __init__(self, handle, pause):
        self._handle = handle
        self._pause = pause

    def write(self, data):
        self._pause()
        return self._handle.write(data)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return self._handle.__exit__(*exc)

    def __getattr__(self, name):
        return getattr(self._handle, name)


def test_a_commit_is_not_visible_while_its_bytes_are_being_written(tmp_path, monkeypatch):
    """Regression for the publish window, independent of how commits are written.

    The writer is frozen at the moment it starts writing the commit's bytes,
    through either os.write or a file object. A reader at that moment must
    still see the previous version. The original implementation created the
    version file with O_EXCL and then wrote into it, so it fails this test:
    the reader finds an empty version 2.
    """
    import tinydelta.log as log
    import tinydelta.table as table_module

    table = new_table(tmp_path)
    in_commit = threading.Event()
    writing = threading.Event()
    release = threading.Event()

    def pause() -> None:
        if in_commit.is_set() and not writing.is_set():
            writing.set()
            assert release.wait(5), "test never released the writer"

    real_write_commit = table_module.write_commit

    def flagged_write_commit(*args, **kwargs):
        in_commit.set()
        return real_write_commit(*args, **kwargs)

    real_os_write = os.write
    real_open = open

    def paused_os_write(fd, data):
        pause()
        return real_os_write(fd, data)

    def paused_open(*args, **kwargs):
        return _PausingFile(real_open(*args, **kwargs), pause)

    monkeypatch.setattr(table_module, "write_commit", flagged_write_commit)
    monkeypatch.setattr(os, "write", paused_os_write)
    monkeypatch.setattr(log, "open", paused_open, raising=False)

    writer = threading.Thread(target=lambda: table.append([{"writer": 1, "seq": 1}]))
    writer.start()
    try:
        assert writing.wait(5), "writer never started writing its commit"
        reader = DeltaTable.open(table.path)
        assert reader.describe()["version"] == 1
        assert len(reader.read()) == 1
    finally:
        release.set()
        writer.join(5)

    assert DeltaTable.open(table.path).describe()["version"] == 2


def test_a_commit_is_invisible_until_it_is_complete(tmp_path, monkeypatch):
    """Regression for the publish window.

    The old writer created the version file with O_EXCL and then wrote into
    it, so a reader in between saw an empty commit as the latest version. Here
    the writer is frozen after its bytes are on disk but before the version
    name exists, which is the last moment it could leak.
    """
    table = new_table(tmp_path)
    linking = threading.Event()
    release = threading.Event()
    real_link = os.link

    def paused_link(src, dst, *args, **kwargs):
        linking.set()
        assert release.wait(5), "test never released the writer"
        return real_link(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "link", paused_link)
    writer = threading.Thread(
        target=lambda: table.append([{"writer": 1, "seq": 1}])
    )
    writer.start()
    try:
        assert linking.wait(5), "writer never reached the publish step"
        reader = DeltaTable.open(table.path)
        assert reader.describe()["version"] == 1
        assert len(reader.read()) == 1
    finally:
        release.set()
        writer.join(5)

    after = DeltaTable.open(table.path)
    assert after.describe()["version"] == 2
    assert len(after.read()) == 2


def test_racing_writers_produce_exactly_one_winner(tmp_path):
    table = new_table(tmp_path)
    n = 8
    start = threading.Barrier(n)
    outcomes: list[str] = []
    lock = threading.Lock()

    def write(i: int) -> None:
        writer = DeltaTable.open(table.path)
        start.wait()
        try:
            writer.append([{"writer": i, "seq": 0}], read_version=1)
            result = "won"
        except ConcurrentWriteError:
            result = "lost"
        with lock:
            outcomes.append(result)

    threads = [threading.Thread(target=write, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)

    assert outcomes.count("won") == 1
    assert outcomes.count("lost") == n - 1
    final = DeltaTable.open(table.path)
    assert final.describe()["version"] == 2
    assert len(final.read()) == 2  # the base row plus the single winner's


def test_writers_that_retry_lose_and_duplicate_nothing(tmp_path):
    table = new_table(tmp_path)
    writers, per_writer = 4, 8
    start = threading.Barrier(writers)
    errors: list[BaseException] = []

    def write(w: int) -> None:
        handle = DeltaTable.open(table.path)
        start.wait()
        try:
            for seq in range(per_writer):
                while True:
                    try:
                        handle.append([{"writer": w, "seq": seq}])
                        break
                    except ConcurrentWriteError:
                        continue  # someone else took that version; re-read and retry
        except BaseException as exc:  # surface anything unexpected to the main thread
            errors.append(exc)

    threads = [threading.Thread(target=write, args=(w,)) for w in range(writers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)

    assert not errors, errors
    rows = DeltaTable.open(table.path).read()
    written = sorted((r["writer"], r["seq"]) for r in rows if r["writer"] >= 0)
    expected = sorted((w, s) for w in range(writers) for s in range(per_writer))
    assert written == expected  # every row exactly once
    versions = list_versions(table.log_dir)
    assert versions == list(range(len(versions)))  # no gaps in the log
    assert versions[-1] == 1 + writers * per_writer


def test_a_visible_version_never_changes(tmp_path):
    """Hammer the log with large commits while a reader watches.

    The reader records what each version contained the first time it saw it.
    Afterwards every version is read again and must match exactly.

    This is a stress check, not the regression guard for the publish window.
    Commit payloads are small, so that window lasted microseconds and random
    timing almost never hit it: this test passed 6 times out of 6 against the
    original implementation. The deterministic test above is what catches it.
    """
    table = new_table(tmp_path)
    commits, rows_per_commit = 20, 500
    done = threading.Event()
    seen: dict[int, list] = {}
    failures: list[BaseException] = []

    def write() -> None:
        try:
            for c in range(commits):
                table.append(
                    [{"writer": 0, "seq": c * rows_per_commit + i} for i in range(rows_per_commit)]
                )
        finally:
            done.set()

    def read() -> None:
        try:
            while not done.is_set():
                reader = DeltaTable.open(table.path)
                info = reader.describe()
                rows = reader.read(version=info["version"])
                assert len(rows) == info["num_records"]
                seen.setdefault(info["version"], rows)
        except BaseException as exc:
            failures.append(exc)

    w = threading.Thread(target=write)
    r = threading.Thread(target=read)
    r.start()
    w.start()
    w.join(60)
    r.join(60)

    assert not failures, failures
    assert len(seen) > 1, "reader never overlapped the writer; test proved nothing"
    final = DeltaTable.open(table.path)
    for version, rows in seen.items():
        assert final.read(version=version) == rows, f"version {version} changed"


def test_a_failed_publish_leaves_nothing_behind(tmp_path, monkeypatch):
    table = new_table(tmp_path)

    def broken_link(src, dst, *args, **kwargs):
        raise OSError("simulated failure while publishing")

    monkeypatch.setattr(os, "link", broken_link)
    with pytest.raises(OSError):
        table.append([{"writer": 1, "seq": 1}])
    monkeypatch.undo()

    assert list_versions(table.log_dir)[-1] == 1  # nothing published
    assert stale_temp_files(table.log_dir) == []  # and nothing left lying around
    table.append([{"writer": 1, "seq": 1}])  # version 2 is still free
    assert DeltaTable.open(table.path).describe()["version"] == 2


def test_temp_files_are_invisible_and_vacuum_removes_them(tmp_path):
    """A crashed writer can leave a temp file. Readers must ignore it."""
    table = new_table(tmp_path)
    debris = table.log_dir / f".{2:020d}.deadbeef.tmp"
    debris.write_text('{"add":{"path":"ghost","size":1,"numRecords":1}}\n')

    assert list_versions(table.log_dir) == [0, 1]
    assert DeltaTable.open(table.path).describe()["version"] == 1

    deleted = table.vacuum(older_than_ms=0)
    assert f"_delta_log/{debris.name}" in deleted
    assert not debris.exists()
    assert list_versions(table.log_dir) == [0, 1]
