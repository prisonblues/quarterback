"""Real types, and the one Any that is honest: an untyped JSON boundary, where
the parameter and the return are both modelled and only the values are not."""

import json
from typing import Any


def decode(payload: str) -> dict[str, Any]:
    return json.loads(payload)


def width_of(row: dict[str, Any]) -> int:
    return len(row)
