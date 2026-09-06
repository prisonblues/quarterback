"""Imports that are used, re-exported, or kept for a side effect no linter and no
matcher can see from the file alone."""

import json
from collections import Counter

import _path_sandbox  # noqa: F401 — imported for the PATH and HOME it builds

__all__ = ["Counter", "encode"]


def encode(rows):
    return json.dumps(rows)
