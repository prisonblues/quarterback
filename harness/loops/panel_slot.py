"""A panel round's memory reservation on the host's `heavy-slot` queue (#874).

Four or five seats run at once, and with code access a seat runs tests, so a round is a
heavy job; on 2026-11-15 two rounds were killed for memory before they produced anything.
`QB_PANEL_MEMORY` sets the size (default 6G, 0 turns it off) and `QB_PANEL_MEMORY_WAIT`
bounds the wait in minutes. Without `heavy-slot` on PATH a round runs as before.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess

#: Held until this process exits, which closes the holder's stdin and ends the reservation.
_HELD: list[subprocess.Popen] = []


def reserve(pr_number: int) -> str | None:
    """Wait for and hold the round's memory. Returns a note when the round goes without."""
    size = os.environ.get("QB_PANEL_MEMORY") or "6G"
    exe = shutil.which("heavy-slot")
    if re.fullmatch(r"\s*0+(\.0*)?\s*([KMGT]i?B?|B)?\s*", size, re.IGNORECASE) or not exe:
        return None
    args = [exe, "hold", "--min", size, "--label", f"panel PR #{pr_number}"]
    if os.environ.get("QB_PANEL_MEMORY_WAIT"):
        args += ["--timeout", os.environ["QB_PANEL_MEMORY_WAIT"]]
    # stderr is inherited, so heavy-slot's own "waiting for ..." line reaches the caller.
    try:
        holder = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  text=True)
    except OSError as e:
        return f"memory: heavy-slot could not start ({e}), so this round ran without one"
    try:
        json.loads(holder.stdout.readline())["grant_bytes"]
    except (ValueError, KeyError, TypeError):
        holder.stdin.close()
        return (f"memory: heavy-slot exited {holder.wait()} before granting {size}, so this "
                "round ran without a reservation")
    _HELD.append(holder)
    return None
