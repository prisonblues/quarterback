"""A wrapper forwarding its own parameters, unchanged, to one call — the shape
"extract this into a helper" produces when there is nothing to extract."""


def render(record, width):
    return f"{record:>{width}}"


def format_row(record, width):
    return render(record, width)
