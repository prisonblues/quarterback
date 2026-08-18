"""The `--ask` path: one question to the seats, no diff, no judge, no cycle.

Split out of `panel.py` (#129). A MOVE, not a rewrite.

Kept apart from the review path deliberately: an ask is a point of order, not a
second reading, and #79's whole argument is that the two must not be able to
drift into each other. `run_seat` is shared and lives in panel_seats, so one
implementation runs a seat for both.
"""

from __future__ import annotations

from panel_core import *            # noqa: F401,F403
import panel_core                   # noqa: F401
from panel_seats import *           # noqa: F401,F403
import panel_seats                  # noqa: F401
from panel_scope import *        # noqa: F401,F403  — re-exported for callers
import panel_scope               # noqa: F401

# ----------------------------------------------------------------------------- ask

#: `path`, `path:12`, `path:3500-3560`. Anchored, so a colon inside a path
#: (`odd:dir/x.py`) is a path and not a malformed range. Digits are bounded
#: because `int()` REFUSES a string of more than 4,300 digits (CPython's
#: integer-from-string limit) with a ValueError — a `--context x:9999…` long
#: enough to trip it would have crashed the command rather than been reported as
#: the nonsense it is. Nine digits is past any file anyone will read.
_RANGE = re.compile(r"^(\d{1,9})(?:-(\d{1,9}))?$")

#: The most one `--context` file will be READ from disk, whatever the char budget
#: then does with it. A separate ceiling from `ask_max_context_chars` because it
#: bounds a different cost: the budget bounds what the seats are SENT (and so
#: what is paid for), this bounds what is materialised in memory to slice a range
#: out of. A source file this big is not context for a premise either way, and it
#: is said rather than silently cut, so a stale spec against a generated file
#: does not look like a file that was read.
ASK_CONTEXT_FILE_MAX_BYTES = 4_000_000

#: Directories an ask will not read out of, however contained they are.
#: Containment answers "is this the repo under review?" and nothing else — and
#: the repo under review is precisely where the credentials are. `.git/config`
#: carries a personal access token in the remote URL on every https clone that
#: was authenticated once, and `.git/` holds every blob the working tree no
#: longer does, so a secret deleted a year ago is still readable through it.
ASK_SECRET_DIRS = frozenset({".git"})

#: Files that are nothing but secrets, by the names they are always given. Short
#: and exact on purpose: this is a denylist, not a secret scanner, and it is not
#: claimed to be one. It closes the routes an agent composing a `--context`
#: actually types, and every refusal is a stated :class:`ContextProblem`, so a
#: false positive costs one visible sentence and a miss costs no more than the
#: containment check alone already did.
ASK_SECRET_FILES = frozenset({".env", ".envrc", ".npmrc", ".netrc", ".pgpass",
                              ".pypirc", "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519"})

#: Extensions that are key material whatever the file is called.
ASK_SECRET_SUFFIXES = (".pem", ".key", ".p12", ".pfx")


class AskContext(NamedTuple):
    """One `--context` argument, resolved and read."""

    spec: str
    #: Repo-relative, resolved — what the report and the payload name it by.
    path: str
    first: int | None
    last: int | None
    text: str


class ContextProblem(NamedTuple):
    """A `--context` spec that did not become context, and why.

    The spec is kept BESIDE the sentence rather than only inside it, because
    "was this verdict reached with all the context the asker intended?" is a
    question the payload has to be able to answer without string-matching prose —
    and that distinction (an answer from missing material, versus an answer from
    unclear material) is the whole reason this feature exists."""

    spec: str
    problem: str


def _readable_file(path: Path) -> bool:
    """Is `path` a file right now — a question asked only to DISAMBIGUATE a spec,
    never to decide a read. Every containment check still runs afterwards."""
    try:
        return path.is_file()
    except OSError:
        return False


def _context_spec(spec: str, root: Path | None = None) -> tuple[str, int | None, int | None,
                                                                str | None]:
    """Split `path[:first[-last]]` into (path, first, last, problem). A bare
    `path:12` is the single line 12.

    **An existing file wins over a line range.** `--context config:2024` names the
    file `config:2024` when that file is there, and line 2024 of `config` when it
    is not. Without that test the range reading was unconditional, so a file whose
    own name ends in `:digits` could never be selected — and, worse, a repo
    holding both `config` and `config:2024` silently read line 2024 of the wrong
    one. There is no escaping syntax (`./notes:12` does not help), so the
    filesystem is the tie-breaker.

    A tail that is not a range, after a path that IS a file, is a bad RANGE and
    said so: `sub/a.py:abc` used to be reported as `sub/a.py:abc` not being a
    file, which is accurate and points at the wrong half of what was typed."""
    head, sep, tail = spec.rpartition(":")
    if not sep:
        return spec, None, None, None
    if root is not None and _readable_file(root / spec):
        return spec, None, None, None
    m = _RANGE.match(tail)
    if not m:
        if root is not None and head and _readable_file(root / head):
            return spec, None, None, (f"`--context {spec}`: {tail!r} is not a line range "
                                      "— expected N or N-M, counting from 1")
        return spec, None, None, None
    first = int(m.group(1))
    return head, first, int(m.group(2)) if m.group(2) else first, None


