# the bash and Python halves name the same pane file under uutils coreutils

`qb_pane_key` read the socket's mtime with `stat -c %Y`. Under uutils coreutils
(Ubuntu 26.04's default, so CI's `ubuntu-latest`), that rounds an mtime within about
100ns of the next second up rather than truncating it, so the hook and the MCP server
named pane files one second apart. `test_pane_parity` caught it in CI. It now reads
`date -r FILE +%s`, which truncates under both GNU and uutils.
