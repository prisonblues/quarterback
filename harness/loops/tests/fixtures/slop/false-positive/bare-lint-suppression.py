"""Suppressions that name what they silence, which is this repo's convention."""

import json  # noqa: F401 — re-exported so callers can import it from here


def encode(rows):
    payload = repr(rows)  # noqa: E501 — the format string is what makes it long
    return payload


def widen(value):
    return value.decode()  # type: ignore[union-attr]