def _read_confined(root: Path, resolved: Path, limit: int) -> bytes:
    """Read at most `limit` + 1 bytes of `resolved` by walking DOWN from a
    descriptor on `root`, refusing a symlink at every step — the ROOT's own open
    included.

    The containment test in :func:`read_context` states the rule; this enforces
    it. Resolving a path and then opening it by that path are two traversals of
    the same string, and between them any component can become a symlink out of
    the repo — so the check would pass and the read would leave. Opening each
    component `O_NOFOLLOW` relative to the descriptor of the one above it never
    re-traverses anything: the file read is the file checked, or the open fails.

    It narrows nothing a caller can reach by typing. `resolved` is symlink-free
    by construction — `Path.resolve` followed every link before the containment
    test — so a spec naming a link inside the repo still reads its target, and the
    walk sees only real directories. `O_NOFOLLOW` firing here means a component
    turned into a symlink AFTER it was checked, which is the race and nothing
    else, and the caller is told so in those words.

    The ROOT is opened `O_NOFOLLOW` too, and it is the step that used not to be:
    every component below it was anchored to a descriptor while the first was
    still opened by pathname, so a repo root (or an ancestor of it) replaced
    between `resolve()` and this call redirected the whole walk out of the tree
    that was checked. `root` is itself resolved by the caller, so its last
    component is not a symlink and the flag narrows nothing reachable by typing —
    it closes the same race one step higher up.

    Bytes, not text, and bounded: what is on disk decides whether this is context
    at all (see :func:`read_context`, which refuses what does not decode), and
    `errors="replace"` would have turned a PNG into a wall of U+FFFD that reads
    as a successful read. `limit` + 1 so the caller can tell "exactly `limit`"
    from "more than `limit`" without a stat that would race the read."""
    parts = resolved.relative_to(root).parts
    if not parts:
        raise IsADirectoryError(errno.EISDIR, "the repo root is not a file", str(root))
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = nxt
        leaf = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd)
    finally:
        os.close(fd)
    # `leaf` is a raw descriptor until fdopen adopts it, and fdopen can fail
    # (MemoryError, a bad encoding name) — leaving it open forever in a caller
    # that is not a one-shot CLI. Closed by hand on exactly that path, and by the
    # file object on every other.
    fh = None
    try:
        fh = os.fdopen(leaf, "rb")
        return fh.read(limit + 1)
    finally:
        if fh is None:
            os.close(leaf)
        else:
            fh.close()


def _secret_context(rel: Path) -> str | None:
    """Why an ask will not read this repo-relative path, or None to read it.

    Containment is not the whole rule. `is_relative_to(root)` answers one
    question — "is this the repo under review?" — and the answer being yes is
    exactly the case where `--context .git/config` hands a PAT to four
    third-party CLIs, because a seat's reply is a place its prompt can come back
    out. `.env`, `.envrc`, `.npmrc` and committed key material are the same
    shape: readable, contained, and not context for a premise.

    Named components, not content: this refuses the files that ARE credentials,
    and says nothing about a token pasted into a source file. It is the cheap
    half of the rule and is documented as such (see `harness/loops/README.md`)."""
    parts = rel.parts
    if not parts:
        return None
    for part in parts:
        if part in ASK_SECRET_DIRS:
            return (f"it is inside `{part}/` — the repo's own object store, where "
                    "`config` carries the access token an https remote was cloned with")
    name = parts[-1]
    if name in ASK_SECRET_FILES or name.startswith(".env."):
        return f"`{name}` is a credentials file, not context for a premise"
    if rel.suffix in ASK_SECRET_SUFFIXES:
        return f"`{rel.suffix}` files are key material"
    return None


