"""Allow running as `python -m mcp_server`."""

import sys

try:
    from mcp_server.server import main
except ImportError as e:
    # The MCP SDK is a `server` extra, not a base dependency, so an install made
    # for the board client alone reaches here — and the bare ModuleNotFoundError
    # names `mcp`, which reads like the package itself is broken. Say which
    # install this is instead. Only the SDK's own absence is translated: an
    # ImportError from our code is a real defect and must surface as itself.
    if (e.name or "").partition(".")[0] != "mcp":
        raise
    sys.exit(
        "mcp_server: the MCP server needs the `server` extra, which this install "
        "does not have.\n"
        "        Install it with: pip install 'quarterback-mcp[server]'\n"
        "        (`qb-board`, the terminal board client, needs only the base install.)"
    )

main()
