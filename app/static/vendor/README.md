# app/static/vendor — third-party code, byte for byte

Everything here was fetched from somewhere else and is checked in unmodified. The
pin below is the answer to "which one is this", which is the question a vendored
minified file cannot answer about itself.

**Do not edit these files.** To change one, fetch the new version, put its URL and
`sha256sum` in the table, and update the pin `tests/test_plan_page.py` asserts.

## sortable.min.js

| | |
|---|---|
| package | [SortableJS](https://github.com/SortableJS/Sortable) |
| version | 1.15.6 |
| licence | MIT |
| source | `https://cdn.jsdelivr.net/npm/sortablejs@1.15.6/Sortable.min.js` |
| sha256 | `6d0a831fc19b4bae851797ad3393157e861afb7862459c11226359b27e2c4337` |
| bytes | 45092 |

The same bytes come back from
`https://unpkg.com/sortablejs@1.15.6/Sortable.min.js` and from
`https://raw.githubusercontent.com/SortableJS/Sortable/1.15.6/Sortable.min.js`, which is
how the checksum above was cross-checked rather than merely recorded.

This is the UMD `dist` build, which already has the AutoScroll plugin mounted —
that matters, because autoscrolling a list taller than the screen is the part of
drag-to-reorder a phone actually needs and the part a hand-rolled version gets
wrong (#388).

### Why it is served rather than inlined

`app/api/board_view.py` `read_text()`s every page at import and returns it as a
string; there is no `StaticFiles` mount in this app and no build step. Inlining
45KB of minified third-party code into a 22KB hand-written page would triple it
and put a blob in every future diff of that file. So it gets one more handler in
`board_view.py`, in the same shape as the page handlers — including reading at
import, so an asset that failed to ship is a **startup crash rather than a silent
404** on a page that would then just quietly have no drag. That failure mode is
#169's pattern, which this repo has closed several defects about.
