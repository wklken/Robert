"""Materialized database records shared by named persistence operations."""

from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True, slots=True, eq=False, repr=False)
class Record(Sequence):
    _names: tuple[str, ...]
    _values: tuple

    def __len__(self):
        return len(self._values)

    def __iter__(self):
        return iter(self._values)

    def __getitem__(self, key):
        if isinstance(key, str):
            for index, name in enumerate(self._names):
                if name == key or (name.isascii() and key.isascii() and name.lower() == key.lower()):
                    return self._values[index]
            raise IndexError("No item with that key")
        return self._values[key]

    def __eq__(self, other):
        if isinstance(other, Record):
            return self._values == other._values
        if isinstance(other, tuple):
            return self._values == other
        return NotImplemented

    def keys(self):
        return list(self._names)


def first(records: Sequence[Record]) -> Record | None:
    return records[0] if records else None
