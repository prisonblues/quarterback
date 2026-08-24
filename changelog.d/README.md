# changelog.d — the whole contract, in four lines

**Write one file. Name no version. Touch nothing else. That is all of it.**

```
changelog.d/296.feat.md
```

`<issue>.<kind>.md`, where `<kind>` is one of `feat`, `fix`, `perf`, `refactor`, `docs`,
`test`, `chore`, `ci`, `revert` — this repo's own commit prefixes, so there is no second
vocabulary to learn. Use `+<short-slug>.<kind>.md` when there is genuinely no issue; a made-up
issue number in a filename reads as a real one forever after.

The file **is** the changelog entry, with a title on the first line:

```markdown
# a branch stops guessing which release it will be

What was broken or missing before this change — that is the part no diff recovers, and it is
what every entry in CHANGELOG.md leads with.

### A sub-heading, if the entry is long

`###` and below only. A `#` or `##` heading would split the release this fragment is folded
into, and the release tool would read the split as a second release.
```

`###` to `#####`, and write them at the depth that reads right *here* — assembly moves them.
When a release folds in several fragments, each fragment's title becomes the `###` and its
own headings drop one level under it, so the `###` above renders as `####`. That is why
`#####` is the floor: a `######` has no level left to drop to, and it is refused by name
rather than quietly flattened.

Underlining a line with `===` or `---` is a heading too — markdown's setext form — and it is
refused for the same reason a `##` is: setext has only levels one and two. A `---` meant as a
horizontal rule needs a blank line above it, which is what makes it one rather than an
underline for the paragraph it touches.

A `#` at the start of a line inside a fenced block, a code span or a four-space indented
block is a shell comment or a quoted sample, and nothing above applies to it. Write examples
fenced rather than indented, which is the convention the rest of this repo's markdown tooling
assumes.

## Name no version

Not `v3.13`, and not `vNEXT` either — the placeholder is retired and a fragment carrying it is
refused. There is no number to name: the release is numbered on `main`, after the merge, by
`scripts/release.py`, against the commit that actually exists.

## Do not open CHANGELOG.md or the README's release list

Those are **output**. `scripts/release.py run` writes them and nothing else does, and a branch
that edits either is refused — by `harness/githooks/pre-push` at the moment you would go wrong,
and by the `generated release files are output` CI job on the pull request.

The refusal is not a formality. Every branch that shipped anything used to edit the same lines
at the top of the same file, so N such branches in flight was N-choose-2 conflicts **by
construction** — over nothing, since both entries are right and both belong, and git cannot
know that two insertions at one offset are independent. On 2026-08-23 six pull requests were
open: the three that had written a release entry were all `CONFLICTING`, the three that had not
were all `MERGEABLE`. PR #398 landed both ways and settles it — unmergeable with the entry,
zero conflicts without it, same branch, same work, same base (#122).

## Something checks that you wrote one

```bash
scripts/changelog_fragments.py check                          # do the fragments parse?
scripts/changelog_fragments.py required --onto origin/main --branch HEAD
```

The `a change that ships carries a release note` CI job runs `required` on every pull request.
A branch that changes something that ships and carries no fragment is refused — because nothing
else could notice. `frozen` guards the entries that exist and `guard` refuses a branch that
touches them, so to both of them an entry nobody wrote is the same shape as a correct one,
which is how #363 landed with this directory holding only the file you are reading (#365).

A branch confined to documentation or tests passes in silence: `changelog.d/`, `CHANGELOG.md`,
any `README.md`, and anything under a `tests/` directory or named like a test are exempt.
Everything else ships — `.github/` and `harness/commands/*.md` included.

Genuinely nothing to tell a reader? Say so on a commit of the branch, where a reviewer sees it:

```
Changelog-Exempt: a comment typo, no behaviour changed
```

## What happens to it afterwards

Nothing you have to do. A fragment that sits here unassembled is not lost and is not an error —
the next release sweeps it in, which is what a release IS: everything since the last one. Six
fragments from six merges become one release, not six.

Cutting one is a deliberate act, once per batch, by whoever decides the batch is done — the
**Cut a release** workflow (`Actions → Cut a release`), or `scripts/release.py run --title "…"`
on `main` at a keyboard. It assembles every fragment here, derives the number, writes
`CHANGELOG.md` and the README's list, bumps the served version if the release touched `app/` or
`migrations/`, deletes the fragments it consumed, commits and tags. `scripts/release.py preview`
says what it would do and changes nothing; it is safe to run from anywhere, including a branch.
