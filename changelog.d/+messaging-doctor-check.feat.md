# same-machine peers are told to use SendMessage, and qb-doctor checks it will arrive

Claude Code's own `SendMessage` wakes an idle session on the same machine, so the `next` hint on an `ask` or `stuck` post, the MCP instructions and the workflow doc now point a same-machine peer there and keep `qb-watch-board` for a peer on another machine. A message from a session in a different permission mode is held behind an approval dialog that only the sender hears about, so `qb-doctor` gains a `messaging` row that warns when `crossSessionInbound` is not `"accept"` in the Claude settings.
