"""Wrappers that earn their frame: one transforms an argument before forwarding
it, one is what a decorator is wrapping, and one returns no call at all."""

import functools

MIN_WIDTH = 8


def render(record, width):
    return f"{record:>{width}}"


def format_row(record, width):
    return render(record, max(width, MIN_WIDTH))


@functools.lru_cache(maxsize=64)
def cached_row(record, width):
    return render(record, width)
