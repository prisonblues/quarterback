"""Numbered narration captioning a function instead of breaking it up."""


def publish(rows, wire):
    # Step 1: drop the empties
    rows = [r for r in rows if r]
    # Step 2: sort them
    rows.sort()
    # 3. hand them to the wire
    wire.send(rows)
