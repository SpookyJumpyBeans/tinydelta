from pathlib import Path

import pytest

from tinydelta.errors import ConcurrentWriteError, TinyDeltaError
from tinydelta.schema import Schema
from tinydelta.table import DeltaTable

SCHEMA = Schema.parse(["id:int", "region:str", "year:int"])


def _create(tmp_path: Path) -> DeltaTable:
    return DeltaTable.create(tmp_path / "orders", SCHEMA)


def test_create_then_read_empty(tmp_path: Path) -> None:
    table = _create(tmp_path)
    assert table.read() == []
    assert table.snapshot().version == 0
    assert table.history()[0].operation == "CREATE"


def test_append_then_read(tmp_path: Path) -> None:
    table = _create(tmp_path)
    table.append(
        [
            {"id": 1, "region": "west", "year": 2024},
            {"id": 2, "region": "east", "year": 2024},
        ]
    )
    assert table.read() == [
        {"id": 1, "region": "west", "year": 2024},
        {"id": 2, "region": "east", "year": 2024},
    ]
    assert table.snapshot().version == 1


def test_overwrite_keeps_old_version(tmp_path: Path) -> None:
    table = _create(tmp_path)
    table.append([{"id": 1, "region": "west", "year": 2024}])
    table.overwrite([{"id": 9, "region": "north", "year": 2025}])
    assert table.read() == [{"id": 9, "region": "north", "year": 2025}]
    assert table.read(version=1) == [{"id": 1, "region": "west", "year": 2024}]
    assert table.read(version=0) == []


def test_stale_writer_conflicts(tmp_path: Path) -> None:
    table = _create(tmp_path)
    table.append([{"id": 1, "region": "west", "year": 2024}], read_version=0)
    with pytest.raises(ConcurrentWriteError) as exc:
        table.append([{"id": 2, "region": "east", "year": 2024}], read_version=0)
    assert exc.value.version == 1
    assert table.read() == [{"id": 1, "region": "west", "year": 2024}]


def test_uncommitted_file_is_invisible(tmp_path: Path) -> None:
    table = _create(tmp_path)
    table.append([{"id": 1, "region": "west", "year": 2024}])
    orphan = table.path / "part-orphan.jsonl"
    orphan.write_text('{"id":99,"region":"ghost","year":1999}\n', encoding="utf-8")
    assert table.read() == [{"id": 1, "region": "west", "year": 2024}]


def test_schema_mismatch_does_not_commit(tmp_path: Path) -> None:
    table = _create(tmp_path)
    with pytest.raises(TinyDeltaError, match="unknown columns"):
        table.append([{"id": 1, "region": "west", "year": 2024, "extra": True}])
    assert table.snapshot().version == 0
    assert table.read() == []


def test_missing_table(tmp_path: Path) -> None:
    with pytest.raises(TinyDeltaError, match="not a tinydelta table"):
        DeltaTable.open(tmp_path / "nope")


def test_vacuum_deletes_orphans_not_history(tmp_path: Path) -> None:
    table = _create(tmp_path)
    table.append([{"id": 1, "region": "west", "year": 2024}])
    table.overwrite([{"id": 9, "region": "north", "year": 2025}])
    orphan = table.path / "part-orphan.jsonl"
    orphan.write_text('{"id":99,"region":"ghost","year":1999}\n', encoding="utf-8")

    deleted = table.vacuum()
    assert deleted == ["part-orphan.jsonl"]
    assert not orphan.exists()
    assert table.read() == [{"id": 9, "region": "north", "year": 2025}]
    assert table.read(version=1) == [{"id": 1, "region": "west", "year": 2024}]


def test_vacuum_respects_retention(tmp_path: Path) -> None:
    table = _create(tmp_path)
    orphan = table.path / "part-orphan.jsonl"
    orphan.write_text('{"id":99,"region":"ghost","year":1999}\n', encoding="utf-8")
    assert table.vacuum(older_than_ms=60_000) == []
    assert orphan.exists()
    assert table.vacuum(older_than_ms=0) == ["part-orphan.jsonl"]
