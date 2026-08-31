# every fetch failed silently on a repo with a stash and worktrees

The shared-stash guard refuses any non-zero write to `refs/stash`. `git pack-refs`
migrates loose refs into `packed-refs` by *creating* each one in the packed backend
— `0000.. -> <sha>` — which, in the fields a `reference-transaction` hook receives,
is indistinguishable from a stash push. So the guard refused it.

git runs `pack-refs` as an auto-maintenance task during `git fetch`, and a refusal
aborts the surrounding transaction. On any repo with a non-empty `refs/stash` and
linked worktrees — the configuration this harness creates on purpose — every fetch
then failed with `task 'pack-refs' failed` **while still exiting 0**. No
remote-tracking ref advanced, and nothing surfaced the breakage.

One repo sat two weeks behind its remote that way. That silently invalidates every
`git branch --merged` and "is this PR landed?" check run against it, including the
one `/tree-shake` uses to decide which worktrees are finished and safe to reap.

`<old>` cannot separate the two cases: the packed backend reports a create either
way. The ref's *current value* can — mid-migration `refs/stash` already resolves to
the value being written, so nothing is being added to the stack, whereas a real
push always moves it somewhere new. The check costs a subprocess only after the
existing cheap tests have matched `refs/stash`, so the common path is unchanged.

Measured against real git rather than assumed. The first attempt keyed on
`old == new`, which never fires — `pack-refs` sends a create followed by a delete,
not a no-op rewrite.
