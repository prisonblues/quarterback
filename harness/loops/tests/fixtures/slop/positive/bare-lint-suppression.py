"""Suppressions that name no rule code, so nobody can tell what they silenced."""

import json


def encode(rows):
    payload = json.dumps(rows)  # noqa
    return payload


def widen(value):
    return value.decode()  # type: ignore
