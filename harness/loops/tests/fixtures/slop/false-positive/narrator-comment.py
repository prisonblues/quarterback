"""Comments carrying an argument the code cannot."""


def slug(name):
    # Casefold rather than lower(): the board addresses agents by machine name,
    # and a Turkish locale's dotless i would split one machine into two.
    return name.strip().casefold()
