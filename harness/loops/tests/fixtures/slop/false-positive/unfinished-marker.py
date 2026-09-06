"""A marker with a home — tracked work, referenced by issue — and a comment that
happens to contain one of the words."""


def render(rows):
    # TODO(#697): drop the compatibility branch once the ratchet reaches zero.
    # The hack of padding to 80 columns is deliberate and #554 says why.
    return "\n".join(str(r).ljust(80) for r in rows)