def read_context(root: Path, specs: list[str], problems: list[ContextProblem],
                 budget: int | None = None) -> list[AskContext]:
    """The files (or line ranges) an ask hands its seats, read from the repo under
    review.

    **Confined to that repo, and refused rather than clamped when it is not.**
    The path comes off a command line that an agent composes, so `--context
    ../../.ssh/id_ed25519` is a real shape: this is a prompt builder, and every
    seat's reply is a place its contents could come back out. Resolution follows
    symlinks before the containment test for the same reason `write_payload`
    opens `O_NOFOLLOW` — a link inside the repo is not a file inside the repo.

    **And containment is not the whole rule**, because the repo under review is
    where the credentials live: `--context .git/config` is contained, readable,
    and on an https remote it is a personal access token. So `.git/` and the
    usual secret filenames are refused too — see :func:`_secret_context`, which
    states each refusal as a problem naming why.

    A spec that cannot be read is a PROBLEM and never a silent omission. A seat
    given less context than the asker believes it has will answer `cannot tell`
    about a question the asker thinks it supplied the answer to, and the asker
    will read that as the code being unclear rather than as the file being
    missing.

    **`budget` bounds what the seats are sent, and the clamp is SAID.** An ask is
    the cheap check — that is its entire claim on anyone's attention — and
    `--context` had no ceiling at all: one spec naming a generated file, or this
    5,700-line module, built a multi-megabyte prompt and shipped a copy of it to
    every vendor on the panel. That is the #117 cost shape (one release-merge
    ≈ $750) reappearing on the path advertised as costing a minute. So the total
    is capped like a round's diff is, per the same rule and with the same
    reporting: the config wins as far as it can, and where it was cut the report
    says which spec and by how much."""
    root = root.resolve()
    out: list[AskContext] = []
    used = 0
    #: Exact repeats only. `--context a.py --context a.py` is one request typed
    #: twice — it read the file twice and formatted two identical sections into
    #: every seat's prompt, which is tokens spent on nothing in the one feature
    #: whose argument is that it is cheap. Overlapping ranges are left alone:
    #: `a.py:1-40` beside `a.py:20-30` is a legible thing to ask for.
    seen: set[str] = set()
    for spec in specs:
        if spec in seen:
            continue
        seen.add(spec)
        path, first, last, bad_range = _context_spec(spec, root)
        if bad_range:
            problems.append(ContextProblem(spec, bad_range))
            continue
        if not path.strip():
            problems.append(ContextProblem(spec, f"`--context {spec}` names no file"))
            continue
        try:
            resolved = (root / path).resolve()
        except (OSError, RuntimeError, ValueError) as e:
            # RuntimeError is a symlink loop, ValueError an embedded NUL or
            # another path the OS will not take — both reach here off a command
            # line an agent composed, and neither is worth a traceback that loses
            # the other seats' answers and the payload with them.
            problems.append(ContextProblem(
                spec, f"`--context {spec}` could not be resolved ({e.__class__.__name__})"))
            continue
        if not resolved.is_relative_to(root):
            problems.append(ContextProblem(
                spec, f"`--context {spec}` is outside {root} — an ask reads the "
                      "repo under review and nothing else"))
            continue
        secret = _secret_context(resolved.relative_to(root))
        if secret:
            problems.append(ContextProblem(
                spec, f"`--context {spec}` was refused: {secret}. An ask hands its "
                      "context to four third-party CLIs, so being inside the repo is "
                      "not on its own a reason to read a file"))
            continue
        if not resolved.is_file():
            # Saying where paths are anchored, because the plausible mistake is
            # an agent running this from `harness/loops/` and typing `panel.py`.
            problems.append(ContextProblem(
                spec, f"`--context {spec}` is not a file in {root} — `--context` "
                      "paths are relative to the repo root, not to the cwd"))
            continue
        try:
            data = _read_confined(root, resolved, ASK_CONTEXT_FILE_MAX_BYTES)
        except OSError as e:
            # ELOOP or ENOTDIR from the walk means the tree changed under it —
            # a directory that was checked is now a symlink (Linux answers a
            # no-follow open of one with ENOTDIR when O_DIRECTORY is also set,
            # which is why both codes read the same way here). Nothing a caller
            # can type reaches either: `resolve()` already settled the links, and
            # a non-directory component fails `is_file()` before the read.
            why = ("a component of the path changed after it was checked — it is "
                   "now a symlink, or no longer a directory"
                   if e.errno in (errno.ELOOP, errno.ENOTDIR) else e.__class__.__name__)
            problems.append(ContextProblem(spec, f"`--context {spec}` could not be read ({why})"))
            continue
        rel = str(resolved.relative_to(root))
        if len(data) > ASK_CONTEXT_FILE_MAX_BYTES:
            problems.append(ContextProblem(
                spec, f"`--context {spec}`: {rel} is over {ASK_CONTEXT_FILE_MAX_BYTES:,} "
                      "bytes — larger than an ask will read, and not context for a premise"))
            continue
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            # `errors="replace"` guaranteed this read SUCCEEDED, so `--context
            # assets/logo.png` became a wall of U+FFFD in every seat's prompt and
            # the asker was never told. A file that is not text is a stated
            # problem, like every other spec that did not become context.
            problems.append(ContextProblem(
                spec, f"`--context {spec}`: {rel} is not UTF-8 text — an ask hands its "
                      "seats source, not bytes"))
            continue
        if "\x00" in text:
            problems.append(ContextProblem(
                spec, f"`--context {spec}`: {rel} carries NUL bytes — an ask hands its "
                      "seats source, not bytes"))
            continue
        #: Newlines KEPT, so a range is a substring of the whole file rather than
        #: a re-joining of it. `path` and `path:1-N` over the same N lines used to
        #: differ by one character (and so by one in the payload's `chars`),
        #: which is nothing to a seat and confusing to anyone diffing two
        #: payloads. `len()` is unchanged — keepends splits at the same points.
        lines = text.splitlines(keepends=True)
        if first is None:
            kept = _budgeted(AskContext(spec, rel, None, None, text),
                             budget, used, problems)
            if kept is not None:
                out.append(kept)
                used += len(kept.text)
            continue
        if first < 1:
            problems.append(ContextProblem(spec, f"`--context {spec}`: lines are numbered from 1"))
            continue
        if first > len(lines):
            problems.append(ContextProblem(
                spec, f"`--context {spec}`: {rel} has {len(lines):,} lines"))
            continue
        if last < first:
            problems.append(ContextProblem(
                spec, f"`--context {spec}`: the range ends before it starts"))
            continue
        if last > len(lines):
            # Clamped and SAID, rather than clamped quietly: "3500-3560" against a
            # 3,510-line file is usually a stale line number, and a seat answering
            # from ten lines where the asker meant sixty is the failure this whole
            # feature exists to make cheap to notice.
            problems.append(ContextProblem(
                spec, f"`--context {spec}`: {rel} has {len(lines):,} lines — "
                      f"the seats got {first}-{len(lines)}"))
            last = len(lines)
        kept = _budgeted(AskContext(spec, rel, first, last, "".join(lines[first - 1:last])),
                         budget, used, problems)
        if kept is not None:
            out.append(kept)
            used += len(kept.text)
    return out


