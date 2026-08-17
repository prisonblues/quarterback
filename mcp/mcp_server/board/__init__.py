"""``qb board`` — a terminal client for the coordination board.

``GET /`` is a browser view behind Authelia, which is fine on a desktop and
unreachable from the headless half of the fleet — precisely the machines where
work runs unattended. This package is the other surface: a client that reaches
every host over ssh, and, because it is a local process rather than a browser
tab, can act on the machine it runs on.

Two halves, and the cheap one stands alone:

* :mod:`~mcp_server.board.follow` — the board tailed to stdout as plain lines.
  No TUI, no extra dependency, pipeable and greppable.
* :mod:`~mcp_server.board.tui` — the full-screen client (Textual, an optional
  extra), with the pull / cherry-pick / resume actions that justify it existing.

Both are consumers of :class:`mcp_server.client.QuarterbackClient`. There were
already two clients for this board — the browser's JavaScript and that one — and
a third would be the thing to avoid, not the thing to build.
"""
