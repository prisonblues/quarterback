"""Handlers that do something: a narrow catch used as control flow, and a broad
one that records what it caught."""

import logging

log = logging.getLogger(__name__)


def cached(cache, path, read):
    try:
        return cache[path]
    except KeyError:
        pass  # a miss is not a failure — fall through to the read below
    return read(path)


def publish(row, wire):
    try:
        wire.send(row)
    except Exception:
        log.exception("publish failed for %r", row)