def _budgeted(ctx: AskContext, budget: int | None, used: int,
              problems: list[ContextProblem]) -> AskContext | None:
    """`ctx` cut to what is left of the ask's context budget, saying so when it
    cut anything — the same shape as the line-range clamp above it, and for the
    same reason: a seat answering from a fragment of what the asker meant to hand
    it is exactly the failure this feature exists to make cheap to notice.

    None when nothing at all was left, because a section with no content in it is
    a header telling the seats a file was supplied when it was not.

    **`last` moves with the text.** Left at the range that was ASKED for, a
    clamped `sub/a.py:1-200` still serialised `{"first": 1, "last": 200}` and
    rendered as `` `sub/a.py:1-200` `` while the seats saw ten lines — two
    records in one payload disagreeing about what was read, and the wide one is
    the one #77's board row ("was this verdict reached with all the context the
    asker intended?") would answer from."""
    if budget is None:
        return ctx
    left = budget - used
    whole = len(ctx.text)
    if left <= 0:
        problems.append(ContextProblem(
            ctx.spec, f"`--context {ctx.spec}`: the {budget:,}-char context budget "
                      "(`review_panel.ask_max_context_chars`) was spent by the specs "
                      "before it — the seats got none of this one"))
        return None
    if whole > left:
        problems.append(ContextProblem(
            ctx.spec, f"`--context {ctx.spec}`: the seats got {left:,} of {whole:,} chars "
                      f"— the {budget:,}-char context budget "
                      "(`review_panel.ask_max_context_chars`) stopped it"))
        cut = ctx.text[:left]
        # The last line the seats saw any of, counted from the text they got: a
        # cut landing mid-line still showed them that line's beginning, and
        # reporting the line before it would be the same lie in the other
        # direction. `first` is untouched — where the range starts is not what
        # the clamp changed. A whole-file spec has no range to correct.
        kept = cut.count("\n") + (0 if cut.endswith("\n") else 1)
        last = None if ctx.first is None else ctx.first + kept - 1
        return ctx._replace(text=cut, last=last)
    return ctx


def _context_chars(contexts: list[AskContext]) -> int:
    """How much CONTENT the seats are being handed — the quantity a budget is
    about, and the one :func:`_context_block` cuts. Not the length of the
    assembled block, which also counts delimiters that no clamp may touch."""
    return sum(len(c.text) for c in contexts)


#: What goes where the context would have been when there is none — and it is a
#: sentence rather than an empty string on purpose. See :func:`_context_block`.
NO_CONTEXT = ("\n--- CONTEXT ---\nNone was given. Answer from the premise's own terms, "
              "and where those do not settle it answer \"cannot tell\" — you have "
              "nothing to check it against and must not answer from memory.\n")


def _context_block(contexts: list[AskContext], budget: int | None = None) -> str:
    """The context as the seats see it, or the sentence that goes where it would
    have been.

    `budget` cuts the FILE CONTENT, section by section, and never the assembled
    block: slicing the finished string is how a clamp lands in the middle of a
    `--- CONTEXT: path ---` line and hands a seat a prompt whose last section has
    a half-written header on it. Every delimiter that is emitted is whole, and a
    section the budget leaves nothing for is dropped with its header rather than
    announced as a file that was supplied. A budget that leaves nothing of ANY
    of them falls through to the no-context sentence below, because that is what
    the seat is looking at.

    An ask with no context is legitimate — some premises are settled by their own
    terms — but a model handed a bare assertion and no material will reach for
    what it remembers about a library, or about this repo, and answer with real
    confidence from nothing. Saying out loud that it was given nothing is what
    makes `cannot tell` the available answer rather than a gap it has to invent
    its way across."""
    out = []
    left = budget
    for c in contexts:
        if left is not None and left <= 0:
            break
        text = c.text if left is None else c.text[:left]
        if left is not None:
            left -= len(text)
        where = f"{c.path}:{c.first}-{c.last}" if c.first else c.path
        out.append(f"\n--- CONTEXT: {where} ---\n{text}\n")
    # No sections is no sections, whether nothing was given or the budget left
    # nothing of what was. Returning "" for the second ended the prompt straight
    # after `--- PREMISE ---`: no material, and — worse — not the sentence above
    # either, so the one seat that can reach a zero budget (antigravity, whose
    # prompt travels in argv) was invited to answer from memory by a prompt that
    # never told it there was nothing to read.
    return "".join(out) or NO_CONTEXT


