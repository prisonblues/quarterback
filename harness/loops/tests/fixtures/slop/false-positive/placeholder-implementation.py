"""Empty bodies that are the point rather than an omission."""

from abc import ABC, abstractmethod
from typing import Protocol, overload


class Store(ABC):
    """A backend contract; the bodies live in the subclasses."""

    @abstractmethod
    def get(self, key):
        """Every backend must answer this."""


class Reader(Protocol):
    """Structural typing — the ellipsis IS the declaration, not a gap in one."""

    def read(self) -> bytes:
        ...


@overload
def widen(value: int) -> int:
    ...


@overload
def widen(value: str) -> str:
    ...


def widen(value):
    """The real implementation the overloads describe."""
    return value
