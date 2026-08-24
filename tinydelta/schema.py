from __future__ import annotations

from dataclasses import dataclass

from tinydelta.errors import TinyDeltaError

VALID_TYPES = ("int", "float", "str", "bool")


@dataclass(frozen=True)
class Column:
    name: str
    type: str


@dataclass(frozen=True)
class Schema:
    columns: tuple[Column, ...]

    def names(self) -> list[str]:
        return [column.name for column in self.columns]

    def to_json(self) -> list[dict[str, str]]:
        return [{"name": column.name, "type": column.type} for column in self.columns]

    @classmethod
    def from_json(cls, data: list[dict[str, str]]) -> Schema:
        columns = tuple(Column(item["name"], item["type"]) for item in data)
        _validate_columns(columns)
        return cls(columns)

    @classmethod
    def parse(cls, specs: list[str]) -> Schema:
        columns: list[Column] = []
        for spec in specs:
            if ":" not in spec:
                raise TinyDeltaError(f"expected name:type, got {spec!r}")
            name, type_name = spec.split(":", 1)
            columns.append(Column(name.strip(), type_name.strip()))
        _validate_columns(tuple(columns))
        return cls(tuple(columns))


def coerce_row(schema: Schema, row: dict[str, object]) -> dict[str, object]:
    extra = set(row) - set(schema.names())
    if extra:
        raise TinyDeltaError(f"unknown columns: {sorted(extra)}")
    missing = set(schema.names()) - set(row)
    if missing:
        raise TinyDeltaError(f"missing columns: {sorted(missing)}")
    return {column.name: _coerce(column, row[column.name]) for column in schema.columns}


def _validate_columns(columns: tuple[Column, ...]) -> None:
    if not columns:
        raise TinyDeltaError("schema needs at least one column")
    names = [column.name for column in columns]
    if len(names) != len(set(names)):
        raise TinyDeltaError(f"duplicate column names: {names}")
    for column in columns:
        if not column.name:
            raise TinyDeltaError("column name is empty")
        if column.type not in VALID_TYPES:
            raise TinyDeltaError(
                f"unknown type {column.type!r}; expected {', '.join(VALID_TYPES)}"
            )


def _coerce(column: Column, value: object) -> object:
    if value is None:
        return None
    if column.type == "int":
        if isinstance(value, bool) or not isinstance(value, int):
            raise TinyDeltaError(f"{column.name} expected int, got {value!r}")
        return value
    if column.type == "float":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TinyDeltaError(f"{column.name} expected float, got {value!r}")
        return float(value)
    if column.type == "str":
        if not isinstance(value, str):
            raise TinyDeltaError(f"{column.name} expected str, got {value!r}")
        return value
    if column.type == "bool":
        if not isinstance(value, bool):
            raise TinyDeltaError(f"{column.name} expected bool, got {value!r}")
        return value
    raise TinyDeltaError(f"unknown type {column.type!r}")
