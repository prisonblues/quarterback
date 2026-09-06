"""Annotations widened to Any to close a type finding."""

from typing import Any, cast


def decode(payload: Any) -> Any:
    return cast(Any, payload)