#: The ask's declared defaults — read from where they are declared and
#: documented, rather than spelled a second time here. See :func:`_ask_rule`.
ASK_DEFAULTS = harness_rules.DEFAULTS["review_panel"]


def _ask_rule(panel: dict, key: str, notes: list[str]) -> int:
    """A tally rule (or the context budget) as a positive int, saying so when the
    config is not one.

    Same discipline as :func:`diff_budget`: what cannot be the thing at all falls
    back and is reported, because silently honouring `ask_quorum: 0` would let a
    tally of nobody decide, and silently dropping it would leave you believing a
    rule you never got.

    The fallback comes from :data:`harness_rules.DEFAULTS`, which is where the
    default is declared and documented. Passing it in meant every call site
    spelled the number a second time, so a default changed in the file that
    documents it would go on being ignored by the file that applies it."""
    fallback = ASK_DEFAULTS[key]
    raw = panel.get(key)
    if raw is None or raw == "":
        return fallback
    n = None
    if not isinstance(raw, bool) and isinstance(raw, (int, str)):
        try:
            n = int(raw)
        except ValueError:
            n = None
    if n is None:
        notes.append(f"`{key}`={raw!r} is not a number — using {fallback}")
        return fallback
    if n < 1:
        notes.append(f"`{key}`={n} would let a tally of nobody decide — using {fallback}")
        return fallback
    return n


#: Environment that says an agent, rather than a person at a prompt, is running
#: this challenge — and which seat that agent is. Claude Code exports both of
#: these into every command it runs, so an agent that asks does not have to
#: remember to declare itself; forgetting is precisely how a premise gets
#: "confirmed" by the model that wrote it.
#:
#: **This is Claude Code's environment and only Claude Code's.** codex, pi and
#: `agy` export nothing this file can recognise as "seat X is running me", so an
#: agent driven by one of them gets no asker and the self-challenge guard does
#: not fire. That is not silent any more: :func:`ask` says in its notes that
#: nothing was detected, because a guard believed to be on and quietly off is
#: worse than one known to need `--asker`.
ASKER_ENV = {"CLAUDE_CODE_SESSION_ID": "claude", "CLAUDECODE": "claude"}


def asking_seat(explicit: str | None) -> str:
    """Which seat is asking, from `--asker` or from the environment.

    `--asker ''` is an explicit "nobody" — for a human at a terminal, where there
    is no agent and so no self-challenge to guard against. It is honoured, since
    the alternative is a person unable to turn off a rule that does not apply to
    them; it is the one hole in this, and it is one an agent has to type — and
    typing it while an agent's environment is present is now reported, so the
    hole cannot be used quietly."""
    if explicit is not None:
        return explicit.strip().lower()
    return detected_asker()


def detected_asker() -> str:
    """The seat :data:`ASKER_ENV` says is running this, or "" for nobody."""
    return next((seat for var, seat in ASKER_ENV.items() if os.environ.get(var)), "")


