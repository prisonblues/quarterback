# a codex pin the box's account refuses no longer kills codex_round

`codex_round.py` exited 2 on every box whose codex account could not serve the
repo's pinned model: the ChatGPT subscription on zeus and daedalus refuses
`gpt-5.5`, and the API gateway on the work laptop and sisyphus serves nothing
else. Agents fell back to hand-written `codex exec` loops. A refused pin is now
retried once on the CLI default, with a note naming what was dropped, and this
repo's own sample leaves the codex seat on each box's CLI default.
