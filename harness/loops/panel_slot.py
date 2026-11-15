"""A panel round's memory reservation on the host's `heavy-slot` queue (#874).

Four or five seats run at once, and with code access a seat runs tests, so a round is
one of the heavy jobs on a box. On 2026-11-15 two rounds were killed for memory before
they produced anything, while lexray CI held the box. The round now takes a reservation
before its seats start and waits for room instead of starting into a squeeze.

`QB_PANEL_MEMORY` sets the size (default 6G; `0` turns the wait off), and
`QB_PANEL_MEMORY_WAIT` bounds the wait in minutes (default: wait). A host without
`heavy-slot` runs the round as before, with one line on stderr saying so.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

DEFAULT_SIZE = "6G"
#: The holder lives until this process exits: the round ends with it, and an exit of any
#: kind closes the holder's stdin, which is what ends the reservation.
_HELD: list[subprocess.Popen] = []


def reserve(notes: list[str], pr_number: int) -> subprocess.Popen | None:
    """Wait for the round's reservation and hold it for the rest of the process."""
    size = os.environ.get("QB_PANEL_MEMORY") or DEFAULT_SIZE
    if size == "0":
        return None
    exe = shutil.which("heavy-slot")
    if not exe:
        print("panel: heavy-slot is not on PATH, so this round does not wait for memory",
              file=sys.stderr)
        return None
    args = [exe, "hold", "--min", size, "--label", f"panel PR #{pr_number}"]
    if os.environ.get("QB_PANEL_MEMORY_WAIT"):
        args += ["--timeout", os.environ["QB_PANEL_MEMORY_WAIT"]]
    # stderr is inherited, so heavy-slot's own "waiting for …" line reaches the caller.
    holder = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    if holder.stdout.readline():
        _HELD.append(holder)
        return holder
    notes.append(f"memory: heavy-slot exited {holder.wait()} before granting {size}, so "
                 "this round ran without a reservation")
    return None
