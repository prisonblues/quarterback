"""Comments restating the line below them."""


def slug(name):
    # This function converts a name to a slug
    # Now we strip the whitespace
    trimmed = name.strip()
    # Return the lowercase form
    return trimmed.lower()