def ask(repo_name: str | None, premise: str, contexts: list[str] | None = None,
        reviewers: str | None = None, pr_number: int | None = None,
        json_out: bool = False, json_file: str = "", record: bool = True,
        asker: str | None = None) -> int:
    """Put one premise to the panel's seats and print what they said.

    No diff, no clustering, no judge. A round already votes on fixes — that is
    what a round IS — so the gap this fills is granularity and latency, not
    absence: three of PR #62's rounds each spent twenty minutes and thirty
    findings answering a yes/no question about one branch of `panel.py`.

    **Not a gate.** It exits 0 on every verdict, including `fails`. Making it a
    pass/fail step turns a one-minute question into a required wait, and a
    required wait gets skipped.

    `asker` is `None` for "work it out" and a seat name (or "") for a caller that
    already has. It used to default to "" — no asker, guard off — so every caller
    but `main()` silently lost the self-challenge rule, which is the one rule
    this feature is built around. **Whatever a caller passes is normalised and
    checked in here**, not at the command line: how a name is spelled must not be
    able to turn the guard off. See the comment at the point it arrives."""
    run_key = uuid.uuid4().hex
    cfg = load_repo_cfg(repo_name)
    repo_name = cfg.get("name") or repo_name
    rev, panel = cfg["reviewers"], cfg["review_panel"]
    selected, override_note = select_reviewers(rev, reviewers)
    # Progress and warnings go to stderr under --json, so stdout is the payload
    # and only the payload — the same rule the review path follows.
    chatter = sys.stderr if json_out else sys.stdout

    notes: list[str] = []
    if "sonarqube" in selected and reviewers:
        # Selectable for a review, and meaningless here: it is a scanner with a
        # rule set, not a correspondent. Said rather than silently dropped —
        # `--reviewers claude,sonarqube` otherwise looks like a two-seat ask.
        # Only when it was ASKED for, though: firing on the resolved set put a
        # permanent warning about a seat nobody tried to ask on every ask in
        # every repo that merely enables sonarqube for its reviews.
        notes.append("sonarqube cannot be asked a question — it scans code against a "
                     "rule set and has no reply to give. Not a seat on this ask.")
    seats = [n for n in LLM_REVIEWERS if n in selected]
    quorum = _ask_rule(panel, "ask_quorum", notes)
    threshold = _ask_rule(panel, "ask_threshold", notes)
    # The unsatisfiable configuration is a rule above the SEAT COUNT, not a
    # threshold above the quorum. Quorum is a minimum, not a maximum: with
    # `ask_quorum: 2`, `ask_threshold: 3` and four seats, three agreeing seats
    # reach the threshold and the ask resolves — so the warning that used to be
    # here fired on configurations that work, and named an invariant that is not
    # one. What can never be reached is a rule no number of seats can satisfy: a
    # one-seat repo with the default quorum of 2 returns `unchallenged` forever,
    # having run and paid for the seat first, and that reads as "nobody checked"
    # rather than as a config that could not have been met.
    unreachable = [f"`ask_{k}` ({v})" for k, v in (("quorum", quorum), ("threshold", threshold))
                   if v > len(seats)]
    if unreachable:
        notes.append(f"{' and '.join(unreachable)} above the {len(seats)} seat"
                     f"{'s' if len(seats) != 1 else ''} on this ask — no answer can reach "
                     "it, so this ask cannot come back as anything but unchallenged or "
                     "unresolved")

    # **The one place an asker enters this function** — detected, normalised and
    # checked here, not at the command line, because that is the only shape of
    # fix this guard has not already been through twice. It was first lost by
    # `ask()` not detecting an asker at all (every caller but `main()` ran with
    # the guard off); it was lost again by `ask()` taking whatever spelling a
    # caller passed, so `"Claude"` or `"claude "` compared a lower-cased seat key
    # against a string that could never equal it and a premise put to itself came
    # back `holds`. A third route in would be a third silent hole, so `main()`'s
    # strip/lower and its seat-name check live HERE and `main()` is one more
    # caller. Anything that is not a seat is refused rather than carried: a name
    # the tally cannot match is a guard that does not fire, and it says so.
    detected = detected_asker()
    if asker is None:
        asker = detected
        if not asker:
            notes.append("no asker was detected — the self-challenge guard is inactive for "
                         "this run. Only Claude Code's environment says which seat is "
                         "running a command; an agent on another vendor's CLI has to pass "
                         "`--asker <seat>` itself")
    else:
        given = str(asker)
        asker = asking_seat(given)
        if asker and asker not in LLM_REVIEWERS:
            notes.append(f"asker {given!r} is not one of {', '.join(LLM_REVIEWERS)} — the "
                         "self-challenge guard is inactive for this run, because a name no "
                         "seat answers to can never match a vote. Recorded as no asker")
            asker = ""
        elif not asker and detected:
            notes.append(f"`--asker ''` was passed while {detected}'s environment is "
                         "present — the self-challenge guard is off by request, so this "
                         "tally may rest entirely on the agent that wrote the premise")

    context_budget = _ask_rule(panel, "ask_max_context_chars", notes)
    context_problems: list[ContextProblem] = []
    read = read_context(Path(cfg["path"]), contexts or [], context_problems, context_budget)
    context = _context_block(read)

    print(f"\n[{repo_name}] premise challenge — {len(seats)} seat"
          f"{'s' if len(seats) != 1 else ''}", file=chatter)
    print(f"  {premise[:120]}\n", file=chatter)

    models = {n: rev.get(n, {}).get("model", SEAT_MODEL_DEFAULTS.get(n, ""))
              for n in LLM_REVIEWERS}
    efforts = {n: rev.get(n, {}).get("effort", "") for n in EFFORTS}

    def prompt_for(budget: int | None) -> str:
        # The budget cuts the file CONTENT inside the block, never the assembled
        # block — see _context_block. Slicing the finished string is how a clamp
        # lands halfway through a `--- CONTEXT: … ---` delimiter.
        return ASK_PROMPT.format(premise=premise,
                                 context=context if budget is None
                                 else _context_block(read, budget))

    # One prompt, shared: it is the same string for every seat, and building it
    # per seat made N copies of every context file to no end.
    base = prompt_for(None)
    prompts = dict.fromkeys(seats, base)

    answers: dict[str, SeatAnswer] = {}
    # `agy`'s prompt travels in argv and the kernel caps one element, whatever is
    # in it — a premise is small but a `--context` file need not be. Same clamp,
    # same report, as the diff gets on a round. The seat is `antigravity`
    # everywhere it is named; `agy` is only the command it runs (see CLI_BIN).
    if "antigravity" in prompts:
        whole = _context_chars(read)
        fitted = fit_argv_budget(prompt_for, whole)
        if fitted < whole:
            notes.append(f"antigravity gets {fitted:,} of {whole:,} context chars "
                         "— its prompt travels in argv and the kernel caps one element "
                         f"at {ARGV_PROMPT_MAX_BYTES:,} bytes")
            prompts["antigravity"] = prompt_for(fitted)
        # The fitting only ever takes CONTEXT out, and the premise and the
        # ASK_PROMPT template have no budget at all — so a long premise leaves a
        # prompt still over the ceiling with nothing left to cut, and
        # `fit_argv_budget` returning 0 is not the same claim as "it fits". Asked
        # of the RENDERED prompt rather than inferred from the reduction, because
        # the alternative is what used to happen: the oversized argv went to
        # execve, `agy` died there with an opaque error, and no note said why.
        # A stated skip is the panel's idiom for a seat that could not be run,
        # and it keeps the seat's absence in the tally instead of in a traceback.
        over = len(prompts["antigravity"].encode()) - ARGV_PROMPT_MAX_BYTES
        if over > 0:
            label = reviewer_label("antigravity", models["antigravity"],
                                   efforts.get("antigravity", ""))
            answers["antigravity"] = SeatAnswer(skip=(
                f"{label}: its prompt is {over:,} bytes over the "
                f"{ARGV_PROMPT_MAX_BYTES:,}-byte argv ceiling with no context left to cut "
                "— `agy` takes a prompt only as one argv element, and the premise alone "
                "does not fit in one"))

    # Only the seats that still need running. A seat the argv check above already
    # settled has its answer, and starting a CLI for a prompt known not to
    # survive exec would spend a turn to arrive at the same skip.
    to_run = [n for n in seats if n not in answers]
    if to_run:
        with ThreadPoolExecutor(max_workers=len(to_run)) as ex:
            tasks = {n: ex.submit(ask_llm, n, models[n], prompts[n], efforts.get(n, ""))
                     for n in to_run}
            for n, fut in tasks.items():
                try:
                    answers[n] = fut.result()
                except Exception as e:  # noqa: BLE001 - one seat never takes the ask down
                    # `run_seat` does filesystem work — a sandbox, temp dirs, an
                    # `os.open` — and ENOSPC or a permission error on any of it
                    # raises outside the err-string path. Re-raised here it took
                    # the whole ask with it: every other seat's finished answer
                    # discarded, no tally, no payload, no --json-file, and a
                    # traceback where the documented exit-0 report should be. The
                    # seat is recorded as not having answered, which is what
                    # happened, and the tally stays honest about it.
                    answers[n] = SeatAnswer(skip=f"{n}: raised {e.__class__.__name__} — {e}")

    tally = ask_tally(answers, quorum, threshold, asker)
    payload = {
        "kind": "ask",
        "repo": repo_name, "github": cfg["github"],
        # The PR this premise is being asked ON BEHALF of, when there is one.
        # Nothing is fetched for it: an ask reads the context it was handed, and
        # a PR number it never opened is a link, not a claim about the PR.
        "pr": pr_number,
        "premise": premise,
        "context": [{"spec": c.spec, "path": c.path, "first": c.first, "last": c.last,
                     "chars": len(c.text)} for c in read],
        # The specs that did NOT become context, machine-readably. "Was this
        # verdict reached with all the context the asker intended?" is the
        # question a later audit (and #77's board row) has to be able to answer,
        # and it could only be answered by string-matching English out of
        # `config_notes` — where these did not belong in the first place: a
        # missing file is not a repo whose configuration wants tuning.
        "context_problems": [{"spec": p.spec, "problem": p.problem} for p in context_problems],
        "asker": asker or None,
        "verdict": tally.verdict,
        "verdict_reason": tally.reason,
        "quorum": quorum,
        "threshold": threshold,
        "answered": tally.answered,
        "counts": tally.counts,
        "seats_selected": sorted(selected),
        "seats_override": override_note,
        # Usage FIRST, so a telemetry key that happens to collide with a primary
        # field (`model`, `verdict`, `reason`, `duration_ms`, …) cannot overwrite
        # what the seat actually answered. Still spread rather than nested,
        # matching the round: a seat whose usage could not be read contributes no
        # keys at all, so the board stores nulls and renders "not recorded"
        # instead of a zero it would average in as a free reviewer.
        "answers": {n: {**(a.usage or {}),
                        "verdict": a.verdict, "reason": a.reason, "gist": a.gist or None,
                        "skip": a.skip, "unreadable": a.unreadable, "absent": a.absent,
                        "model": models[n] or None, "effort": efforts.get(n) or None,
                        "duration_ms": a.duration_ms}
                    for n, a in sorted(answers.items())},
        "config_notes": notes,
        "run_key": run_key,
    }
    write_failed = write_payload(json_file, payload)
    # Not recorded when the local artefact could not be written. The run is about
    # to exit non-zero through `finish(write_failed)`, and a board row for a run
    # its caller was told had failed is two records that disagree about whether
    # this ask happened. (`run()` has the same shape on the review path and is
    # left alone here — it is not what this change is about.)
    if record and not write_failed:
        record_ask(payload)
    if json_out:
        print(json.dumps(payload, indent=2))
        return finish(write_failed)

    # Separated, because "demo#62" reads as one token. The round's heading spells
    # it `PR #<n>` (see `heading` in run()), and one spelling across both reports
    # is one less thing for a reader to parse.
    lines = [f"## Premise challenge — {repo_name}"
             + (f", PR #{pr_number}" if pr_number else ""), ""]
    lines.append(f"**Premise:** {premise}")
    if read:
        lines.append("**Context:** " + ", ".join(
            f"`{c.path}:{c.first}-{c.last}`" if c.first else f"`{c.path}`" for c in read))
    else:
        lines.append("**Context:** none given — the seats answered from the premise alone")
    # `fallback_label`, not `reviewer_label`: a seat that could not use its pins
    # answered on something else, and the Seats line is where a reader learns which
    # brain settled the premise (#215).
    lines.append("**Seats:** " + (", ".join(
        fallback_label(n,
                       "" if answers.get(n) and answers[n].model_unavailable else models[n],
                       "" if answers.get(n) and answers[n].effort_unsupported
                       else efforts.get(n, ""),
                       answers[n].model_unavailable if answers.get(n) else "",
                       answers[n].effort_unsupported if answers.get(n) else "")
        for n in seats) or "none"))
    if asker:
        # Only the seats on THIS ask have a vote to be the only one, so
        # `--reviewers codex --asker claude` gets the other sentence: the first
        # asserts something untrue of the run it is describing.
        lines.append(f"**Asked by:** {asker}" + (
            " — its own answer is one vote and cannot be the only one" if asker in seats
            else " — not a seat on this ask, so it has no vote here"))
    if override_note:
        lines.append(f"  - {override_note}")
    for note in notes:
        lines.append(f"  - ⚠️ config: {note}")
    # Kept apart from the config notes, and labelled for what they are: a reader
    # told that a missing file is a "config" problem goes looking for a key that
    # does not exist, and the remedy for a context that never got read is a
    # different one entirely.
    for problem in context_problems:
        lines.append(f"  - ⚠️ context: {problem.problem}")
    lines.append("")

    # One column per seat, whether or not it answered, because the absences are
    # the part a tally hides: "2 of 2 say it holds" over a four-seat panel is a
    # different sentence from the same words over a two-seat one.
    width = max((len(n) for n in seats), default=0)
    for name in seats:
        a = answers[name]
        if a.verdict:
            lines.append(f"    {name.ljust(width)}  {a.verdict.ljust(11)}"
                         + (f" — {a.reason}" if a.reason else ""))
        elif a.unreadable:
            lines.append(f"    {name.ljust(width)}  ⚠️ no verdict — its reply could not be "
                         "read as one, and is NOT counted as `cannot tell`"
                         + (f" (it said: {a.gist})" if a.gist else ""))
        else:
            lines.append(f"    {name.ljust(width)}  ⚠️ did not answer — {a.skip}")
    arrow = {"holds": "the premise HOLDS", "fails": "the premise FAILS",
             "unresolved": "UNRESOLVED", "unchallenged": "UNCHALLENGED"}[tally.verdict]
    lines.append(f"\n→ **{arrow}** — {tally.reason}")
    if tally.verdict == "unchallenged":
        lines.append("  _An unchallenged premise is not a confirmed one. Read this as "
                     "\"nobody checked\", which is where it started._")
    lines.append("\n_Not a gate: this is a point of order, and it decides nothing on its "
                 "own. It is one question to the seats — no diff was read and no judge "
                 "ruled, so it is evidence about the premise and not a review._")
    print("\n".join(lines))
    return finish(write_failed)


#: Everything this module offers, INCLUDING the underscore names — the suites
#: reach for several of them through `panel`, and a plain star import would drop
#: them silently. Generated from the module's own top level, so a helper added here
#: is exported without anyone remembering to list it.
__all__ = [
    "panel_core", "panel_seats", "_RANGE", "ASK_CONTEXT_FILE_MAX_BYTES",
    "ASK_SECRET_DIRS", "ASK_SECRET_FILES", "ASK_SECRET_SUFFIXES", "AskContext",
    "ContextProblem", "_readable_file", "_context_spec", "_read_confined",
    "_secret_context", "read_context", "_budgeted", "_context_chars",
    "NO_CONTEXT", "_context_block", "ASK_DEFAULTS", "_ask_rule",
    "ASKER_ENV", "asking_seat", "detected_asker", "ask",
]
