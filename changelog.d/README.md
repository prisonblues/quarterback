# changelog.d — one file per change, so the CHANGELOG stops conflicting

Every branch that ships something used to edit the same lines at the top of
[`../CHANGELOG.md`](../CHANGELOG.md), so every pair of concurrent branches conflicted there.
That conflict is not a disagreement about anything: both entries are right and both belong,
and git cannot know that two insertions at one offset are independent.

Write a **fragment** instead. One file, named after your issue, that no other branch will
ever open:

```
changelog.d/296.feat.md
```

`<issue>.<kind>.md`, where `<kind>` is one of `feat`, `fix`, `perf`, `refactor`, `docs`,
`test`, `chore`, `ci`, `revert` — this repo's own commit prefixes, so there is no second
vocabulary to learn. Use `+<short-slug>.<kind>.md` when there is genuinely no issue; a made-up
issue number in a filename reads as a real one forever after.

The file is the CHANGELOG entry, with a title on the first line:

```markdown
# a branch stops guessing which release it will be

What was broken or missing before this change — that is the part no diff recovers, and it is
what every entry in CHANGELOG.md leads with.

### A sub-heading, if the entry is long

`###` and below only. A `#` or `##` heading would split the release this fragment is folded
into, and `release_stamp.py` would read the split as a second release.
```

**Name no version.** Not `v2.67`, not `vNEXT`. That is the whole reason two branches can each
write a fragment without racing for a number — the number is decided at land time, by
`release_stamp.py`, against the ref you are merging into.

## At land time

```bash
scripts/changelog_fragments.py check                     # do the fragments parse?
scripts/changelog_fragments.py assemble --title "<what this release does>"
scripts/release_stamp.py apply --onto origin/main        # vNEXT -> the next free number
```

`assemble` folds every fragment present into one `## vNEXT — <title>` entry at the top of
`CHANGELOG.md`, adds the matching `- **vNEXT** — …` bullet to the README's release list, and
deletes the fragments it consumed. With exactly one fragment, `--title` is optional: the
fragment's own title becomes the release's.

A fragment that lands unassembled is not lost and is not an error — the next `assemble` sweeps
it into that release's entry, which is what a release IS: everything since the last one.

Hand-writing `## vNEXT — <title>` in `CHANGELOG.md` still works and is still what that file's
convention paragraph describes. Fragments are how you avoid the conflict, not a new
requirement.
