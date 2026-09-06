"""Comments that carry a number without captioning a sequence, each opening its
own block so the pattern is what has to reject it."""

MAX_FILES = 300


def publish(rows, wire):
    #483) dropped the tail of a list, which is why this slice is spelled out.
    wire.send(rows[:MAX_FILES])


def settle(rows, wire):
    # Step 2 of that read is the only one that can fail, and it degrades to None.
    wire.send(rows)


def widen(rows, wire):
    # 300 files is the compare API's ceiling, so a wider pass is reported short.
    wire.send(rows[:MAX_FILES])
