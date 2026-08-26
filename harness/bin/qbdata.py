"""Fleet data and the formatting both dashboards share.

One source of truth for "what is the board saying", so the snapshot renderer
(qb-dash) and the clickable one (qb-dash-tui) cannot drift on a fix.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import socket
import ssl
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone

REPO = "prisonblues/quarterback"          # the fallback, not the answer
REPO_URL = f"https://github.com/{REPO}"


def repo_slug(path: str = ".") -> str | None:
    """'owner/name' from a checkout's origin remote, or None.

    Handles scp syntax, https://, ssh:// and a .git suffix — the same shapes the
    MCP server's own repo_slug() has always parsed, and the same lesson as #148:
    a repo spelled by its caller and a repo read from git are two values that
    disagree silently.
    """
    try:
        got = subprocess.run(["git", "-C", path, "remote", "get-url", "origin"],
                             capture_output=True, text=True, timeout=10)
    except Exception:                             # noqa: BLE001
        return None
    if got.returncode != 0:
        return None
    url = got.stdout.strip().removesuffix(".git")
    if not url:
        return None
    tail = re.sub(r"^(git@|ssh://|https?://)[^/:]+[:/]", "", url)
    parts = [p for p in tail.split("/") if p]
    return "/".join(parts[-2:]) if len(parts) >= 2 else None


_repos: list[str] | None = None


def resolve_repos() -> list[str]:
    """Which repositories this dashboard reports on, most relevant first.

    QB_DASH_REPOS names them outright, comma separated, which is how one screen
    watches a fleet that works in three. Otherwise it is the repo of the
    directory the dashboard was started in — hardcoding one meant a screen
    pointed at nix-fleet reported quarterback's pull requests and said nothing
    about having done so.

    Worked out ONCE per process. Neither the environment nor the cwd's origin
    changes under a running dashboard, and the fallback shells out to git — which
    a per-row caller (the plan panel asks "is this one of mine?" of every item it
    draws) would turn into a subprocess per row per redraw.
    """
    global _repos
    if _repos is None:
        named = os.environ.get("QB_DASH_REPOS", "").strip()
        _repos = ([r.strip() for r in named.split(",") if r.strip()] if named
                  else [repo_slug(os.environ.get("QB_DASH_REPO") or os.getcwd()) or REPO])
    return _repos


def short_repo(repo: str) -> str:
    """'prisonblues/quarterback' → 'quarterback'. The owner never distinguishes."""
    return repo.split("/", 1)[-1]


def set_repos(repos: list[str]) -> None:
    """Pin which repositories this process watches, overriding the environment.

    The dashboards take ``--repo`` now, and half of what reads the answer reaches
    :func:`resolve_repos` for itself — ``plan_repo``, ``sort_plan``, ``fetch_prs``
    — rather than taking a list as an argument. Priming the cache is what makes
    one flag reach all of them; threading it through every call site would leave
    whichever one was missed silently watching the cwd, which is the shape of
    #176 all over again.
    """
    global _repos
    cleaned = [r.strip() for r in repos if r and r.strip()]
    _repos = cleaned or None


#: An owner or a repository name, as GitHub spells one. Anything else in an
#: ``owner/name`` argument — a space, a quote, a shell metacharacter — is a
#: malformed slug rather than a repository, and it would reach `gh` as one.
#: At least one character that is not a dot, so ``owner/..`` is not a repository
#: name that merely looks odd: it is a path, and `gh` would be asked about it.
_SLUG_PART = re.compile(r"(?=[^.])[A-Za-z0-9._-]+")


def repo_target(value: str) -> tuple[str, str | None]:
    """A ``--repo`` argument as ``(slug, the checkout it names or None)``.

    A directory is asked for its origin, which is how "the project this screen is
    set up for" is spelled when you are standing in it — and the directory comes
    back beside the slug because the clickable renderer LAUNCHES work in it, so
    ``--repo ~/src/nix-fleet`` has to move the cwd of what the ⚒ starts and not
    only the rows the panels draw. A slug names a repository this process may have
    no checkout of, which is why the second half of the answer can be None.

    SHAPE FIRST, the filesystem second, and the whole rule in three lines:
    ``owner/name`` is a **slug**; anything with another separator, a leading ``.``
    or ``/``, or a single segment naming a directory is a **checkout**; anything
    else is refused. Deciding on the shape is what stops the answer depending on
    the cwd — ``--repo prisonblues/quarterback`` run from a ``~/src/<owner>/<repo>``
    tree used to match ``os.path.isdir`` and die as "not a git checkout" — and the
    cost is one keystroke: a two-segment relative path needs its ``./``, because
    ``owner/name`` means a repository everywhere else in the fleet.

    A bare name is REFUSED rather than completed: `gh` needs an owner, the fleet
    works in repos whose owner is not this one's, and inventing one aims the PR
    panel — and the ⚒ that starts work off it — at somebody else's repository of
    the same name. Unless it names a directory, which is not a guess about an
    owner: ``--repo nix-fleet`` beside a checkout of that name is unambiguous, and
    that spelling worked before the shape rule arrived.

    THE PATH COMES BACK ABSOLUTE. It is handed to tmux as a ``-c`` start directory
    for the pane the ⚒ and ⚖ open, and tmux resolves a relative ``-c`` against the
    tmux SERVER's cwd — where the server was first started, months ago — not this
    process's. `self.repo` used to be `os.getcwd()` and so absolute by
    construction; ``--repo ./nix-fleet`` would have quietly launched work in
    whatever that name means to the server, while the guard beside it resolved the
    same relative path correctly in-process and reported the right repo, hiding it.
    """
    raw = (value or "").strip() or "."
    # `~` is expanded here because the help text and the README advertise
    # `--repo ~/src/nix-fleet`, and only an interactive shell expands it: quoted,
    # built into a QB_SEATS_DASH command string or sent through `tmux send-keys`,
    # the tilde arrives intact and used to be reported as a bad slug.
    path = os.path.expanduser(raw).rstrip("/") or "/"
    parts = [part.strip() for part in path.split("/")]
    if len(parts) == 2 and not path.startswith(".") and all(
            _SLUG_PART.fullmatch(part) for part in parts):
        return "/".join(parts), None
    # A single segment is a checkout only if it IS one — and a value with a `/`
    # that reached here is not slug-shaped, so it is a path or it is nothing. Said
    # from the shape rather than by asking git: `git -C 'owner/na@me'` costs a
    # subprocess and answers "not a git checkout with an origin remote", which
    # misdiagnoses a bad slug as a missing remote.
    if path.startswith((".", "/")) or os.path.isdir(path):
        slug = repo_slug(path)
        if not slug:
            raise ValueError(f"{path}: not a git checkout with an origin remote")
        return slug, os.path.abspath(path)
    if "/" in path:
        raise ValueError(f"{raw!r}: not an owner/name slug — a repository name may "
                         "hold only letters, digits, dots, hyphens and underscores; "
                         "write ./" + raw.lstrip("/") + " if you meant the directory")
    raise ValueError(f"{raw!r}: neither a checkout nor an owner/name slug — a bare "
                     "name needs its owner, or a directory of that name here")


def repo_arg(value: str) -> str:
    """Just the slug of a ``--repo`` argument, for a renderer that launches nothing."""
    return repo_target(value)[0]


_REPO_COLOURS = ["cyan", "magenta", "green", "yellow", "blue", "red"]
_repo_colour: dict[str, str] = {}


def repo_colour(name: str) -> str:
    """A stable colour per repo, shared by every panel and both renderers.

    The colour is the fastest way to see that a PR and the agent working it are
    in the same place, so it has to mean the same thing in FLEET as in OPEN PRs.
    """
    key = short_repo(name or "")
    if key not in _repo_colour:
        _repo_colour[key] = _REPO_COLOURS[len(_repo_colour) % len(_REPO_COLOURS)]
    return _repo_colour[key]


# ---- scope: which project's rows this dashboard is about ---------------------
#
# Every board-derived panel is fleet-wide by construction — FLEET is every live
# agent, CLAIMED every claim, PLANS every repo's list — and a screen is built for
# ONE project. So most rows are somebody else's, and the repo cell is then the
# same word, eleven columns wide, on every line: a 78-column pane spending its
# scarcest thing on a fact the header can state once (#261).
#
# One object holds both halves of the decision because they have to agree. What is
# kept, and whether the column is worth keeping, are the same question asked twice:
# a column dropped from a table whose rows were NOT narrowed shows nothing anywhere,
# and rows narrowed with the column left in place is the waste this exists to end.

#: Opens the dashboard wide instead of on the screen's own repos. Any other value
#: — including nothing — is the narrow default, which is what a seat screen wants
#: every time but the one time it is looking for a peer somewhere else.
SCOPE_ENV = "QB_DASH_SCOPE"
SCOPE_WIDE = ("all", "fleet", "wide")


def repo_name(value: str | None) -> str | None:
    """A repo in the one form these panels can be compared in, or None.

    THREE spellings reach this dashboard for one repository. A lease reports a
    bare ``quarterback``, because what it saw was the checkout's directory; the
    plan and `gh` both report ``prisonblues/quarterback``; and a hand-written plan
    item reports whichever the human typed. Folded to the bare name, lowercased,
    so what gets compared is repositories rather than spellings — comparing the
    slugs would put a seat's own FLEET row out of its own scope.
    """
    return short_repo((value or "").strip()).lower() or None


def claim_repo(key: str | None, plan: list[dict] | None = None) -> str | None:
    """Which repo a claim is against, or None when its key cannot say.

    Three key shapes are in use and only two of them carry a repo: ``owner/repo#12``
    (an issue), ``owner/repo:2.40`` (a release number), and ``plan:<uuid>`` — which
    names an item and not a repo, so the plan is consulted when the caller has it
    and the claim is left unattributed when it does not.

    None means "cannot say", and every caller treats that as in scope. A claim
    whose repo is unknown is not evidence that it belongs to another project, and
    hiding it drops the one row that says somebody already holds the work you were
    about to pick up.
    """
    key = (key or "").strip()
    if not key:
        return None
    if key.startswith("plan:"):
        wanted = key.split(":", 1)[1]
        for item in plan or []:
            if item.get("item_id") == wanted:
                return item.get("repo") or None
        return None
    head = key.split("#", 1)[0].split(":", 1)[0]
    # A head with no owner is not a repo we can name. `gh` and the plan both spell
    # a claim key with its owner, so a bare word here is some other namespace's
    # key, and reading it as a repo would scope rows against a word that is not one.
    return head if "/" in head else None


class Scope:
    """Which rows a dashboard is about, and whether to spend a column saying so."""

    def __init__(self, repos: list[str], on: bool = True) -> None:
        self.repos = list(repos)
        self.on = bool(on)
        #: The bare names, for the header and for comparing against a board row
        #: that reports no owner (a lease reports the checkout's directory).
        self.names = {n for n in (repo_name(r) for r in self.repos) if n}
        #: The full slugs of the watched repos that name an owner, and the bare
        #: names of the ones that do not.
        self.slugs = {r.strip().lower() for r in self.repos if "/" in r.strip()}
        #: ...and the bare names of the ones that do not, MINUS any that a slug
        #: already accounts for. `QB_DASH_REPOS=quarterback,prisonblues/quarterback`
        #: is one repository named twice — which `keeps` has always treated as one —
        #: and counting both spellings put the eleven-column cell back on a
        #: single-project pane, which is the waste this whole thing removes.
        named = {short_repo(slug) for slug in self.slugs}
        self.bare = {n for n in (repo_name(r) for r in self.repos
                                 if "/" not in r.strip()) if n and n not in named}
        #: ONE ENTRY PER REPOSITORY, in the strongest form each was named in. The
        #: bare names alone folded a fork and its upstream into one — `column` then
        #: dropped the only cell that told them apart and `keeps` accepted both
        #: repos' rows — because `len(names) == 1` had stopped meaning "exactly one
        #: repository", which is what both of them rely on it meaning.
        self.keys = self.slugs | self.bare

    @property
    def column(self) -> bool:
        """Is the repo cell worth its eleven columns?

        Not when the rows have been narrowed to ONE repository: every cell then
        carries the same word, and the header says it once for the whole pane. Yes
        for the wide view — telling repos apart is the entire reason to widen — and
        yes for a screen watching two, where the cell still distinguishes rows.
        """
        return not self.on or len(self.keys) != 1

    def keeps(self, repo: str | None) -> bool:
        """Does a row in ``repo`` belong on this pane?

        A row whose repo nothing could name STAYS. No repo is not evidence of
        another repo, and dropping it hides a live agent working outside a checkout
        — or a claim whose plan item this process has not fetched — on the strength
        of a missing field. The narrow view is a way to read the fleet, not a claim
        to have accounted for all of it.

        Compared on the WHOLE slug when both sides name an owner: ``myuser/quarterback``
        and ``prisonblues/quarterback`` are two repositories, and a fork whose rows
        were kept as the upstream's is the one narrowing that reads as a fact. A row
        that gives only a bare name can only be compared as one — and, per the rule
        above, a bare name that matches is kept rather than guessed at.
        """
        if not self.on or not self.names:
            return True
        name = repo_name(repo)
        if name is None:
            return True
        full = (repo or "").strip().lower()
        if "/" in full:
            return full in self.slugs or name in self.bare
        return name in self.names

    def label(self) -> str:
        """What the header says this pane is showing, in one phrase."""
        if not (self.on and self.names):
            return "all repos"
        # The bare names, which is all a header needs — unless two of them are the
        # same word, when the owner is the only thing that says which pane this is.
        return ", ".join(sorted(self.names if len(self.names) == len(self.keys)
                                else self.keys))

    def toggled(self) -> Scope:
        """The same watch list, the other way round — what the `s` key switches to.

        Named for what it does. It was `widened()`, which promised one direction and
        delivered two: called on a wide scope it narrows, so the one line in
        `action_toggle_scope` was also the line that took the pane back.
        """
        return Scope(self.repos, not self.on)


def resolve_scope(repos: list[str] | None = None, on: bool | None = None) -> Scope:
    """The scope a dashboard starts in: narrow, unless told otherwise.

    Narrow by default because that is what a screen is for. ``QB_DASH_SCOPE=all``
    opens wide for a session that is watching the fleet rather than working in it;
    the repos are the ones :func:`resolve_repos` already worked out, so one
    ``--repo`` or ``QB_DASH_REPOS`` aims the filter and the `gh` calls together.
    """
    if on is None:
        on = os.environ.get(SCOPE_ENV, "").strip().lower() not in SCOPE_WIDE
    return Scope(resolve_repos() if repos is None else repos, on)


def in_scope(rows: list[dict], scope: Scope | None,
             repo_of=None) -> tuple[list[dict], int]:
    """The rows a scope keeps, and HOW MANY IT HID.

    The count comes back rather than being dropped because a filtered panel that
    does not say it filtered is a panel quietly lying about the fleet — the same
    defect as a hardcoded repo (#176) one level up. Every caller puts it in the
    panel title, so "nobody else is working" and "nobody else is working *here*"
    can never read the same.
    """
    if scope is None:
        return list(rows), 0
    getter = repo_of or (lambda row: row.get("repo"))
    kept = [row for row in rows if scope.keeps(getter(row))]
    return kept, len(rows) - len(kept)


def elsewhere(hidden: int) -> str:
    """What a narrowed panel adds to its own title, or nothing.

    Every panel that filters says so, because a panel that filtered silently is a
    panel lying about the fleet: "nothing claimed" and "nothing claimed HERE" are
    different facts, and the second is the one the reader is being shown.

    Here rather than in a renderer because both of them format it and they have to
    agree — the two copies this replaces had already drifted by a word.
    """
    return f" · {hidden} elsewhere" if hidden else ""


def scope_mark(scope: Scope | None, repo: str | None) -> str:
    """The prefix a row the scope could not attribute wears, or nothing.

    :meth:`Scope.keeps` deliberately keeps a row whose repo nothing could name —
    an agent outside any checkout, a fleet-wide plan item, a claim whose item this
    process has not fetched. The repo cell was the only thing that ever SAID so
    (``—``, ``fleet``), and the narrow view is exactly the view that drops it: with
    the cell gone and no mark, an agent working nowhere reads as one working here,
    which is the panel-that-filtered-silently one level down.

    A prefix rather than a cell of its own because a table's columns are fixed for
    every row: what needs marking is one row in ten, and the pane cannot spend a
    column on it. Only where the cell is gone — the wide view has the repo itself,
    which says more than a mark can.
    """
    if scope is None or scope.column or repo_name(repo) is not None:
        return ""
    return "? "


def ago(stamp: str | None) -> str:
    """'4m', '2h10m' — how long since an ISO timestamp. '' if unparseable."""
    if not stamp:
        return ""
    try:
        then = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return ""
    secs = int((datetime.now(timezone.utc) - then).total_seconds())
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m"
    return f"{secs // 3600}h{(secs % 3600) // 60:02d}m"


def until(stamp: str | None) -> str:
    """'12m' left on a lease/claim, '—' once it is in the past."""
    if not stamp:
        return "—"
    try:
        then = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return "—"
    secs = int((then - datetime.now(timezone.utc)).total_seconds())
    if secs <= 0:
        return "—"
    if secs < 3600:
        return f"{secs // 60}m"
    return f"{secs // 3600}h{(secs % 3600) // 60:02d}m"


def minutes_left(stamp: str | None) -> int | None:
    """Whole minutes remaining, for deciding what to colour red."""
    if not stamp:
        return None
    try:
        then = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    return int((then - datetime.now(timezone.utc)).total_seconds() // 60)


#: How long a `working` may stand before a reader calls it stalled. Tool calls
#: refresh it, so the gap this has to clear is the longest a session legitimately
#: goes without one — a long think, a slow build, a big edit in a single pass.
#:
#: It MUST agree with the same constant in the footer (nix-fleet's
#: home/claude/scripts/statusline.sh, STALL_AFTER). Two readers of one beacon
#: disagreeing about when it goes stale is worse than either threshold being
#: wrong: the dashboard and the pane's own bar would describe the same seat
#: differently, and there is no way to tell from the outside which one to believe.
STALL_AFTER = 480


def agent_state(agent: dict) -> tuple[str, str]:
    """(word, style) for what a live agent is doing — '' when it never said.

    The board stores what the holder reported; `stalled` is concluded HERE, from
    the age of that report, and is the reason `state_at` travels with `state`.
    A pane that said `working` and then went quiet is the failure this whole
    field exists to surface: it looks identical to a busy one from the outside.

    `waiting` and `input` do not go stale. A pane that has been waiting on a
    human since lunch is still waiting on that human — ageing it into `stalled`
    would hide the one state somebody is actually scanning for.
    """
    state = agent.get("state") or ""
    if not state:
        return "", "grey50"
    if state == "working":
        try:
            then = datetime.fromisoformat((agent.get("state_at") or "").replace("Z", "+00:00"))
        except ValueError:
            return "working", "grey50"
        if (datetime.now(timezone.utc) - then).total_seconds() >= STALL_AFTER:
            return "stalled", "bold red"
        return "working", "grey50"
    return {"waiting": ("waiting", "bold yellow"),
            "input": ("input", "bold magenta")}.get(state, (state, "grey50"))


#: What a lease that never reported a workflow stage renders as.
#:
#: The word is **unreported** — the fleet page's own vocabulary for a fact nobody
#: said (`live | ended | unclear | unreported`), spelled out in full there because
#: a browser has the width. This cell is six characters wide, so it uses the glyph
#: these panels already use for every unsaid value: `repo`, `state` and `title` all
#: render one this way, and `mcp/mcp_server/board/views.py` renders this same field
#: this same way for `qb-board`.
#:
#: **It cannot be misread as a stage.** A stage is 1-6 alphanumerics by
#: construction (`app.api.leases.STAGE_RE`, and `qb-stage`'s own check before it),
#: so a non-alphanumeric glyph is outside the value space. An empty cell is not: it
#: reads equally as a rendering bug, a clipped column, or an agent that has no
#: stage — and "those agents have no stage" is the lie #262 is about.
STAGE_UNREPORTED = "—"


def stage_cell(agent: dict) -> tuple[str, str]:
    """(text, style) for how far along a live agent's work is — `F0`, `R1F`, `R2`.

    Beside `state`, never instead of it, and they answer different questions:
    `state` says whether the pane is moving, `stage` says where it has got to. The
    other columns — repo, branch, what — read identically at every stage of a PR's
    life, which is why a fleet view without this one cannot answer "how far along
    is it" about any row on it.

    Reported by `qb-stage` and by nothing else, so most rows will not have one for
    a while. That is not a defect and the dash says so honestly rather than
    leaving a blank that reads like a stage.
    """
    stage = (agent.get("stage") or "").strip()
    if not stage:
        return STAGE_UNREPORTED, "grey50"
    return clip(stage, 6), "bold cyan"


def short_key(key: str) -> str:
    """'prisonblues/quarterback:2.40' → 'quarterback:2.40'.

    The owner is the same for every repo the fleet touches, so it is 12 columns
    that never distinguish two rows.
    """
    return key.split("/", 1)[-1] if key.count("/") == 1 else key


def clip(s: str | None, n: int) -> str:
    s = " ".join((s or "").split())
    return s if len(s) <= n else s[: max(0, n - 1)] + "…"


#: How a seat spells itself on the board: `seat-<scope>-<n>`, where the scope is
#: the project the seat sits in and is what stops two screens on one machine both
#: wanting seat 1 (#208). The scope is optional because a seat whose scope slugged
#: away to nothing — or that was deliberately started with an empty QB_SEAT_SCOPE —
#: keeps the bare `seat-<n>` this had before, and the dashboard must go on
#: recognising those.
#:
#: The NUMBER is the last hyphenated field, not the first: a scope may contain
#: hyphens of its own, and `seat-nix-fleet-3` is seat 3 of nix-fleet rather than
#: anything about `nix`. The bound is qb-seat's own 1-99.
SEAT_RE = re.compile(r"^seat-(?:(.+)-)?([1-9][0-9]?)$")

#: The most of `seat-<scope>-<n>` a scope may take, mirroring SEAT_SCOPE_MAX in
#: qb-seat: the board allows 40 characters and `seat-`, a hyphen and two digits
#: account for the other eight.
SCOPE_MAX = 32


def _seat(holder: str | None) -> "re.Match[str] | None":
    """The seat match for a board identity, on the name half of `machine/name`."""
    if not holder:
        return None
    return SEAT_RE.match(holder.rsplit("/", 1)[-1])


def seat_number(holder: str | None) -> int | None:
    """1 for 'zeus/seat-lexray-1' and for 'zeus/seat-1'.

    None for anything that is not a seat.
    """
    match = _seat(holder)
    return int(match.group(2)) if match else None


def seat_machine(holder: str | None) -> str | None:
    """'zeus' for 'zeus/seat-lexray-1'. None for anything that is not a seat.

    The board is the FLEET's, not this box's: two machines can each hold a
    `seat-lexray-1`, so the machine half is part of what identifies a seat and
    leaving it out shows a remote agent's state against a local pane.
    """
    if _seat(holder) is None:
        return None
    machine, sep, _ = (holder or "").partition("/")
    return machine if sep else None


def seat_scope(holder: str | None) -> str | None:
    """'lexray' for 'zeus/seat-lexray-1'.

    None for 'zeus/seat-1', which is a seat numbered across the whole machine,
    and None for anything that is not a seat at all. The two cases are told
    apart by :func:`seat_number`, which answers for the first and not the second.
    """
    match = _seat(holder)
    return match.group(1) if match else None


def slug_scope(text: str | None) -> str | None:
    """Turn a requested scope into the one a seat will actually carry.

    MIRRORS `seat_scope_slug` in qb-seat, and is pinned to it by
    test_the_scope_rule_is_the_one_qb_seat_actually_applies — two implementations
    of one rule is exactly how a dashboard ends up showing one seat's state
    against another seat's pane. Case folding is ASCII-only for the same reason:
    qb-seat folds with `tr '[:upper:]' '[:lower:]'`, which is bytes, where
    str.lower() is Unicode.
    """
    if not text:
        return None
    lowered = "".join(chr(ord(c) + 32) if "A" <= c <= "Z" else c for c in text)
    slug = re.sub(r"[^a-z0-9]+", "-", lowered).strip("-")[:SCOPE_MAX].rstrip("-")
    return slug or None


def scope_of(repo_path: str | None) -> str | None:
    """The scope qb-seat gives a seat working in ``repo_path`` and told nothing else.

    The default half of the rule: a seat is named after its repository's own
    directory.
    """
    return slug_scope(os.path.basename((repo_path or "").rstrip("/")))


def pane_scope(seat: dict) -> str | None:
    """The scope of the seat in a tmux pane, or None when the pane cannot say.

    `@qb_scope` first, because a screen given an explicit QB_SEAT_SCOPE is the one
    case the repository cannot answer for: two screens on ONE repository, which is
    precisely what that knob exists for. `@qb_repo` otherwise, which is the default
    the seat itself computed from its cwd.

    An explicitly EMPTY scope reads here as "the pane cannot say", and that is the
    right answer rather than a gap: an empty scope asks for the machine-wide seat
    numbering, in which a second screen cannot hold the same number at all — so
    there is never a second candidate for the caller to confuse it with.
    """
    return slug_scope(seat.get("scope")) or scope_of(seat.get("repo"))


class BoardConfig:
    """Where the board is and how to authenticate to it.

    TWO URLS AND TWO CREDENTIALS, because the board has two front doors and they
    are not interchangeable. `base_url` is the AGENT vhost, reached with a bearer
    token, and it is what every read and every agent write here uses.
    `human_url` is the BROWSER vhost, reached with a signed-in session cookie,
    and it is the only door the human-only endpoints can be reached through at
    all — `app/auth.py` has the agent vhost strip `X-Edge-Auth`, so a call that
    only a person may make cannot be made on the agent side however it is
    authenticated. See :class:`HumanClient`.
    """

    def __init__(self, base_url: str, token: str, agent: str,
                 human_url: str = "", human_key: str = "",
                 human_key_cmd: str = "") -> None:
        self.base_url, self.token, self.agent = base_url.rstrip("/"), token, agent
        self.human_url = (human_url or "").rstrip("/")
        #: A PERSON's key for the human-only endpoints (`X-Human-Key`), presented
        #: to the SAME host as the bearer — the agent vhost, no Authelia. See
        #: :class:`HumanClient`.
        self.human_key = human_key or ""
        #: How to GET it, for the same reason `token_cmd` exists beside `token`: a
        #: secret that lives in 1Password is one `op` can re-read, and one that
        #: never sits in a file on disk. A value is still accepted — a test, or a
        #: box with no `op` — but the command is the form the fleet ships.
        self.human_key_cmd = human_key_cmd or ""


def resolve_config() -> BoardConfig:
    """Environment first, then the per-host config file.

    The same contract qb-seat implements in bash, and read the same way: the
    config is an unrestricted shell script, so it is SOURCED IN A SUBSHELL with
    three values read back out. Sourcing it into this process would let it
    replace anything it liked; parsing it with a regex would get the quoting
    wrong on the day someone puts a `$(…)` in their token command.

    Deliberately no mcp_server import. Depending on it made the dashboard need a
    built checkout of this repo's mcp/ — which is a thing an INSTALLED harness
    has no reason to have, and it is why `qb` failed on a freshly rebuilt host
    while every test passed on the machine that wrote it.
    """
    url = os.environ.get("QUARTERBACK_BASE_URL", "")
    token = os.environ.get("QUARTERBACK_TOKEN", "")
    token_cmd = os.environ.get("QUARTERBACK_TOKEN_CMD", "")
    # The browser vhost and a signed-in session for it. Both optional and both
    # usually only in the file — a shell has no reason to carry them — which is
    # why the condition below asks about them too. It used to read "no url or no
    # token", and a host with those two in its environment then never sourced the
    # config at all: the human URL sitting in that file was invisible, and the
    # dashboard reported no human credential on a machine that had one.
    human_url = os.environ.get("QUARTERBACK_HUMAN_URL", "")
    human_key = os.environ.get("QUARTERBACK_HUMAN_KEY", "")
    human_key_cmd = os.environ.get("QUARTERBACK_HUMAN_KEY_CMD", "")

    if (not url or not (token or token_cmd) or not human_url
            or not (human_key or human_key_cmd)):
        config = (os.environ.get("QUARTERBACK_CONFIG")
                  or os.path.join(os.environ.get("XDG_CONFIG_HOME")
                                  or os.path.expanduser("~/.config"),
                                  "quarterback", "config"))
        if os.path.isfile(config):
            # `%s\n` PER VALUE and nothing clever: a cookie is one line of opaque
            # text that may carry `=` and `;`, so the reader below partitions on
            # the FIRST `=` only — `name, _, value = line.partition("=")` keeps
            # everything after it verbatim, which is what a `session=abc; other=d`
            # needs. A cookie carrying a newline would break the framing, and
            # cannot: HTTP header values do not have them.
            script = (f'. {shlex.quote(config)} >&2 || exit 1\n'
                      'printf "url=%s\\n" "${QUARTERBACK_BASE_URL:-}"\n'
                      'printf "token=%s\\n" "${QUARTERBACK_TOKEN:-}"\n'
                      'printf "token_cmd=%s\\n" "${QUARTERBACK_TOKEN_CMD:-}"\n'
                      'printf "human_url=%s\\n" "${QUARTERBACK_HUMAN_URL:-}"\n'
                      'printf "human_key=%s\\n" "${QUARTERBACK_HUMAN_KEY:-}"\n'
                      'printf "human_key_cmd=%s\\n" "${QUARTERBACK_HUMAN_KEY_CMD:-}"\n')
            got = subprocess.run(["bash", "-c", script], capture_output=True,
                                 text=True, timeout=15)
            if got.returncode == 0:
                for line in got.stdout.splitlines():
                    name, _, value = line.partition("=")
                    if name == "url" and not url:
                        url = value
                    elif name == "token" and not token:
                        token = value
                    elif name == "token_cmd" and not token_cmd:
                        token_cmd = value
                    elif name == "human_url" and not human_url:
                        human_url = value
                    elif name == "human_key" and not human_key:
                        human_key = value
                    elif name == "human_key_cmd" and not human_key_cmd:
                        human_key_cmd = value

    if not token and token_cmd:
        got = subprocess.run(["bash", "-c", token_cmd], capture_output=True,
                             text=True, timeout=30)
        token = got.stdout.strip() if got.returncode == 0 else ""

    if not url:
        raise RuntimeError("no board configured (QUARTERBACK_BASE_URL is unset "
                           "and the site config did not supply one)")
    return BoardConfig(url, token, socket.gethostname().split(".", 1)[0],
                       human_url=human_url, human_key=human_key,
                       human_key_cmd=human_key_cmd)


def _ssl_context():
    """A context that trusts something, on interpreters that trust nothing.

    A uv-installed standalone Python has no CA bundle of its own and no NixOS
    ssl paths, so `urllib` there fails every HTTPS request with
    CERTIFICATE_VERIFY_FAILED — while the same code works on the interpreter the
    harness packages. That asymmetry is invisible until someone runs the
    dashboard from a checkout's venv and sees "board unreachable" on a board that
    is up, which is how this was found: in a pane, next to a working copy in the
    shell beside it.

    certifi is not a dependency; it is used when the interpreter already has it,
    which is exactly the case where the default store is empty.
    """
    try:
        import certifi
    except ImportError:
        return None                                 # the default store, which is fine
    return ssl.create_default_context(cafile=certifi.where())


#: The header a person's own key travels in — `app.auth.HUMAN_KEY_HEADER`. Named
#: rather than spelled inline so the two halves of one contract are greppable as a
#: pair; the board owns the name and this is the client that honours it.
HUMAN_KEY_HEADER = "X-Human-Key"


class HumanClient:
    """The writes only a PERSON may make, on a person's own key (#477, #479).

    A separate class from :class:`BoardClient` and not a method on it, because
    the credential is different and the difference is the whole point: this one
    authorises as `human/<user>`, and a caller that could reach it by accident
    from the ordinary client would be authoring decisions as a person.

    **Same host as everything else, and no Authelia.** It presents `X-Human-Key`
    to the agent vhost, beside the bearer that says which machine this is. That
    is what makes it maintainable: an earlier cut of this class held a signed-in
    Authelia session and posted to the browser vhost, and a session expires on a
    wall clock — so the dashboard's `✎` would have gone dead every time the
    cookie lapsed, and stayed dead until somebody re-minted it by hand. A static
    key rotates when somebody decides to rotate it and never otherwise.

    **What it costs is `app/config.py`'s to state and this class's to repeat**,
    because a reader arrives here first: the key sits on this workstation,
    readable by the processes running here, so an agent that goes looking can
    find it and author as a person. Accepted deliberately (#479) and bounded by
    being per person and revocable in one line.
    """

    #: What a caller is told when there is no human key here. Phrased as a remedy
    #: because the usual reason is a machine that never had one — and because a UI
    #: that greys a control out has room for a sentence and no room for a runbook.
    NO_KEY = ("no human key on this host — set QUARTERBACK_HUMAN_KEY_CMD "
              "in ~/.config/quarterback/config")

    #: What the key command's own failure is reported as. Kept apart from NO_KEY
    #: because they are opposite states with opposite remedies: nothing configured
    #: is a box that never had one, and a command that failed is usually `op`
    #: wanting to be unlocked — fixable in ten seconds by somebody who is told.
    KEY_FAILED = "the human-key command failed"

    #: Everything a key may be made of: printable ASCII, no controls. The bound
    #: that matters is CR and LF — `http.client.putheader` refuses those, and
    #: refuses them by raising a ValueError CARRYING THE HEADER VALUE, which this
    #: dashboard would then print on its detail line. A credential in a UI string
    #: is a credential in a screenshot, a scrollback and a tmux buffer.
    _KEY_OK = re.compile(r"^[\x20-\x7e]+$")

    def __init__(self, cfg: BoardConfig) -> None:
        self.cfg = cfg
        #: The resolved key, once something has resolved it. `None` is "not asked
        #: yet" and not "there isn't one" — see :meth:`key`.
        self._key: str | None = None

    def key(self, refresh: bool = False) -> str:
        """The key, resolved as late as possible.

        **Lazily, and this is the difference between a credential and a copy of
        one.** A value in the environment is in every child process of the shell
        that set it and in the config file it came from; a command is run when a
        write is actually made, which on this fleet means `op read` against a
        vault that may need unlocking. The secret then lives in this process for
        as long as it is useful and nowhere else.

        `refresh` re-runs the command. Unlike the session this replaced, a static
        key does not go stale on its own — so this exists for the one case that
        remains, a key rotated while a long-lived dashboard was running.
        """
        if self._key is not None and not refresh:
            return self._key
        if self.cfg.human_key and not refresh:
            # THROUGH THE SAME CHECK, and stripped the same way. A literal comes
            # from a config file or an environment variable, both of which carry
            # trailing newlines as readily as `op` does.
            self._key = self._checked(self.cfg.human_key.strip())
            return self._key
        if not self.cfg.human_key_cmd:
            raise RuntimeError(self.NO_KEY if not self.cfg.human_key else
                               "this key is a fixed value and cannot be "
                               "refreshed — update QUARTERBACK_HUMAN_KEY, or use "
                               "the _CMD form")
        try:
            got = subprocess.run(["bash", "-c", self.cfg.human_key_cmd],
                                 capture_output=True, text=True, timeout=30)
        except Exception as exc:                  # noqa: BLE001
            raise RuntimeError(f"{self.KEY_FAILED}: {type(exc).__name__}") from exc
        value = got.stdout.strip()
        if got.returncode != 0 or not value:
            # stderr, clipped: `op` says "not signed in" there and nowhere else,
            # and a remedy the caller cannot read is a remedy they do not have.
            why = " ".join((got.stderr or "").split())[:200]
            raise RuntimeError(f"{self.KEY_FAILED}" + (f": {why}" if why else ""))
        self._key = self._checked(value)
        return self._key

    def _checked(self, value: str) -> str:
        """One header-safe line, or a refusal that does NOT quote what was wrong.

        Checked HERE rather than left to `putheader`, and the difference is the
        whole point: the stdlib's own message is `Invalid header value
        b'<the entire secret>'`, and every caller of this class turns an exception
        into a sentence for a human to read.
        """
        if self._KEY_OK.match(value):
            return value
        bad = next((f"{c!r}" for c in value if not (0x20 <= ord(c) <= 0x7e)), "?")
        raise RuntimeError(
            f"the human key is not usable as a header — it contains {bad}. "
            "A newline or control character in the vault field is the usual "
            "cause; the value itself is not shown here on purpose")

    def why_not(self) -> str | None:
        """None when a human write could work here; otherwise why it cannot.

        Asked BEFORE the control is drawn rather than after it is used. A verb
        that looks available and fails on the click is the shape that gets read as
        a broken button, and this one would fail against a board that is perfectly
        healthy — the thing that is missing is on this host.

        **A CONFIGURED COMMAND COUNTS**, and running it to find out does not
        belong here: this is asked on every paint, `op read` is a network call and
        a possible unlock prompt, and a dashboard that ran one every few seconds
        would be its own bug. Whether the key WORKS is answered where it is used —
        at the write, in a sentence the panel shows verbatim.
        """
        if not (self.cfg.human_key or self.cfg.human_key_cmd):
            return self.NO_KEY
        return None

    def post(self, path: str, body: dict) -> dict:
        """One human write. Raises with a sentence a panel can show verbatim."""
        why = self.why_not()
        if why:
            raise RuntimeError(why)
        headers = {"Content-Type": "application/json", HUMAN_KEY_HEADER: self.key()}
        if self.cfg.token:
            # The bearer rides along: it is what says which MACHINE this is, and
            # the board reads both — the key answers "which person", the token
            # answers "from where". A board that got only the key would authorise
            # the write and have nothing to say about its origin.
            headers["Authorization"] = f"Bearer {self.cfg.token}"
        req = urllib.request.Request(
            f"{self.cfg.base_url}{path}", data=json.dumps(body).encode(),
            headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=30,
                                        context=_ssl_context()) as resp:
                text = resp.read().decode()
        except urllib.error.HTTPError as exc:
            raise RuntimeError(self._refusal(exc)) from exc
        except ValueError as exc:
            # The stdlib refusing to send the header at all. `_checked` should have
            # caught this; if something got past it, the one thing that must not
            # happen is the message reaching a screen, because it quotes the value.
            raise RuntimeError("this key cannot be sent as a header "
                               f"({type(exc).__name__}); the value is withheld") from None
        return json.loads(text) if text.strip() else {}

    @staticmethod
    def _refusal(exc: "urllib.error.HTTPError") -> str:
        """What the board said, in words a panel can show.

        The board names its own mechanism in the body, so the body is what is
        worth relaying — a 403 here is "this key matches nobody" or "this endpoint
        is human-only", and those want different things done about them.
        """
        try:
            body = exc.read().decode()[:400]
        except Exception:                             # noqa: BLE001
            body = ""
        said = ""
        try:
            detail = json.loads(body).get("detail")
            said = detail if isinstance(detail, str) else json.dumps(detail)
        except Exception:                             # noqa: BLE001
            said = " ".join(body.split())[:200]
        if exc.code in (401, 403):
            return said or f"the board refused this key ({exc.code})"
        return said or f"the board answered {exc.code}"

    def set_dial(self, dial: str, value, reason: str, repo: str | None = None,
                 expires_at: str | None = None) -> dict:
        """Put a dial in force (`POST /dials`), as the person this key names.

        **`value` is any JSON and this does not know what a dial means** — the
        harness owns that vocabulary and a client that checked it would be the
        second place a dial is written down (#56, #305). It is passed through as
        given, `None` included: `null` is the documented off switch for three
        dials and the board keeps it apart from "no row at all".

        `reason` is required by the board and required here — a dial whose
        argument was never written down is one nobody can decide to remove — so a
        blank one is refused where it can be explained rather than at a 422.
        """
        if not (reason or "").strip():
            raise RuntimeError("a dial needs a reason: why is this value in force?")
        body: dict = {"dial": dial, "value": value, "reason": reason.strip()}
        if repo:
            body["repo"] = repo
        if expires_at:
            body["expires_at"] = expires_at
        return self.post("/dials", body)

    def clear_dial(self, dial: str, repo: str | None = None) -> dict:
        """Take a dial off the board (`POST /dials/clear`); the repo's default returns."""
        body: dict = {"dial": dial}
        if repo:
            body["repo"] = repo
        return self.post("/dials/clear", body)


class BoardClient:
    """The handful of calls the harness makes. stdlib only, on purpose."""

    def __init__(self, cfg: BoardConfig) -> None:
        self.cfg = cfg

    def _request(self, req: urllib.request.Request, *, allow_empty: bool = False) -> dict:
        """One authenticated request. ``allow_empty`` belongs to the WRITE path only.

        An empty body is not ``{}``. A proxy's contentless 502, a 204 from a board
        mid-deploy, a truncated response — read as an empty object, every one of
        those arrives at a caller as "the board says there is nothing there", which
        is the absence-vs-inability collapse this client's consumers exist to
        report on rather than commit. Two of them are one line away from it:
        `qb-reconcile` turns `GET /plan` into `plan.get("items") or []` and would
        print "the plan agrees with GitHub and the board on everything checked"
        over a plan it never received, and :func:`fetch_board` sets ``error`` from
        an exception it would no longer get, rendering an empty fleet as a healthy
        one. So ``get`` lets `json` raise, exactly as it did before ``post``
        existed; only ``post``, whose 200 legitimately carries no body, tolerates
        an empty one.
        """
        if self.cfg.token:
            req.add_header("Authorization", f"Bearer {self.cfg.token}")
        with urllib.request.urlopen(req, timeout=30, context=_ssl_context()) as resp:
            body = resp.read().decode()
        if allow_empty and not body.strip():
            return {}
        return json.loads(body)

    def get(self, path: str, params: dict | None = None) -> dict:
        query = urllib.parse.urlencode(
            {k: v for k, v in (params or {}).items() if v is not None})
        url = f"{self.cfg.base_url}{path}" + (f"?{query}" if query else "")
        return self._request(urllib.request.Request(url))

    def post(self, path: str, body: dict) -> dict:
        req = urllib.request.Request(
            f"{self.cfg.base_url}{path}",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
            method="POST")
        return self._request(req, allow_empty=True)

    def active(self) -> dict:
        return self.get("/active")

    def claims(self) -> dict:
        return self.get("/claims")

    def claim_held(self, repo: str, session: str | None = None) -> dict:
        """Does this agent hold a live claim in `repo` — the board's own answer.

        Read rather than worked out from `claims()`, because the repo a key
        belongs to is derived from the key and this file has no business
        re-deriving it: `issue_claims` below already joins on the key shape while
        the plan filters on the kind, and #172 is the record of those two
        disagreeing.
        """
        return self.get("/claim/held", {"repo": repo, "session": session})

    def claim_ref(self, ref_kind: str, ref_value: str, repo: str | None = None,
                  **over) -> dict:
        """Take a claim by naming the RESOURCE. The board derives the key (#172)."""
        ref: dict = {"kind": ref_kind, "value": str(ref_value)}
        if repo is not None:
            ref["repo"] = repo
        return self.post("/claim", {"ref": ref, **over})


def board_client():
    cfg = resolve_config()
    return BoardClient(cfg), cfg


def fetch_board(client) -> dict:
    """Everything the board can tell us. Never raises — a dead board is a state."""
    out: dict = {"agents": [], "subagents": [], "claims": [], "error": None}
    try:
        active = client.active()
        out["agents"] = active.get("agents", [])
        out["subagents"] = active.get("subagents", [])
        out["claims"] = [
            c for c in client.claims().get("claims", [])
            if not c.get("released") and not c.get("lapsed")
        ]
    except Exception as exc:                      # noqa: BLE001 — display it, don't die
        out["error"] = f"{type(exc).__name__}: {exc}"
    return out


def _gh_list(kind: str, repo: str, fields: str,
             limit: int = 30) -> tuple[list[dict], str | None]:
    """One `gh <kind> list` against one repo, every row tagged with that repo.

    The tag is what lets the panels say where a row came from, and it is applied
    here rather than at render time so nothing downstream can hold a row whose
    origin it cannot name.
    """
    try:
        raw = subprocess.run(
            ["gh", kind, "list", "--repo", repo, "--state", "open",
             "--limit", str(limit), "--json", fields],
            capture_output=True, text=True, timeout=45,
        )
        if raw.returncode != 0:
            # The fallback has to be INSIDE the interpolation. Outside it, `or`
            # tests the whole f-string, which holds `": "` and so is never falsy —
            # the exit code was unreachable and a `gh` that failed silently
            # rendered as a repo name followed by nothing, which is the one error
            # line a reader cannot act on.
            why = clip(raw.stderr, 50) or f"gh exit {raw.returncode}"
            return [], f"{short_repo(repo)}: {why}"
        rows = json.loads(raw.stdout)
        for row in rows:
            row["repo"] = repo
        return rows, None
    except Exception as exc:                      # noqa: BLE001
        return [], f"{short_repo(repo)}: {type(exc).__name__}"


def _gh_list_many(kind: str, fields: str, repos: list[str] | None = None,
                  limit: int = 30):
    """Every repo this dashboard watches. One failing repo is reported, not fatal:
    a token that cannot see one of three is still worth three panels."""
    out: list[dict] = []
    errs: list[str] = []
    for repo in (repos if repos is not None else resolve_repos()):
        rows, err = _gh_list(kind, repo, fields, limit)
        out.extend(rows)
        if err:
            errs.append(err)
    return out, ("; ".join(errs) if errs else None)


#: How many open PRs one `gh` call may return. Higher than the issue list's 30
#: because the review queue's DEPTH is computed off this list, and a depth
#: silently capped at the fetch limit is the number this whole feature exists to
#: make trustworthy. :func:`fetch_review_queue` still says so when a repo comes
#: back holding exactly this many, because "60 open PRs" and "at least 60" are
#: different facts and only one of them is a depth.
PR_LIMIT = 60

#: Rows the printed OPEN PRs panel draws before it stops and counts the rest. A
#: printed panel cannot scroll, and `PR_LIMIT` rows of it would push everything
#: below off the screen.
PR_ROWS = 12


def fetch_prs(repos: list[str] | None = None,
              probe_ci: bool = True) -> tuple[list[dict], str | None]:
    """Open PRs, each stamped with what its checks actually say.

    `headRefOid` is fetched because :func:`ci_report` needs it: when the rollup
    comes back empty the only way to tell "nothing ran" from "a run exists and is
    gated" is to ask the workflow-runs API about that exact head (#324).

    `probe_ci=False` skips the extra call and leaves the empty case reading
    `unknown` — coarser, but never the quiet grey dot that used to mean "no news".
    """
    rows, err = _gh_list_many(
        "pr",
        # headRefOid / mergeable / createdAt are the review queue's inputs (#273).
        # Fetched here rather than in a second `gh` call because both panels read
        # one list, and two lists a minute apart would disagree about a PR that
        # moved between them.
        "number,title,isDraft,updatedAt,statusCheckRollup,headRefName,"
        "headRefOid,mergeable,createdAt", repos, PR_LIMIT)
    return resolve_ci(rows, probe=probe_ci), err


def fetch_issues(repos: list[str] | None = None) -> tuple[list[dict], str | None]:
    return _gh_list_many("issue", "number,title,updatedAt,labels,assignees", repos)


def issue_claims(claims: list[dict], repo: str = REPO) -> dict[int, dict]:
    """{issue number → the claim on it}, for `repo`'s issues.

    The board namespaces an issue claim as `owner/repo#n` and that `n` is the
    number `gh issue list` reports, so the join wants no lookup table. Another
    repo's key is skipped rather than joined on the number alone — two repos
    both have a #12, and marking ours held because theirs is would send the
    next seat past the one issue it should have taken.

    The repo is an argument, not a constant read inside: which repo a dashboard
    is showing is on its way to being derived from the checkout rather than
    hardcoded here (#176), and a caller that has worked it out should not have
    to reach past this function to use it.
    """
    held: dict[int, dict] = {}
    for c in claims:
        prefix, _, number = (c.get("key") or "").strip().rpartition("#")
        if prefix == repo and number.isdigit():
            held.setdefault(int(number), c)
    return held


# ---- the review queue: what review is waiting on, and for how long (#273) ----
#
# `qb-dash` already pays for one `gh pr list` per refresh and the board already
# holds every panel run, every plan item and every claim. Neither half alone can
# answer "what is review waiting on" — which is why, on 2026-08-20, six of eight
# open PRs had never been panelled and nobody could see it. `POST /review-queue`
# is the join; this is the client for it, and it reuses the PR rows the OPEN PRs
# panel already fetched rather than asking `gh` a second time.

#: The verb each `next_action` gets in a narrow pane. The board publishes the
#: long form on every entry; this is only the column width.
QUEUE_VERB = {
    "integrate": "rebase",
    "review": "panel",
    "re-review": "re-panel",
    "fix": "fix",
    "land": "land",
    "answer": "answer",
    "none": "—",
}

#: State → colour. Red is "a round spent here is wasted", magenta is "findings
#: are sitting unfixed", yellow is "review is owed", green has left the queue.
QUEUE_COLOUR = {
    "escalated": "bold red",
    "blocked": "red",
    "unresolved": "magenta",
    "stale": "yellow",
    "unreviewed": "yellow",
    "unconverged": "cyan",
    "ready": "green",
    "exempt": "grey50",
}

#: The only hold code too long for the verb column. Abbreviated here rather than
#: clipped, because `mergeable…` and `mergeable-unknown` read as the same word
#: and the `?` is the whole of what it says.
QUEUE_HOLD = {"mergeable-unknown": "mergeable?"}

#: The `gh pr list` fields `POST /review-queue` reads, under GitHub's own names —
#: the endpoint takes them as aliases so nothing has to translate on the way.
QUEUE_PR_FIELDS = ("number", "title", "headRefOid", "mergeable", "createdAt", "isDraft")


def waited(seconds) -> str:
    """'40m', '6h12m', '2d12h' — how long an entry has been waiting.

    Days, which :func:`ago` does not do: this queue's whole complaint is measured
    in them, and "60h13m" is a number a reader has to convert before it means
    anything.
    """
    try:
        secs = int(seconds)
    except (TypeError, ValueError):
        return ""
    if secs < 0:
        return ""
    if secs < 3600:
        return f"{secs // 60}m"
    if secs < 86400:
        return f"{secs // 3600}h{(secs % 3600) // 60:02d}m"
    days, rest = divmod(secs, 86400)
    return f"{days}d{rest // 3600:02d}h"


def gh_error_repos(err: str | None) -> set[str]:
    """The SHORT repo names named in a :func:`_gh_list_many` error string.

    Reads the format :func:`_gh_list` writes — ``"<short repo>: <why>"``, joined
    by ``"; "`` — and the two live in this file together for exactly that reason.
    The alternative was worse in both directions: a queue that cannot tell WHICH
    repo's listing failed must either refuse every repo (throwing away good data
    for two repos because a third's token expired) or trust a list nobody could
    read (rendering a repo with eight PRs waiting as a drained queue).

    A malformed or unattributable error yields the empty set, and the caller
    treats that as "every repo is suspect" — the conservative direction, since it
    can only withhold an answer, never invent one.

    It answers in SHORT names because that is what the string carries, and short
    names are not unique: `org1/api` and `org2/api` produce the same prefix, and
    a panel showing both has always rendered their errors identically. The caller
    resolves that rather than this function guessing — see
    :func:`fetch_review_queue`, which refuses to derive for either of a colliding
    pair while a listing error is outstanding.
    """
    return {part.split(":", 1)[0].strip()
            for part in (err or "").split("; ") if ":" in part}


def _queue_pr(row: dict) -> dict:
    """One `gh` PR row, trimmed to what the endpoint reads.

    Trimmed rather than sent whole because `statusCheckRollup` is a list of every
    check on the PR, and it would be the bulk of a request that is otherwise a
    few hundred bytes per PR.
    """
    return {k: row.get(k) for k in QUEUE_PR_FIELDS if row.get(k) is not None}


def fetch_review_queue(client, prs: list[dict], repos: list[str] | None = None,
                       pr_err: str | None = None) -> dict:
    """The derived review queue for the watched repos. Never raises.

    A dead board is a state, not an exception — the same contract
    :func:`fetch_board` keeps, and for the same reason: a dashboard that dies on
    a 502 tells its reader less than one that says the board is unreachable.

    **A PR list that failed is not an empty PR list**, which is why `pr_err` is an
    argument. `fetch_prs` returns `([], err)` when `gh` fails, and sending that
    empty list as the repo's open PRs would have the board honestly answer "no
    open pull requests" — rendering a repo with eight PRs waiting as a queue that
    has drained. So a failed listing refuses to derive at all and reports the
    failure, because the whole value of this panel is a depth somebody can act
    on and a depth built on a list nobody could read is not one.

    Entries come back flattened across repos and tagged with the repo they came
    from, sorted oldest-drainable first. That sort is a READING order and not a
    work order: the board deliberately refuses to rank the queue (#232 owns the
    order), so this is the dashboard deciding what to put at the top of a panel
    that cannot scroll, which is a different decision from deciding what to do.
    """
    out: dict = {"entries": [], "open": 0, "depth": 0, "oldest": None,
                 "oldest_held": None, "idle": None, "error": None}
    watched = list(repos if repos is not None else resolve_repos())
    failed = gh_error_repos(pr_err) if pr_err else set()
    if pr_err and not failed:
        # An error nothing could attribute makes every listing suspect.
        out["error"] = f"pr list: {pr_err}"
        return out
    # How many watched repos each short name stands for. A listing error names a
    # short name, so while one is outstanding a name shared by two watched repos
    # cannot say which of them failed — and both a false "drained" and a false
    # "unavailable" would be a fact stated about a repo nobody measured.
    shared: dict[str, int] = {}
    for r in watched:
        shared[short_repo(r)] = shared.get(short_repo(r), 0) + 1
    by_repo: dict[str, list[dict]] = {}
    for row in prs:
        by_repo.setdefault(row.get("repo") or "", []).append(row)

    errs: list[str] = []
    idles: list[str] = []
    for repo in watched:
        rows = by_repo.get(repo, [])
        if pr_err and shared[short_repo(repo)] > 1:
            errs.append(f"{repo}: which `{short_repo(repo)}` failed is ambiguous")
            continue
        if short_repo(repo) in failed:
            # This repo's listing failed, so its empty row list is not an empty
            # repo. The others' lists are still good and still worth a queue.
            errs.append(f"{short_repo(repo)}: pr list unavailable")
            continue
        if len(rows) >= PR_LIMIT:
            errs.append(f"{short_repo(repo)}: {PR_LIMIT}+ open PRs, depth is a floor")
        body = {"repo": repo, "prs": [_queue_pr(r) for r in rows]}
        try:
            answer = client.post("/review-queue", body)
        except Exception as exc:                  # noqa: BLE001 — display it, don't die
            errs.append(f"{short_repo(repo)}: {type(exc).__name__}")
            continue
        # `post` tolerates an empty body because a WRITE's 200 legitimately
        # carries none. This is a read through that method, so the tolerance has
        # to be undone here: a truncated response, a proxy's contentless 502 or a
        # board mid-deploy would otherwise arrive as `{}` and render as a queue
        # that is empty rather than one that could not be read (#244).
        if not isinstance(answer, dict) or "counts" not in answer:
            errs.append(f"{short_repo(repo)}: empty answer")
            continue
        counts = answer.get("counts") or {}
        out["open"] += counts.get("open") or 0
        out["depth"] += counts.get("drainable") or 0
        for entry in answer.get("entries") or []:
            out["entries"].append({**entry, "repo": repo})
        for field in ("oldest", "oldest_held"):
            oldest = answer.get(field)
            if oldest and (out[field] is None
                           or (oldest.get("age_seconds") or 0)
                           > (out[field].get("age_seconds") or 0)):
                out[field] = {**oldest, "repo": repo}
        if answer.get("idle_reason"):
            idles.append(f"{short_repo(repo)}: {answer['idle_reason']}")

    out["entries"].sort(key=lambda e: (not e.get("drainable"),
                                       -(e.get("age_seconds") or 0)))
    out["error"] = "; ".join(errs) if errs else None
    # Only when NOTHING is drainable anywhere. A queue with depth is not idle,
    # however many of its repos happen to be.
    out["idle"] = "; ".join(idles) if idles and not out["depth"] else None
    return out


def queue_oldest(queue: dict) -> tuple[str, bool]:
    """`("2d12h", held)` — the longest wait in the queue, and whether it is held.

    Falls back to the oldest HELD entry when nothing is drainable, because a repo
    where every PR has been stuck for five days is the reading this panel exists
    to surface and reporting only the drainable ones would render it as a queue
    with no age at all. The flag is what stops the fallback reading as a wait
    somebody could go and end.
    """
    oldest = queue.get("oldest")
    if oldest:
        return waited(oldest.get("age_seconds")), False
    return waited((queue.get("oldest_held") or {}).get("age_seconds")), True


def queue_cell(queue: dict) -> tuple[str, str, str, str]:
    """`REVIEW  3 waiting  oldest 2d12h` — the header's own summary of the queue.

    Beside the caps because it is the same kind of number: the caps say what the
    seats may spend, and this says what is waiting to be spent on. A depth of
    zero is not the same as a board that could not be asked, so an error reports
    as `?` rather than as an empty queue (#244).
    """
    if queue.get("error") and not queue.get("open"):
        return ("REVIEW", "?", clip(queue["error"], 40), "red")
    depth = queue.get("depth") or 0
    age, held = queue_oldest(queue)
    colour = "green" if not depth else ("yellow" if depth < 5 else "red")
    if not age:
        return ("REVIEW", f"{depth} waiting", "", colour)
    return ("REVIEW", f"{depth} waiting", f"{'held' if held else 'oldest'} {age}", colour)


#: What each `since_basis` means, said as the thing the age is measured FROM.
#: The board publishes the basis beside the age precisely so a reader is not left
#: guessing what "waiting 2d12h" is counting; spelling it out is that promise
#: kept, and an unknown basis falls through as itself rather than being dropped.
QUEUE_BASIS = {
    "pr_opened": "since it was opened",
    "last_run": "since the last round",
    "queue_entered": "since it entered the queue",
    "plan_item_updated": "since the plan item last moved",
    "needs_human_first_flagged": "since a human was first asked",
}


def queue_detail(entry: dict) -> str:
    """The whole of a review-queue row, for the detail line under the tables.

    Worth a click because the row is six narrow cells and the entry carries the
    argument: the board computes a `reason` sentence for every verdict, and that
    sentence is the answer to "why is this one waiting" — it exists nowhere else,
    not on the PR and not on the board tape.

    EVERY hold is listed, not the first. `holds` is a list on purpose — "it is a
    draft" and "somebody holds the claim" are two facts, and a reader shown only
    the first would act the moment the draft flag cleared and be wrong. The row
    above has room for one; this has room for all of them.
    """
    bits = [f"{short_repo(entry.get('repo') or REPO)}#{entry.get('pr')}",
            entry.get("title") or "(untitled)",
            entry.get("state") or "?"]
    action = entry.get("next_action")
    if action and action != "none":
        # The board's own vocabulary, not the abbreviation the column wears: the
        # cell says `re-panel` because eleven columns is what it has, and this
        # line has room for the word the API actually uses.
        bits.append(f"next: {action}" if entry.get("drainable")
                    else f"would be: {action}")
    age = waited(entry.get("age_seconds"))
    if age:
        basis = QUEUE_BASIS.get(entry.get("since_basis"), entry.get("since_basis"))
        waiting = f"waiting {'at most ' if entry.get('age_is_upper_bound') else ''}{age}"
        bits.append(f"{waiting} ({basis})" if basis else waiting)
    holds = [h.get("code") for h in (entry.get("holds") or []) if h.get("code")]
    if holds:
        bits.append("held by " + ", ".join(holds))
    if entry.get("reason"):
        bits.append(entry["reason"])
    return " · ".join(b for b in bits if b)


def repo_ref(row: dict) -> str:
    """'prisonblues/quarterback#176' — a numbered row's identity, across repos.

    The number alone is not one. Once a panel shows more than one repo two rows
    both called #12 are two different things, and anything that keys on the bare
    number silently conflates them — a claim join marking ours held because
    theirs is, or a DataTable handed the same row key twice (#209).
    """
    return f"{row.get('repo') or REPO}#{row.get('number')}"


def issue_key(row: dict) -> str:
    """'prisonblues/quarterback#176' — how the board namespaces a claim.

    The same identity :func:`repo_ref` builds, under the name the board's claim
    keys use, so a reader following a claim is not sent to a function about
    table rows.
    """
    return repo_ref(row)


def claims_by_issue(claims: list[dict]) -> dict[str, dict]:
    """{'owner/repo#n' → the claim on it}, across every repo on the board."""
    held: dict[str, dict] = {}
    for c in claims:
        key = (c.get("key") or "").strip()
        prefix, sep, number = key.rpartition("#")
        if sep and prefix.count("/") == 1 and number.isdigit():
            held.setdefault(key, c)
    return held


def sort_issues(issues: list[dict], held: dict) -> list[dict]:
    """Free issues first, newest first inside each group.

    The free ones are what the panel is for — a seat reads it to find work
    nobody has taken — and on a pane that fits a dozen rows, a run of held
    issues along the top is the list failing at its one job.
    """
    def taken(issue: dict) -> bool:
        # Accepts either index: keyed by 'owner/repo#n' (what the panels use now
        # that they show several repos) or by bare number (the older shape).
        return issue_key(issue) in held or issue.get("number") in held

    return sorted(issues, key=lambda i: (taken(i), -(i.get("number") or 0)))


# ---- what the checks actually say --------------------------------------------
#
# #324. `gh pr checks 282` printed nothing for two days and every reader took that
# for "CI has not run yet". What had happened is that CI ran, went RED, and the two
# commits pushed to fix it came back `action_required` — GitHub's workflow-approval
# gate — so they executed nothing, contributed no check runs, and the PR's check
# list went EMPTY. Not red, not stale-green: absent. Absent is the one answer a
# reader treats as benign, so the red result became a benign-looking one and the
# branch sat.
#
# Verified against the incident's own commits rather than reasoned about:
# `repos/prisonblues/quarterback/commits/e5a07b5/check-runs` returns
# `total_count: 0`, and so does its check-suites — an unexecuted run contributes
# NOTHING to the head it was created for.
#
# So the rollup alone cannot answer the question, and the rule this file already
# applies to an unreadable board applies here: the absence of a signal must never
# render as the presence of a good one (#244, and `qb-reconcile`'s Unknown).

#: The six answers a reader can get about a PR's checks. Closed, and constrained
#: here rather than restated per reader, for the reason `qb-reconcile.CONDITIONS`
#: is closed: they feed counts and glyphs, and an unknown value silently leaves the
#: numerator while still counting as coverage.
#:
#: They are six because collapsing any pair loses the distinction that bit:
#:
#: * ``green``   — a run finished and every check passed.
#: * ``red``     — a run finished and something failed.
#: * ``pending`` — a run exists and is still going. Wait.
#: * ``blocked`` — a run exists, will NOT execute without a human, and so will
#:                 never report. An approval gate, a stale run, a cancelled one.
#: * ``none``    — no run has been created for this head at all. Genuinely untested.
#: * ``unknown`` — could not be determined. NOT a synonym for ``none``.
CI_STATES = ("green", "red", "pending", "blocked", "none", "unknown")

#: A rollup entry that says the check will never run without somebody clicking.
#: `stale` and `cancelled` join `action_required` because all three share the
#: property that matters: the run was created, produced no verdict, and the newest
#: EXECUTED run is the fact a reader actually wants.
#: `REQUESTED` is deliberately NOT here. It is GitHub's word for a check run that
#: has been created and not started — transient, and on its way to `pending` — so
#: reading it as gated would tell a reader a human is needed when nobody is. Found
#: by Codex on this change. `WAITING` is here because it means a deployment
#: protection rule, which IS a person.
CI_BLOCKED_CONCLUSIONS = frozenset({
    "ACTION_REQUIRED", "STALE", "CANCELLED", "WAITING"})
CI_FAILING_CONCLUSIONS = frozenset({
    "FAILURE", "ERROR", "TIMED_OUT", "STARTUP_FAILURE"})
#: `WAITING` is deliberately absent: it is in the blocked set, which is tested
#: first, so listing it here would be dead and would read as a disagreement.
CI_RUNNING_CONCLUSIONS = frozenset({
    "PENDING", "QUEUED", "IN_PROGRESS", "EXPECTED", "REQUESTED", ""})
CI_PASSING_CONCLUSIONS = frozenset({"SUCCESS", "NEUTRAL", "SKIPPED", "COMPLETED"})

#: A workflow run's `status`/`conclusion` when GitHub created it and is waiting on
#: a person. `action_required` appears in both fields depending on which endpoint
#: answered, which is why this set is matched against either. `requested` is left
#: out for the reason above: an ordinary run passes through it on its way to
#: starting, and calling that permanently blocked is a false alarm in the direction
#: that makes people stop reading the alarms. It falls to `pending` instead, which
#: still refuses a merge.
CI_GATED_RUN = frozenset({"action_required", "waiting"})

#: A run that never executed reaches a conclusion without producing a verdict, so
#: these do NOT count as "the newest executed run".
CI_UNEXECUTED_RUN = frozenset({"action_required", "stale", "cancelled", "skipped", ""})

#: (glyph, colour) per state, for both dashboards. `none` keeps the quiet dot it
#: has always had — but it is now only ever reached by ASKING, never by finding the
#: rollup empty, which is the whole of the fix. `unknown` is yellow and not grey:
#: "I could not tell" is a thing to look at, not a thing to scroll past.
CI_GLYPHS = {
    "green":   ("✓", "green"),
    "red":     ("✗", "red"),
    "pending": ("◐", "yellow"),
    "blocked": ("⚑", "magenta"),
    "none":    ("·", "grey50"),
    "unknown": ("?", "yellow"),
}

#: Seconds a probe's answer is reused. The dashboards redraw on a timer and each
#: probe is one or two `gh api` calls per PR with an empty rollup; without this a
#: gated PR would cost two round trips every refresh, forever.
CI_PROBE_TTL = 90

#: How many probe answers to keep. A dashboard runs for days and every push gives
#: a PR a new head, so an unbounded dict is a slow leak in a process that is meant
#: to be left open.
CI_CACHE_MAX = 256

#: How many of a branch's workflow runs to read back when looking for the newest
#: one that actually executed. Generous enough to see past a run of gated pushes —
#: the incident had two, and a third would have hidden the failure just as well.
CI_RUN_PAGE = 30

_ci_cache: dict[tuple, tuple[float, "CiReport"]] = {}


@dataclass(frozen=True)
class CiReport:
    """What is known about one PR's checks, and how it was established.

    `state` is the answer; `reason` is why it is that answer when the answer is
    one a reader must not skim past. Frozen because it is cached and handed to
    several renderers, none of which owns it.
    """

    state: str
    #: One line fit to print anywhere — a dashboard title, a preland reason, a
    #: reviewer prompt. Never empty.
    summary: str
    #: The blocking or unreadable detail, when there is one.
    reason: str = ""
    #: "failure at 843c506" — the newest run on this branch that actually RAN.
    #: The fact that matters when the head's own runs never executed, and the
    #: thing #324 says must be reported alongside a block.
    last_executed: str = ""
    #: How many rollup entries the state was read from. 0 is not evidence.
    checks: int = 0

    @property
    def blocking(self) -> bool:
        """Is this an answer a merge gate must refuse on?

        Every state except `green` is, and that includes `none` and `unknown` —
        the two that used to read as "nothing to see". A gate that merges on the
        absence of a signal is the defect, not the caller's tuning.
        """
        return self.state != "green"


def classify_rollup(rollup) -> str:
    """`green|red|pending|blocked|unknown` from a `statusCheckRollup` ALONE.

    Deliberately never returns `none`. An empty rollup is exactly the ambiguity
    #324 is about — no run created, or a run created and gated so hard it
    contributed nothing — and this function cannot tell them apart, so it says so.
    Only :func:`ci_report`, which asks GitHub a second question, may answer `none`.
    """
    entries = rollup or []
    if not entries:
        return "unknown"
    states = set()
    for c in entries:
        s = c.get("conclusion") or c.get("state") or c.get("status") or ""
        states.add(str(s).upper())
    if states & CI_BLOCKED_CONCLUSIONS:
        return "blocked"
    if states & CI_FAILING_CONCLUSIONS:
        return "red"
    if states & CI_RUNNING_CONCLUSIONS:
        return "pending"
    if states <= CI_PASSING_CONCLUSIONS:
        return "green"
    return "pending"


def _gh_api(path: str, timeout: int = 30) -> tuple[dict | list | None, str | None]:
    """`(parsed, error)` for one `gh api` GET. Never raises, never returns both."""
    try:
        raw = subprocess.run(["gh", "api", path], capture_output=True, text=True,
                             stdin=subprocess.DEVNULL, timeout=timeout)
    except Exception as exc:                      # noqa: BLE001
        return None, type(exc).__name__
    if raw.returncode != 0:
        return None, clip(raw.stderr.strip().splitlines()[-1] if raw.stderr.strip()
                          else f"gh exit {raw.returncode}", 80)
    try:
        return json.loads(raw.stdout or "null"), None
    except json.JSONDecodeError:
        return None, "unparseable gh output"


def workflow_runs(repo: str, sha: str = "", branch: str = "") -> tuple[list[dict], str | None]:
    """Workflow runs for one head SHA or one branch, newest first.

    This is the second source of truth #324 needs and the ONLY one that sees a
    gated run: an unexecuted run contributes no check runs and no check suites, so
    it is invisible to `statusCheckRollup`, to `gh pr checks` and to the commit's
    own check-runs endpoint. It is visible here, with `conclusion:
    "action_required"` — which is how `gh run list` showed the incident while the
    PR showed nothing.
    """
    query = urllib.parse.urlencode(
        {k: v for k, v in (("head_sha", sha), ("branch", branch),
                           ("per_page", str(CI_RUN_PAGE))) if v})
    got, err = _gh_api(f"repos/{repo}/actions/runs?{query}")
    if err:
        return [], err
    if not isinstance(got, dict) or "workflow_runs" not in got:
        # An error, not an empty list. A 200 carrying something other than this
        # endpoint's document is a lookup that did not happen, and returning `[]`
        # for it would settle the state as `none` — "no run exists" — off a reply
        # nobody understood.
        return [], "gh returned no workflow_runs document"
    return list(got.get("workflow_runs") or []), None


def _run_is_gated(run: dict) -> bool:
    return (str(run.get("status") or "").lower() in CI_GATED_RUN
            or str(run.get("conclusion") or "").lower() in CI_GATED_RUN)


def _newest_executed(runs: list[dict]) -> str:
    """'failure at 843c506' for the newest run that actually ran, else ''.

    The point of the whole exercise: when the head's own runs never executed, the
    last conclusion anybody reached is the fact that matters, and it is two clicks
    away in a place nobody looks.
    """
    for run in runs:
        conclusion = str(run.get("conclusion") or "").lower()
        if str(run.get("status") or "").lower() != "completed":
            continue
        if conclusion in CI_UNEXECUTED_RUN:
            continue
        return f"{conclusion or 'completed'} at {str(run.get('head_sha') or '')[:7]}"
    return ""


def ci_report(pr: dict, repo: str | None = None, probe: bool = True) -> CiReport:
    """The full six-state answer for one PR, asking GitHub again when it must.

    The rollup answers four of the six on its own. The fifth and sixth — "no run
    was ever created" and "a run exists and is gated" — look identical from the
    rollup, so when it comes back empty this asks the workflow-runs API, which is
    the only endpoint that can see a run that never executed.

    `probe=False` gives the rollup-only reading, which is honest but coarser: the
    empty case stays `unknown` rather than being resolved into `none` or `blocked`.
    A caller with no `gh` gets that instead of a wrong answer.
    """
    rollup = pr.get("statusCheckRollup") or []
    state = classify_rollup(rollup)
    # NOT the module's REPO fallback. That constant exists so a dashboard has
    # something to render before it has resolved anything; using it here would
    # send a probe about one repo's commit to a different repo's API and read the
    # answer as fact. A caller that cannot name the repo gets `unknown`, which is
    # this module's whole discipline applied to itself.
    repo = repo or pr.get("repo") or ""
    sha = str(pr.get("headRefOid") or "")
    branch = str(pr.get("headRefName") or "")

    if state != "unknown":
        report = CiReport(state, _ci_summary(state, len(rollup)), checks=len(rollup))
        if state != "blocked" or not probe or not repo:
            return report
        runs, err = workflow_runs(repo, branch=branch)
        last = _newest_executed(runs) if not err else ""
        return _with_block_context(report, last, err)

    if not probe:
        return CiReport("unknown", "checks unread (rollup only, no probe)",
                        reason="the check rollup is empty, which is either an "
                               "untested head or a gated run; telling them apart "
                               "needs a second call that was not made")
    if not repo:
        return CiReport("unknown", "checks unread (no repo)",
                        reason="the check rollup is empty and no repository was "
                               "named, so the workflow runs for this head could "
                               "not be looked up")
    if not sha:
        return CiReport("unknown", "checks unread (no head sha)",
                        reason="the PR carries no headRefOid, so the runs for its "
                               "head could not be looked up")

    cached = _ci_cache.get((repo, sha))
    if cached and time.time() - cached[0] < CI_PROBE_TTL:
        return cached[1]

    head_runs, err = workflow_runs(repo, sha=sha)
    if err:
        report = CiReport("unknown", f"checks unread ({err})",
                          reason=f"the check rollup is empty and the workflow runs "
                                 f"for {sha[:7]} could not be read ({err}), so "
                                 f"whether anything ran is unknown")
        _remember(repo, sha, report)
        return report

    branch_runs, berr = ([], None) if not branch else workflow_runs(repo, branch=branch)
    last = _newest_executed(branch_runs) if not berr else ""

    report = _from_runs(head_runs, sha, last, berr)
    _remember(repo, sha, report)
    return report


def _remember(repo: str, sha: str, report: CiReport) -> None:
    """Cache one answer, dropping the expired ones once the dict gets large."""
    now = time.time()
    if len(_ci_cache) >= CI_CACHE_MAX:
        for key in [k for k, (ts, _r) in _ci_cache.items() if now - ts >= CI_PROBE_TTL]:
            del _ci_cache[key]
        if len(_ci_cache) >= CI_CACHE_MAX:
            _ci_cache.clear()
    _ci_cache[(repo, sha)] = (now, report)


def _from_runs(head_runs: list[dict], sha: str, last: str, berr: str | None) -> CiReport:
    """The reading when the rollup was empty and the workflow runs answered instead.

    Note what this deliberately never returns: `green`. A head whose runs all
    completed while contributing no check runs is a head nothing verified, and
    calling that green would re-commit the error the whole module is against — an
    absent signal rendered as a good one.
    """
    gated = [r for r in head_runs if _run_is_gated(r)]
    if gated:
        return _with_block_context(
            CiReport("blocked", f"{len(gated)} run(s) created for {sha[:7]} and gated",
                     reason=f"{len(gated)} workflow run(s) for {sha[:7]} are waiting on a "
                            f"human to approve them, so they have executed nothing, "
                            f"contributed no checks and will never report on their own"),
            last, berr)
    running = [r for r in head_runs
               if str(r.get("status") or "").lower() != "completed"]
    if running:
        return CiReport("pending", f"{len(running)} run(s) under way on {sha[:7]}",
                        reason="a run exists for this head and has not reported yet")
    failed = [r for r in head_runs
              if str(r.get("conclusion") or "").upper() in CI_FAILING_CONCLUSIONS]
    if failed:
        return CiReport("red", f"{len(failed)} run(s) failed on {sha[:7]}",
                        reason=f"{len(failed)} workflow run(s) for {sha[:7]} concluded "
                               f"as failed while contributing no check runs")
    if head_runs:
        return _with_block_context(
            CiReport("unknown",
                     f"{len(head_runs)} run(s) on {sha[:7]} reported nothing",
                     reason=f"{len(head_runs)} workflow run(s) for {sha[:7]} completed "
                            f"without contributing a single check run, so what they "
                            f"verified cannot be read — this is not a pass"),
            last, berr)
    report = CiReport("none", f"no run has been created for {sha[:7]}",
                      reason=f"no workflow run exists for {sha[:7]} at all — this head "
                             f"is untested, which is not the same as passing")
    return _with_block_context(report, last, berr)


def _with_block_context(report: CiReport, last: str, err: str | None) -> CiReport:
    """Attach "and the last run that DID execute said X" to a block.

    #324's specific ask, and the half that turns a block from a shrug into a fact:
    the branch's last real conclusion is what a reader would have wanted all along.
    When the lookup itself failed the report says that instead of implying there
    was nothing to find.
    """
    if err:
        return replace(report, reason=report.reason
                       + f"; the branch's earlier runs could not be read ({err}), so "
                         f"the last executed conclusion is unknown")
    if not last:
        return replace(report, reason=report.reason
                       + "; no earlier run on this branch has executed either")
    return replace(report, last_executed=last,
                   reason=report.reason + f"; the newest EXECUTED run on this branch "
                                          f"was {last}")


def _ci_summary(state: str, checks: int) -> str:
    return {
        "green": f"green ({checks} check(s))",
        "red": f"red ({checks} check(s))",
        "pending": f"pending ({checks} check(s))",
        "blocked": f"blocked ({checks} check(s) reported, none executed)",
    }.get(state, f"{state} ({checks} check(s))")


def resolve_ci(prs: list[dict], probe: bool = True) -> list[dict]:
    """Stamp each PR with its :class:`CiReport`, under `ci`.

    Done once at fetch time rather than per renderer, so the two dashboards cannot
    drift on what a dot means and neither pays for the probe twice.
    """
    for pr in prs:
        pr["ci"] = ci_report(pr, pr.get("repo"), probe=probe)
    return prs


def ci_state(pr: dict) -> tuple[str, str]:
    """(glyph, colour) for a PR's checks, over all six states.

    Reads the report :func:`resolve_ci` stamped on the row when there is one, and
    falls back to the rollup-only reading when there is not — which now yields `?`
    rather than the quiet grey dot that let #282 rot. A caller that wants the dot
    to mean "nothing has run" has to have asked.
    """
    report = pr.get("ci")
    state = report.state if isinstance(report, CiReport) else classify_rollup(
        pr.get("statusCheckRollup"))
    return CI_GLYPHS.get(state, CI_GLYPHS["unknown"])


def ci_counts(prs: list[dict]) -> dict[str, int]:
    """{state → how many PRs}, for a panel title. Zeroes omitted by the caller."""
    counts = dict.fromkeys(CI_STATES, 0)
    for pr in prs:
        report = pr.get("ci")
        state = report.state if isinstance(report, CiReport) else classify_rollup(
            pr.get("statusCheckRollup"))
        counts[state] = counts.get(state, 0) + 1
    return counts


# ---- the plan ----------------------------------------------------------------
#
# The board's plan is what the fleet agreed to do next, in order, one list per
# repo plus a fleet-wide one. It is the half a dashboard could not show before:
# FLEET says who is here, CLAIMED says what they hold, and neither answers "and
# what is that work FOR" — the plan does, and it names the item that is next
# when nobody is on it.

PLAN_LIMIT = 200        # a plan is tens of rows by design; this is a backstop


#: The shape of an answer, so a caller that got an error still gets one it can
#: read. Every key `GET /plan` always sends, defaulted to the empty version of
#: itself — a renderer asking a dead board what is next gets "nothing", not a
#: KeyError three panels later.
EMPTY_PLAN: dict = {"items": [], "counts": {}, "order_trust": {}, "next": None,
                    "truncated": False, "plans": [], "scopes": [], "plan": None,
                    "repo": None, "exact": False, "generated": None}


def viewer_session() -> str:
    """The session id a dashboard reads the plan as. Never nothing, and never a lie.

    `GET /plan` decides `covered_by` — "this item is inside a plan somebody ELSE
    holds" — by machine when the caller does not say which session it is, because
    a bearer token proves a machine and knows nothing finer. This machine runs
    seven agents at once. So a read with no session came back with every plan
    claim taken on this box resolved as the reader's own, the panel drew that work
    with the cyan "free to take" glyph, and the duplicated work a plan claim
    exists to prevent was restored on the read path (app/api/plan.py:_covered_by).

    A dashboard holds no claims, so the honest session is one that is nobody
    else's: this process. $CLAUDE_CODE_SESSION_ID when the dashboard was started
    inside an agent's session, so that agent's own plan claim still reads as its
    own; otherwise a per-process id, which matches no claim — which is the point,
    not a shortcoming.
    """
    return ((os.environ.get("CLAUDE_CODE_SESSION_ID") or "").strip()
            or f"qb-dash:{os.getpid()}")


def fetch_plan(client, session: str | None = None) -> tuple[dict, str | None]:
    """The board's whole answer about the plan — the envelope, not just the items.

    `items` is a page of a list; the envelope is what the board CONCLUDED about
    that list, and it is all computed already: `next` (what to pick up) with its
    own `caveat` (how much that is worth), `order_trust` (who chose this order),
    `truncated` (whether you got all of it) and `counts` (how much of it is taken,
    covered, blocked and stale). Keeping `items` alone threw away six answers a
    second call could not have got back, and left both dashboards re-deriving the
    plan's order against the plan's own order.

    No repo filter, deliberately: a repo read widens to the fleet-wide items but
    still hides the other repos' lists, and this panel is called PLANS because
    the fleet runs more than one. Which list a row belongs to is the repo column.

    Never raises — a dead board is a state the panel renders, the same way
    :func:`fetch_board` treats it.
    """
    try:
        data = client.get("/plan", {"limit": PLAN_LIMIT,
                                    "session": session or viewer_session()})
    except Exception as exc:                      # noqa: BLE001 — display it, don't die
        return dict(EMPTY_PLAN), f"{type(exc).__name__}: {exc}"
    plan = {**EMPTY_PLAN, **(data or {})}
    # A key present and null is the same to a renderer as a key that is absent,
    # and only one of the two survives the merge above.
    for key, empty in (("items", []), ("counts", {}), ("order_trust", {}),
                       ("plans", []), ("scopes", [])):
        plan[key] = plan.get(key) or empty
    return plan, None


def plan_items(plan: dict | None) -> list[dict]:
    """The rows out of a `/plan` envelope. One reader, so nobody unwraps it by hand."""
    return (plan or {}).get("items") or []


def plan_holder(item: dict) -> dict | None:
    """The claim standing over this item — its own, or the plan's. None if free.

    **The one place "is anybody on this?" is decided, because it was decided four
    ways.** A plan claim is coarse and deliberate: an agent that claimed a whole
    plan said "all of this is mine", so `GET /plan` reports the item's
    `covered_by` and `next` skips it (#172). Every presentation function here read
    `claim` alone, so the panel and the TUI drew the cyan ○ "free to take" on the
    items of somebody else's held plan, sorted them into the free band, counted
    them as neither running nor blocked, and showed an idle age where the holder
    goes — advertising as free the exact work the plan claim exists to reserve.
    The truth was in the detail line, which a reader has to ask for.

    `covered_by` is already somebody ELSE's: the board resolves "mine" before it
    answers, and your own plan claim covers nothing from you — that is what lets
    the holder work through its own list item by item. "Else" is decided per
    SESSION now that :func:`fetch_plan` sends one (:func:`viewer_session`), so a
    co-tenant's held plan on this machine reads as held rather than as nobody's;
    reading by machine alone was the understatement this panel used to draw over.
    """
    return item.get("claim") or item.get("covered_by") or None


def plan_state(item: dict, next_id: str | None = None) -> tuple[str, str]:
    """(glyph, colour) for a plan row: running, held via its plan, blocked, or free.

    ▷ rather than ▶ for a covered item, because the remedy is different: ▶ is an
    agent on this line, ▷ is an agent on the whole list this line is in, and what
    a reader does about it is talk to them rather than take one item out of it.

    ◉ is the board's own `next` — the filled version of the free ○, because that
    is exactly what it is: `next` is by definition open, unclaimed, unblocked and
    uncovered, so it can only ever be a row this would otherwise draw ○. The panel
    could not point at it before, and re-deriving which row it should be is how
    the two surfaces came to disagree about the answer the board had already sent.
    """
    if item.get("claim"):
        return "▶", "green"
    if item.get("covered_by"):
        return "▷", "yellow"
    if item.get("blocked_by"):
        return "⊘", "grey50"
    if next_id and item.get("item_id") == next_id:
        return "◉", "bold cyan"
    return "○", "cyan"


def plan_ref(item: dict) -> str:
    """'#78' for an issue-backed item, 'PR#78' for a PR-backed one, '' for neither.

    Most plan items are a line of plan and nothing else; the ref is the link to
    where the *what* and the *why* live.

    **The kind is read rather than dropped.** :func:`plan_issue` refuses anything
    but an issue, so a PR-backed row draws a dim ⚒ and does nothing when it is
    clicked — and with both kinds rendered `#78` there was nothing on the row
    saying why the item beside it was takeable and this one was not. The web page
    had the same defect and answered it the same way (`refLabel`, plan.html).
    """
    ref = item.get("ref") or {}
    value = ref.get("value")
    if not value:
        return ""
    return f"PR#{value}" if ref.get("kind") == "pr" else f"#{value}"


def plan_rank(item: dict) -> tuple[str, str]:
    """(text, colour) for the rank cell — the item's place, and whether anybody chose it.

    The human's order used to reach the terminal as row position and nothing else,
    which is the one presentation that cannot distinguish a chosen priority from
    the order the adds arrived in. That is #183's complaint exactly, and the data
    to answer it (`rank`, `rank_source`) has been on every row all along.

    `~12` means rank 12 is where the item landed because appending was all the
    endpoint could do — nobody put it there. A bare `12` was placed, submitted,
    ordered or picked up by somebody. The tilde is the same mark the panel title
    counts with, so `~5 unchosen` in the title and five tildes down the column are
    one fact.

    A `picked-up` row (#427) is deliberately on the bare side of that line. It is
    at the top because an agent claimed the work, which is a position somebody's
    action decided — and the holder is already in the `who` column beside it, so
    the row says who and why without spending the rank cell on it.
    """
    rank = item.get("rank")
    if rank is None:
        return "", "grey42"
    if (item.get("rank_source") or "appended") == "appended":
        return f"~{rank}", "grey42"
    return f"{rank}", "grey70"


def plan_repo(item: dict, repos: list[str] | None = None) -> str | None:
    """The GitHub slug behind a plan item's repo, or None if it names no repo.

    A plan item's repo is free text — the fleet has lists under both
    'prisonblues/quarterback' and a bare '65lowther' — so a bare name is matched
    against the repos this dashboard watches rather than guessed at. Guessing
    would put an owner on a name that never had one, and the ⚒ would then start
    work on somebody else's issue of the same number.
    """
    repo = item.get("repo")
    if not repo:
        return None                               # fleet scope: no repo to name
    if "/" in repo:
        return repo
    for watched in (repos if repos is not None else resolve_repos()):
        if short_repo(watched) == repo:
            return watched
    return None


def plan_issue(item: dict, repos: list[str] | None = None) -> dict | None:
    """The issue behind a plan item, shaped like a `gh issue list` row — or None.

    What the ⚒ needs and all it needs: a number, and the repo it belongs to. An
    item with no ref, a `pr` ref, or a repo that cannot be resolved to a slug has
    no issue to fix and the icon stays dim.
    """
    ref = item.get("ref") or {}
    value = str(ref.get("value") or "")
    repo = plan_repo(item, repos)
    if ref.get("kind") != "issue" or not value.isdigit() or not repo:
        return None
    return {"number": int(value), "repo": repo, "title": item.get("title")}


#: The states the board counts a plan in, and what the title calls each. The
#: board's own categories, in the board's own order, because the title reports
#: the board's own numbers — `covered` split out from `claimed` is the whole
#: point of it: a blocked item needs work finishing, a covered one needs a word
#: with its holder (app/api/plan.py).
#:
#: They OVERLAP, exactly as the board's do: an item both claimed and blocked is
#: in both numbers. A local recount that quietly deduplicated would be a third
#: answer about the plan, which is the thing this file has stopped giving.
PLAN_TALLY = (("claimed", "running", "green"), ("covered", "covered", "yellow"),
              ("blocked", "blocked", None), ("stale", "stale", None))


def plan_tally(plan: dict, items: list[dict], hidden: int = 0) -> dict:
    """How much of the plan is taken, covered, blocked and stale.

    **The board's own counts whenever the panel is drawing the board's own list.**
    They are computed over the whole open set rather than over the page, they
    separate a covered item from a claimed one, and they know about `stale` — none
    of which a client can work out from a truncated page of items. The local
    recount this replaces folded covered into running, so the pane could not make
    the distinction the board keeps those two numbers apart to make.

    When a scope has hidden rows, those counts describe a list this panel is NOT
    drawing, and a title that counts rows the pane refuses to show is a title
    lying about the pane. So the same categories are recomputed over the rows that
    are left — same words, same overlaps, narrower list, and `elsewhere(hidden)`
    beside them saying so.

    A recount can only see the page. Scoped AND truncated, it therefore counts the
    rows that arrived rather than the rows that exist — which is why `truncated` is
    the one segment of the title that is never dropped: the number is qualified
    beside it rather than left to be taken for the whole.
    """
    counts = plan.get("counts") or {}
    if counts and not hidden:
        return {"open": counts.get("open", 0),
                **{key: counts.get(key, 0) for key, _, _ in PLAN_TALLY}}
    return {"open": len(items),
            "claimed": sum(1 for i in items if i.get("claim")),
            "covered": sum(1 for i in items if i.get("covered_by")),
            "blocked": sum(1 for i in items if i.get("blocked_by")),
            "stale": sum(1 for i in items if i.get("stale"))}


def plan_next_id(plan: dict) -> str | None:
    """The item id the board says to pick up next, or None."""
    return ((plan.get("next") or {}).get("item_id")) or None


def plan_next_label(plan: dict, visible: set[str] | None = None) -> str:
    """How the title names `next`, or '' when the board offers nothing.

    Repo-qualified when the row itself is not on the pane: a scope can hide the
    item `next` names, and "next #78" over a list that does not contain #78 reads
    as a rendering fault rather than as an answer about the whole plan.
    """
    nxt = plan.get("next") or {}
    if not nxt:
        return ""
    label = plan_ref(nxt) or clip(nxt.get("title"), 18)
    if visible is not None and nxt.get("item_id") not in visible:
        label = f"{short_repo(nxt.get('repo') or 'fleet')} {label}"
    return label


#: What the title gives up first when it does not fit, in the order it gives them
#: up. The tally's detail goes before its total, and `next`, `truncated` and the
#: open count never go: one is the answer a reader came for, one is a statement
#: that the rows are not all of them, and the third is what the rows are.
PLAN_HEAD_DROP = ("stale", "blocked", "covered", "running", "unchosen")


def plan_head_bits(plan: dict, items: list[dict], hidden: int = 0,
                   room: int | None = None) -> list[tuple[str, str | None]]:
    """The PLANS title as (text, colour) segments — one title, two renderers.

    Built here rather than twice, because the two panels had already drifted on
    the two numbers they did share, and this adds five more: the split counts,
    how much of the order nobody chose, what the board says is next, and whether
    the page is all of it.

    **The title is where the envelope goes, and that is a decision about rows.**
    #269 measured 55 rows drawn into a 38-row pane with a whole panel below a fold
    nothing can scroll to, so an answer worth a line of its own is an answer that
    pushes another panel off the screen. Every one of these facts is about the
    list as a whole, which is what a title is for; nothing here adds a row.
    """
    tally = plan_tally(plan, items, hidden)
    bits: list[tuple[str, str | None]] = [(f"{tally['open']} open", None)]
    bits += [(f"{tally[key]} {word}", colour)
             for key, word, colour in PLAN_TALLY if tally.get(key)]
    # #183's minimum fix, on the surface that could not show it: how many of these
    # positions nobody chose. Silent when the whole order was chosen, because that
    # is the state a reader needs no warning about — the per-row tilde is where
    # "always present" lives.
    #
    # Recounted over the visible rows when a scope has hidden some, for the reason
    # `plan_tally` recounts: the board's number is about the whole plan, and a
    # title claiming five unchosen positions over two tildes on screen sends the
    # reader looking for three rows that are not there.
    unchosen = (plan.get("order_trust") or {}).get("unchosen") or 0
    if hidden:
        unchosen = sum(1 for i in items
                       if (i.get("rank_source") or "appended") == "appended")
    if unchosen:
        bits.append((f"~{unchosen} unchosen", None))
    label = plan_next_label(plan, {i.get("item_id") for i in items})
    if label:
        bits.append((f"next {label}", "cyan"))
    if plan.get("truncated"):
        bits.append((f"truncated at {len(plan_items(plan))}", "red"))
    return _fit_head(bits, room)


def _head_len(bits: list[tuple[str, str | None]]) -> int:
    return len(" · ".join(text for text, _ in bits))


def _fit_head(bits: list[tuple[str, str | None]],
              room: int | None) -> list[tuple[str, str | None]]:
    """Drop the least load-bearing segments until the title fits its panel.

    A panel title is clipped at the border and clipped from the END — so a title
    that overflows loses `next` and `truncated`, which are the two answers this
    line exists to carry, and loses them without saying so. Dropping a segment
    nobody would miss says the same thing in less room; being cut off mid-word at
    "· trunca" does not.
    """
    if room is None:
        return bits
    for word in PLAN_HEAD_DROP:
        if _head_len(bits) <= room:
            return bits
        bits = [(text, colour) for text, colour in bits if not text.endswith(word)]
    # Then the count of what was truncated, which is the detail rather than the
    # fact. "truncated" alone still says the rows are not the whole list.
    if _head_len(bits) > room:
        bits = [("truncated", colour) if text.startswith("truncated") else (text, colour)
                for text, colour in bits]
    # And a pane too narrow even for that gives up whole segments rather than
    # having the last one cut off in the middle of a word. `next` is what survives
    # to the end: on a pane with room for one answer, it is the one worth having.
    for word in ("truncated", "open"):
        if _head_len(bits) <= room:
            break
        bits = [(text, colour) for text, colour in bits if not text.endswith(word)]
    return bits


def plan_who(item: dict) -> tuple[str, str]:
    """(text, colour) for the right-hand column: who has it, or what it waits on.

    Three different facts share one column because only one of them is ever true
    of a row: a taken item has a holder, a blocked one has something to wait for,
    and a free one has only how long it has been sitting there.

    "Taken" includes an item inside somebody else's held plan, and the holder is
    the whole point of showing it: the column read as an idle age — "4d", the
    strongest possible invitation to pick something up — over work another agent
    had reserved.

    **The machine stays on.** `machine/name` is the whole identity and the name
    half alone is not unique: names are short, memorable and RECYCLED when an
    agent finishes, so two agents on two boxes read as one agent — and the fleet
    runs several boxes precisely so that they can work the same repo at once.

    **A held row that is also blocked says both**, with the ⊘ the state column
    uses for the same fact. Only one of the three could ever be true at a time was
    the premise, and it is false for exactly this pair: an agent holding an item
    that waits on something else is stuck, which is the one combination a reader
    would want to do something about.
    """
    holder = plan_holder(item)
    if holder:
        waits = "⊘" if item.get("blocked_by") else ""
        return waits + (holder.get("holder") or "?"), "yellow"
    blockers = item.get("blocked_by") or []
    if blockers:
        first = blockers[0].get("ref")
        return (f"waits #{first}" if first else f"waits ×{len(blockers)}"), "grey50"
    return ago(item.get("updated")), "grey50"


def claim_label(key: str, plan: list[dict] | None = None,
                scope: Scope | None = None) -> str:
    """What a claim is ON, in words a human can read off a pane.

    A claim on a board object is keyed by uuid — ``item:<id>`` for one line of the
    plan, ``plan:<id>`` for a whole one: right for a lock, useless on a screen,
    where 36 hex characters say only "something on the plan". Given the plan, the
    item's title or the plan's label goes in instead. Without it the raw key
    stays, because a key nobody can resolve still beats a blank.

    **Both prefixes, because #172 moved them.** An ITEM claim used to be keyed
    ``plan:<uuid>`` and is now ``item:<uuid>``, while ``plan:`` became the
    whole-plan claim that release added — so the old test matched a plan id
    against item ids, which cannot ever be equal, and the CLAIMED pane showed a
    bare uuid for every claim the new plan takes.

    The scope trims the repo off the front for the same reason the panels drop
    their repo column (#261): on a pane showing one project, ``quarterback#209``
    spends twelve columns to say ``#209``. Only when the scope is that one project
    — the wide view keeps the repo, because there it is what tells two claims apart.

    And the OWNER survives where two watched repos share a name. CLAIMED has no
    repo column for the scope to restore — its three are who/key/left — so if the
    owner goes too, a fork's claim and its upstream's both read ``quarterback#3``,
    which is the ambiguity the slug comparison exists to remove, reintroduced one
    panel further on.
    """
    key = key or "?"
    prefix, _, wanted = key.partition(":")
    if not wanted or prefix not in ("item", "plan"):
        # `acme/widget:feat/x` is a branch and `acme/widget#5` an issue: both are
        # already readable, and neither is a uuid to look up — but both wear the
        # repo the pane may already be scoped to, so they go through the trim.
        return _scoped_key(key, scope)
    for item in (plan or []):
        if prefix == "item" and item.get("item_id") == wanted:
            head = " ".join(x for x in ("plan", plan_ref(item)) if x)
            return f"{head} {item.get('title') or '?'}"
        row = item.get("plan") or {}
        if prefix == "plan" and row.get("plan_id") == wanted and row.get("label"):
            # The plan's own claim, over every item under that label. Named as the
            # plan rather than as one of its items: it is not the first row's
            # work, it is all of it.
            return f"plan {row['label']}"
    return _scoped_key(key, scope)


def _scoped_key(key: str, scope: Scope | None) -> str:
    """A claim key shortened as far as the scope allows, and no further.

    Two watched repos sharing a bare name is the one case where even the owner is
    load-bearing, so the key comes back whole: ``short_key`` would drop it and put
    a fork's claim and its upstream's both at ``quarterback#3``.
    """
    if scope is not None and len(scope.names) != len(scope.keys):
        return key
    return _unprefixed(short_key(key), scope)


def _unprefixed(label: str, scope: Scope | None) -> str:
    """``quarterback#209`` → ``#209``, and ``quarterback:2.40`` → ``2.40``.

    Only against the repo the pane is scoped to, and only when that is a single
    repo: trimming a name the header does not state would leave a bare ``#209``
    with nothing anywhere saying whose #209 it is.
    """
    if scope is None or scope.column or len(scope.names) != 1:
        return label
    name = next(iter(scope.names))
    lowered = label.lower()
    if lowered.startswith(f"{name}#"):
        return label[len(name):]                  # the '#' stays: it reads as an issue
    if lowered.startswith(f"{name}:"):
        return label[len(name) + 1:]              # the ':' does not: it read as a namespace
    return label


def plan_detail(item: dict, envelope: dict | None = None) -> str:
    """The whole of a plan row, for the detail line under the tables.

    The note is why this is worth a click: it is the reasoning behind the item's
    place in the order, it exists nowhere else — not in the issue, not on the
    board tape — and it does not fit in a 44-column title cell.

    The rest of what a row cannot carry goes here for the same reason: where the
    item sits and who put it there, how long it has sat, when the claim on it
    lapses, and — for the one row the board named `next` — the board's own caveat
    about how much that recommendation is worth. `next.caveat` is a sentence; the
    title has room for the count and this has room for the argument.
    """
    bits = [f"{short_repo(item.get('repo') or 'fleet')} {plan_ref(item)}".strip(),
            item.get("title") or "(untitled)"]
    if item.get("rank") is not None:
        source = item.get("rank_source") or "appended"
        where = f"rank {item['rank']} ({source}"
        if item.get("placed_for"):
            where += f" {item['placed_for']}"
        bits.append(where + ")")
    plan = item.get("plan") or {}
    if plan.get("label"):
        bits.append(f"[{plan['label']}]")
    claim = item.get("claim")
    if claim:
        held = f"held by {claim.get('holder') or '?'}"
        if claim.get("expires"):
            held += f", {until(claim['expires'])} left"
        if claim.get("note"):
            held += f" — {claim['note']}"
        bits.append(held)
    covered = item.get("covered_by")
    if covered and not claim:
        # A plan-level claim over an item nobody has taken individually. Said
        # differently from "held", because the remedy is: the whole plan is
        # somebody's, so talk to them rather than taking one line out of it.
        held = f"in {plan.get('label') or 'a plan'} held by {covered.get('holder') or '?'}"
        if covered.get("note"):
            held += f" — {covered['note']}"
        bits.append(held)
    blockers = item.get("blocked_by") or []
    if blockers:
        bits.append("waits on " + ", ".join(
            f"{b.get('ref') and '#' + str(b['ref']) or ''} {b.get('title') or ''}".strip()
            for b in blockers))
    if item.get("stale"):
        bits.append(f"stale, idle {item.get('idle_days')}d")
    if item.get("added_by"):
        bits.append(f"added by {item['added_by']}")
    if item.get("note"):
        bits.append(item["note"])
    # The caveat belongs to `next` and to no other row: it is the board's
    # statement of how much ITS recommendation is worth, and pinning it to any
    # other item would read as a warning about that item.
    # `envelope` and not `plan`: the local `plan` here is the item's own plan ROW
    # (the label it sits under), which is a different thing from the /plan answer.
    caveat = ((envelope or {}).get("next") or {}).get("caveat")
    if caveat and plan_next_id(envelope or {}) == item.get("item_id"):
        bits.append(caveat)
    return clip(" · ".join(bits), 400)


# ---- the dials in force ------------------------------------------------------
#
# A dial is a setting: the repo supplies a default, the BOARD states the value in
# force, and the layer that answered is part of the answer (#305). Until #477
# nothing a person or an agent looks at showed one. `GET /dials` was written and
# read by exactly two things — a browser endpoint and one function in
# `panel_seats.py` — so the value governing every round on the fleet was invisible
# on `qb-dash`, `qb-dash-tui`, `qb-board` and the web board alike.
#
# That was tolerable while a dial only configured what a review round costs. It
# stops being tolerable with `tempo` (#474), which is the answer to "is this fleet
# working right now, and how hard" — and that is a fact which has to be legible
# from the place the fleet is driven from, which is a terminal.
#
# **This file does not learn the vocabulary, and that is not laziness.** The board
# stores `dial` as opaque text and `value` as opaque JSON because the harness owns
# the dial table (`harness/loops/harness_rules.py`), and a copy anywhere else is a
# second place a dial is written down — #56's rule, and the confusion #305 exists
# to end. So everything below renders what the board said and asserts nothing
# about what it MEANS: `tempo` gets a cell of its own because the issue asks for
# one, not because this module knows what an `eager` is.

#: The dial the fleet's throttle lives on (#474). Named here for one reason: it is
#: the dial that gets a cell of its own on the header line, next to the caps it is
#: there to protect. Its VALUES are not named here, and must not be — a screen that
#: knew the rungs would be a second copy of the ladder.
TEMPO_DIAL = "tempo"

#: The shape of an answer, so a caller that got an error still gets one it can
#: read — `EMPTY_PLAN`'s argument, for the same reason.
EMPTY_DIALS: dict = {"dials": [], "shadowed": [], "error": None, "asked": False,
                     "now": None}


def fetch_dials(client, repos: list[str] | None = None) -> dict:
    """What the board says is in force, for the repos this screen watches.

    One call per repo, because `GET /dials?repo=X` answers with X's own dials AND
    the fleet-wide ones — which is the join a reader wants and a screen watching
    two projects has to make twice. A screen that resolved no repo at all asks
    once with no scope, which returns the fleet rows: the honest answer for a
    dashboard that cannot say which project it is in.

    Reading is free with the credential this dashboard already holds: `GET /dials`
    takes `app.auth.reader`, which a machine bearer token passes. Writing is not,
    and that asymmetry is the whole shape of #477 — see :func:`dials_url`.

    Never raises. A board that will not answer is `error`, and `asked` stays True
    so that a caller can tell "no dials are set" from "nobody has asked yet": the
    first is a state worth drawing and the second is a screen that has not
    finished starting, and #244's rule is that those must not look alike.
    """
    out: dict = {**EMPTY_DIALS, "asked": True}
    scopes = [r for r in (repos if repos is not None else resolve_repos()) if r] or [None]
    rows: list[dict] = []
    seen: set[tuple] = set()
    for scope in scopes:
        try:
            got = client.get("/dials", {"repo": scope} if scope else None)
        except Exception as exc:                  # noqa: BLE001 — display it, don't die
            out["error"] = f"{type(exc).__name__}: {exc}"
            break
        # The BOARD's clock, kept for anything that has to write a time against
        # it. "In four hours" computed from this machine's clock is four hours
        # from whatever this machine believes, and a box whose clock is an hour
        # slow has its expiry refused as being in the past — a validation error
        # about a field nobody typed. `app/static/dials.html` corrects the same
        # way from the same field.
        out["now"] = (got or {}).get("now") or out.get("now")
        for row in (got or {}).get("dials") or []:
            # Two repos' answers both carry the fleet rows. Keyed on (repo, dial)
            # rather than on identity because that pair is what the board's own
            # `ix_dial_settings_live` holds unique: one live row per scope per
            # dial, so a duplicate here is the same row arriving twice.
            key = (row.get("repo"), row.get("dial"))
            if key in seen:
                continue
            seen.add(key)
            rows.append(row)
    out["dials"], out["shadowed"] = dials_in_force(rows, scopes)
    return out


def dials_in_force(rows: list[dict], repos: list[str | None] | None = None
                   ) -> tuple[list[dict], list[dict]]:
    """Split the board's rows into the ones in force and the ones overridden.

    **A repo dial beats a fleet dial of the same name**, and that precedence is
    the client's to apply — the board says so in one line and deliberately does
    not do it, because a repo read returns both scopes so that ONE call answers
    "what is in force here".

    A fleet row is shadowed only where every repo on this screen overrides it. A
    screen watching two projects, one of which sets `review_panel.max_rounds` for
    itself, still has that fleet row in force in the other — and a panel that
    dropped it would be reporting the first repo's answer as the fleet's.

    Ordered repo-first then by name, which is the order `GET /dials` returns and
    the order a reader would write the table in.
    """
    scopes = [r for r in (repos or []) if r]
    overridden: dict[str, set[str]] = {}
    for row in rows:
        if row.get("repo"):
            overridden.setdefault(row.get("dial") or "", set()).add(row["repo"])
    in_force, shadowed = [], []
    for row in rows:
        beaten = (not row.get("repo") and scopes
                  and set(scopes) <= overridden.get(row.get("dial") or "", set()))
        (shadowed if beaten else in_force).append(row)
    return sorted(in_force, key=_dial_order), sorted(shadowed, key=_dial_order)


def _dial_order(row: dict) -> tuple:
    """Repo before fleet, then by name — `GET /dials`' own order, and the order a
    reader would write the table in."""
    return (row.get("repo") is None, row.get("dial") or "")


def dial_of(dials: dict | None, name: str, repo: str | None = None) -> dict | None:
    """The row in force for one dial, or None. Repo scope first, then fleet.

    The precedence is applied again here rather than trusted to the sort, because
    a caller asking for one dial by name is asking which value ANSWERS — and with
    two rows for it in the list, the wrong one is a plausible answer rather than
    a visible bug.
    """
    rows = [d for d in (dials or {}).get("dials") or [] if d.get("dial") == name]
    if repo:
        rows.sort(key=lambda d: d.get("repo") != repo)
    else:
        rows.sort(key=lambda d: d.get("repo") is not None)
    return rows[0] if rows else None


def dial_value(row: dict | None, width: int = 24) -> str:
    """A dial's value, rendered without claiming to know what it means.

    JSON spellings throughout — `true`, `false`, `null` — rather than a friendlier
    `on`/`off`, because the friendly words are a vocabulary and this file does not
    have one. `null` in particular is a real setting on three dials (the documented
    off switch for `max_fix_growth`, `distant_merge_lines` and
    `escalate_on.premise_repeated`), and the board goes to some trouble to keep it
    apart from "no row at all"; rendering it as blank would put the two back
    together on the one screen a person reads.
    """
    if row is None:
        return ""
    value = row.get("value")
    if isinstance(value, str):
        text = value
    elif isinstance(value, bool) or value is None:
        text = {True: "true", False: "false", None: "null"}[value]
    elif isinstance(value, (int, float)):
        text = f"{value}"
    else:
        text = json.dumps(value, separators=(",", ":"))
    return clip(text, width)


#: What an indefinite dial's remaining-life cell says. **Not** `until()`'s `—`,
#: which every other panel uses for a value nobody reported: an expiry that was
#: never set is a decision somebody made, and the opposite of an unknown one.
DIAL_NO_END = "no end"


def dial_life(row: dict | None) -> tuple[str, str]:
    """How long this dial has left, and how loudly to say it — (text, style).

    **The half that must not be dropped.** A `tempo: eager` with forty minutes on
    it and a `tempo: eager` set indefinitely are different situations and they must
    not render identically — #244's rule (being idle and being broken must not look
    alike) applied to a switch instead of a queue.

    Which is why the INDEFINITE one is the loud cell and the countdown is the quiet
    one, rather than the other way about. A dial that expires will take itself off
    the board with nobody remembering it; a dial with no end stays until a person
    goes and clears it, and the failure mode of the whole layer is a temporary
    setting that outlived its reason with nothing saying it is still in force.
    """
    if row is None:
        return "", "grey50"
    if not row.get("expires_at"):
        return DIAL_NO_END, "yellow"
    left = until(row.get("expires_at"))
    mins = minutes_left(row.get("expires_at"))
    return left, "grey50" if mins is None or mins >= 10 else "grey62"


def tempo_cell(dials: dict | None, repo: str | None = None
               ) -> tuple[str, str, str, str] | None:
    """The header cell: ("TEMPO", value, life, colour) — or None before the first ask.

    Four states and no two of them alike, because collapsing any pair is the bug
    this issue is about:

      * **not asked yet** — None, and the caller draws nothing. A screen that
        printed "unset" while its first fetch was still in flight would be stating
        something it did not know.
      * **the board would not answer** — `?`, in the colour of a thing that is
        wrong. Not "unset": an unreadable dial is not an absent one.
      * **no dial set** — `unset`, quietly. The harness's own default governs, and
        this screen does not know what that is (see the section note).
      * **a dial in force** — its value, and its life beside it.
      * **more than one answer** — see below. Only reachable on a screen watching
        several projects, and only where they disagree.

    `repo` is the question "what is the tempo HERE", and with it given there is one
    answer by construction: that repo's row, or the fleet's behind it. Without it
    the screen is asking about everything it watches, and everything it watches can
    disagree — which is the case this cell must not paper over.
    """
    if not (dials or {}).get("asked"):
        return None
    if (dials or {}).get("error"):
        return "TEMPO", "?", "", "red"
    live = [d for d in (dials or {}).get("dials") or [] if d.get("dial") == TEMPO_DIAL]
    if not live:
        return "TEMPO", "unset", "", "grey50"
    row = dial_of(dials, TEMPO_DIAL, repo)
    life, _ = dial_life(row)
    if repo or len(live) == 1:
        return "TEMPO", dial_value(row, 12), life, "cyan"
    # SEVERAL ANSWERS, and the cell has room for one. Printing either of them would
    # be this panel's own defect — one layer's value stated as though it were
    # everybody's — so it says how many there are and leaves the panel below to say
    # which is which.
    #
    # **The expiry counts as a disagreement, not just the value.** Two repos both
    # `eager`, one for forty minutes and one indefinitely, are the pair this whole
    # issue says must not render alike; agreeing on the word and then showing one
    # of the two countdowns beside it is that failure with an extra step. So a
    # split expiry keeps the value — it IS agreed — and gives up the life cell,
    # which is the half that has no single answer.
    if len({json.dumps(d.get("value"), sort_keys=True) for d in live}) > 1:
        return "TEMPO", "mixed", f"{len(live)} repos", "yellow"
    if len({d.get("expires_at") for d in live}) > 1:
        return "TEMPO", dial_value(row, 12), f"{len(live)} repos", "yellow"
    return "TEMPO", dial_value(row, 12), life, "cyan"


def parse_dial_value(text: str):
    """What a person typed, as the VALUE it looks like — JSON where it parses.

    `2`, `true`, `null` and `["a","b"]` are values several dials document, and a
    `max_rounds` of `"2"` is a dial the harness refuses to apply and reports by
    name — a puzzle handed to somebody at a keyboard. Anything that is not JSON is
    the string it looks like, because `P3` and `eager` are values too and
    demanding quotes round them would make the common case the fiddly one.

    `app/static/dials.html` implements this same rule in JavaScript, deliberately
    and unavoidably twice: one is a browser and one is a terminal. They are kept
    honest by `tests/test_dials_page.py`, which asserts the page's half, and by
    this function's tests, which assert the same table of inputs.
    """
    raw = (text or "").strip()
    if raw == "":
        return ""
    try:
        return json.loads(raw)
    except ValueError:
        return raw


#: How long a written expiry may be, and the units a person types. Deliberately
#: not seconds: a dial is set for an afternoon or for a fortnight, and "14400" is
#: a number somebody has to work out.
#: DIGITS ARE BOUNDED, and not for tidiness: `timedelta` raises OverflowError —
#: not ValueError — past about 2.7 million days, so `99999999999999999999d`
#: escapes a caller that catches the documented failure and lands in a UI
#: callback as a crash. Six digits is 2739 years: past every legitimate use and
#: short of every overflow.
_EXPIRY_RE = re.compile(r"^\s*(\d{1,6})\s*([mhd])\s*$", re.I)
_EXPIRY_UNITS = {"m": 60, "h": 3600, "d": 86400}


def parse_dial_expiry(text: str, now: str | None = None) -> str | None:
    """`4h` → an ISO timestamp four hours from the BOARD's now. Blank → None.

    None means indefinite, which is a real answer and the one the board stores as
    "until somebody clears it" — so a blank box is not a missing value here.

    Measured from `now` when the board supplied one (:func:`fetch_dials` keeps
    it). A clock an hour slow otherwise writes "in one hour" as a time already
    past, which `POST /dials` refuses at the door — correctly, and in words about
    a field the person never filled in.
    """
    raw = (text or "").strip()
    if not raw:
        return None
    match = _EXPIRY_RE.match(raw)
    if not match:
        raise ValueError(f"{raw!r} is not a duration — try 30m, 4h or 7d, "
                         "or leave it empty for a dial with no end")
    seconds = int(match.group(1)) * _EXPIRY_UNITS[match.group(2).lower()]
    if seconds <= 0:
        raise ValueError("an expiry of zero is a dial that is absent the moment "
                         "it is written — leave it empty for no end")
    base = datetime.now(timezone.utc)
    if now:
        try:
            base = datetime.fromisoformat(now.replace("Z", "+00:00"))
        except ValueError:
            pass                                  # a board too old to send one
    return (base + timedelta(seconds=seconds)).isoformat()


def dial_where(row: dict, show_repo: bool = True) -> tuple[str, str]:
    """Which LAYER answered — ("fleet" | the repo, style).

    The one column on these panels that does not answer to the screen's scope,
    and deliberately: everywhere else the repo cell names a PROJECT, and a screen
    showing one project spends eleven columns restating it (#261). Here it names
    the layer a value came from, which is half of what a dial's answer IS — "in
    force fleet-wide" and "in force for this repo" are different facts about the
    same number, and a reader who cannot tell them apart cannot tell whether
    clearing it changes one project or all of them.

    So the word `repo` stands in for the name on a single-project screen, where
    the name is already in the header and only the layer is news.
    """
    repo = row.get("repo")
    if not repo:
        return "fleet", "cyan"
    return (clip(short_repo(repo), 11) if show_repo else "repo"), "grey62"


def dial_detail(row: dict) -> str:
    """The whole of a dial, for the detail line under the tables.

    Worth a click because the row is six narrow cells and the argument does not
    fit in one of them. The board REQUIRES a reason on every write for exactly
    this reason — "a dial nobody can read an argument for is a dial nobody can
    decide to remove" — so a surface that renders the value and drops the reason
    keeps the setting and throws away the only thing that lets anybody undo it.

    The expiry is spelled out rather than counted down. `no end` in a six-column
    cell is the fact; "set indefinitely — nothing will clear this but a person" is
    what the fact MEANS, and that sentence is the whole of why the two states must
    not render alike.
    """
    bits = [f"{row.get('dial') or '?'} = {dial_value(row, 200)}"]
    bits.append("fleet-wide" if not row.get("repo") else f"for {row['repo']}")
    if row.get("expires_at"):
        left = until(row.get("expires_at"))
        bits.append(f"expires in {left}" if left != "—" else "expired")
    else:
        bits.append("set indefinitely — nothing will clear this but a person")
    who = " ".join(x for x in ("set by", row.get("set_by")) if x)
    # HOW, when the board recorded one. The identity is the same by either door,
    # so this is the half that says which — a browser the edge vouched for, or a
    # key on a workstation (#479). Absent is a row older than the column, and it
    # is left out rather than guessed at.
    via = {"edge": "in a browser", "key": "with a key",
           "dev": "via the dev bypass"}.get(row.get("set_via"), row.get("set_via"))
    if via:
        who += f" {via}"
    when = ago(row.get("set_at"))
    bits.append(f"{who} {when} ago" if when else who)
    if row.get("reason"):
        bits.append(row["reason"])
    return clip(" · ".join(b for b in bits if b), 400)


def dials_url(cfg, repo: str | None = None) -> str:
    """The board's dials page — the OTHER surface, not the only one any more.

    Two things want this URL and neither is a workaround. A person comparing
    projects wants the page, because it shows every repo's dials at once where
    the panel shows the screen's own. And a host with no human key wants it,
    because that is the whole of what the dashboard could do before #477's second
    half — the panel reads, and names the door.

    **Which is #443's option (3), and it is still carrying weight.** That issue's
    three shapes were: the client holds an edge session; a credential distinct
    from the machine token; or read-only plus a printed URL. The dashboard now has
    (2) — `X-Human-Key`, a person's key presented to the agent host, no Authelia —
    and (3) is what it degrades to when the key is absent, which is every box that
    has not been given one. #443 is also why the fallback names the URL rather
    than implying it: it is the record of a person told the reorder was theirs to
    do, in a terminal, whose reply was "i don't know how to re-order".

    **(1) was built and thrown away, and the reason is worth keeping.** An earlier
    cut of `HumanClient` held a signed-in Authelia session and posted to the
    browser vhost. Three things were wrong with it and any of them is fatal: the
    holder BECOMES the person, so every human-only endpoint opens at once rather
    than the ones being asked for; the session is SSO for a whole estate; and it
    expires on a wall clock, so the `✎` would have died whenever it lapsed and
    stayed dead until somebody re-minted it by hand. #479 records the reversal.
    """
    #: The repo rides along so a reader arriving from a terminal lands on the scope
    #: the terminal was showing rather than on the fleet's. A screen watching
    #: several sends none: there is one box on that page and it would have to pick
    #: one of them, which is a worse answer than letting the page ask.
    base = f"{getattr(cfg, 'base_url', '') or ''}/dials/view"
    watched = resolve_repos() or []
    scope = repo if repo is not None else (watched[0] if len(watched) == 1 else None)
    return f"{base}?repo={urllib.parse.quote(scope)}" if scope else base


# ---- the tmux screen ---------------------------------------------------------
# The dashboard reads the seats off tmux rather than off the board, because they
# are different questions. The board knows which AGENTS are live anywhere on the
# fleet; this knows which PANES are on the screen in front of you, including the
# ones whose agent has exited and left a shell behind. Only the second can be
# closed with a click.

SEAT_FIELDS = ("pane", "seat", "session", "window", "command", "path", "repo", "scope")


def tmux_seats() -> list[dict]:
    """Every seat pane on this tmux server, lowest seat number first.

    A seat is a pane carrying the @qb_seat option, which is how qb-seats marks
    them and the only handle that survives a pane being added or closed — the
    index shifts and the agent rewrites the title.

    `@qb_repo` and `@qb_scope` come back with it because `list-panes -a` is the
    whole SERVER and not this screen: since #208 two screens can each have a seat
    1, so the number alone no longer says which board identity a pane is. Both are
    set on the SESSION (or, for `--add`, on the pane) and formats resolve a user
    option up through the hierarchy, so every pane of a screen answers for its own
    screen. Either can be empty — `@qb_scope` whenever the screen was not given an
    explicit one, `@qb_repo` on a screen built by a qb-seats old enough not to set
    it — which is why the dashboard falls back to matching on the number.

    Returns [] rather than raising when there is no tmux, no server, or no
    screen: the dashboard runs inside the screen most of the time and in a bare
    terminal the rest, and an empty SEATS panel is the honest answer to the
    second case.
    """
    if not os.environ.get("TMUX"):
        return []
    fmt = "\t".join("#{%s}" % f for f in
                    ("pane_id", "@qb_seat", "session_name", "window_index",
                     "pane_current_command", "pane_current_path", "@qb_repo",
                     "@qb_scope"))
    try:
        got = subprocess.run(["tmux", "list-panes", "-a", "-F", fmt],
                             capture_output=True, text=True, timeout=5)
    except Exception:                             # noqa: BLE001
        return []
    if got.returncode != 0:
        return []
    seats = []
    for line in got.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) != len(SEAT_FIELDS) or not parts[1]:
            continue
        seats.append(dict(zip(SEAT_FIELDS, parts)))
    # By seat NUMBER, not by pane order: --add splits off the leftmost pane, so
    # pane order runs 1, 3, 2 on a screen that has had a seat added to it. By
    # SCREEN first, because this lists the whole server and two screens now
    # interleave their 1, 2, 3 otherwise — which reads as one screen with every
    # seat number twice.
    return sorted(seats, key=lambda s: (s["session"],
                                        int(s["seat"]) if s["seat"].isdigit() else 0))


# ---- claude code's own limits ------------------------------------------------
#
# The seats all bill to ONE subscription, so the ceiling every one of them is
# working towards is a single fleet-wide number — and it is the one fact this
# dashboard could not previously show. Six seats making a plan happen in
# parallel is exactly the way to spend a five-hour window in forty minutes, and
# the human at the screen finds out by watching an agent stop.
#
# The figures come from the same endpoint `/usage` reads, so what this shows and
# what a seat says about itself cannot disagree. It is the subscription's usage,
# not this session's: there is no per-session budget to report.
#
# NO TOKEN, NO LINE. An API-key install has no subscription limits to report and
# a missing credentials file is that case, not a failure — it returns nothing to
# show and no error, and the header simply has one line fewer.

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
# THREE MINUTES, AND THAT IS ALREADY GENEROUS. The endpoint rate-limits harder
# than a dashboard's instincts suggest — five calls inside ten minutes was enough
# to get a 429 while this was being written. A five-hour window moves about a
# percent in three minutes, so nothing legible is being given up.
LIMITS_EVERY = 180.0    # seconds between calls, across every pane on the machine
BACKOFF = 600.0         # …and this long once the endpoint has said slow down
STALE_AFTER = 600.0     # past this the line admits its figures are old


def _oauth_token() -> str | None:
    """Claude Code's own OAuth access token, or None if this install has none.

    Read fresh every time rather than cached: Claude Code refreshes the token in
    place, and a dashboard that cached one at startup would start 401ing after
    an hour with nothing to say about why.
    """
    env = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
    if env:
        return env
    home = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
    try:
        with open(os.path.join(home, ".credentials.json"), encoding="utf-8") as fh:
            return (json.load(fh).get("claudeAiOauth") or {}).get("accessToken") or None
    except Exception:                             # noqa: BLE001 — absent, or not ours to read
        return None


def _limit_label(row: dict) -> str:
    """What to call a limit in two or three columns.

    The kinds are the endpoint's, and the scoped one names its own model: a
    weekly cap that applies to Opus alone reads 'Opus', because 'weekly_scoped'
    is not a thing anybody is watching for.
    """
    kind = row.get("kind") or ""
    if kind == "session":
        return "5h"
    if kind == "weekly_all":
        return "7d"
    if kind == "weekly_scoped":
        scope = (row.get("scope") or {}).get("model") or {}
        return scope.get("display_name") or "7d*"
    return kind[:6] or "?"


# THE CACHE IS ON DISK BECAUSE THE POLLERS ARE SEPARATE PROCESSES. A developer
# running three seat screens has three dash panes, each its own process, each
# with its own timer — and the endpoint rate-limits, which is not a guess: it
# answered 429 while this was being built. A per-process interval cannot fix
# that, so the interval is enforced where they can all see it. One file, last
# answer plus the time the next call is allowed.

def _cache_path() -> str:
    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return os.path.join(base, "quarterback", "limits.json")


def _read_cache() -> dict:
    try:
        with open(_cache_path(), encoding="utf-8") as fh:
            got = json.load(fh)
        return got if isinstance(got, dict) else {}
    except Exception:                             # noqa: BLE001 — no cache is a cold start
        return {}


def _write_cache(cache: dict) -> None:
    """Atomically, because the other dash panes are reading it as this one writes."""
    path = _cache_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(cache, fh)
        os.replace(tmp, path)
    except Exception:                             # noqa: BLE001 — a cache is an optimisation
        pass


def _get_usage(token: str) -> tuple[list[dict], str | None]:
    req = urllib.request.Request(USAGE_URL, headers={
        "Authorization": f"Bearer {token}",
        "anthropic-beta": "oauth-2025-04-20",
    })
    try:
        with urllib.request.urlopen(req, timeout=15, context=_ssl_context()) as resp:
            return parse_limits(json.loads(resp.read().decode())), None
    except urllib.error.HTTPError as exc:
        return [], f"HTTP {exc.code}"             # the code, or 429 is invisible
    except Exception as exc:                      # noqa: BLE001
        return [], type(exc).__name__


def _cached(cache: dict, now: float, err: str | None = None) -> tuple[list[dict], str | None]:
    limits = cache.get("limits") or []
    if now - float(cache.get("at") or 0) > STALE_AFTER:
        return limits, err or "stale"
    return limits, (None if limits else err)


def fetch_limits() -> tuple[list[dict], str | None]:
    """[{label, percent, resets, severity}, …] — the caps the whole fleet shares.

    Never raises, and never blanks a good answer. Returns ([], None) when there
    is nothing to report at all (no token, so no subscription caps), and the
    LAST figures with an error beside them when a call fails — they are minutes
    old and still roughly true, which is more use than an empty line.
    """
    token = _oauth_token()
    if not token:
        return [], None
    cache = _read_cache()
    now = time.time()
    if now < float(cache.get("next") or 0):
        return _cached(cache, now)                # somebody else asked recently enough
    limits, err = _get_usage(token)
    if err is None:
        _write_cache({"at": now, "limits": limits, "next": now + LIMITS_EVERY})
        return limits, None
    # A 429 means back further off, not try again in a minute: the failing call
    # is itself the thing being rate limited, and three panes retrying on the
    # short clock is how a warning becomes a wall.
    cache["next"] = now + (BACKOFF if err == "HTTP 429" else LIMITS_EVERY)
    _write_cache(cache)
    return _cached(cache, now, err)


def parse_limits(data: dict) -> list[dict]:
    """The endpoint's answer, reduced to what fits on one line.

    Split out from the fetch so the shaping is testable without a network, and
    because the endpoint carries a dozen fields per cap of which this shows
    three.
    """
    out = []
    for row in data.get("limits") or []:
        percent = row.get("percent")
        if percent is None:
            continue
        percent = int(round(percent))
        # A scoped cap nobody has touched is noise; the two headline caps stay
        # even at zero, because "0%" at the start of a window is information.
        if percent <= 0 and row.get("kind") not in ("session", "weekly_all"):
            continue
        out.append({
            "label": _limit_label(row),
            "percent": percent,
            "resets": row.get("resets_at"),
            "severity": row.get("severity") or "normal",
        })
    return out


def resets_in_s(stamp: str | None) -> int | None:
    """Seconds until a cap comes back, or None when there is no readable stamp.

    Split out of `limit_reset` so the pacing verdict and the bar's countdown read
    one clock: a `hold` carries a resumption time, and a resumption time that
    disagreed with the number drawn two inches above it would be two answers to
    one question. Never negative — a window whose reset is in the past has come
    back, which is 0 seconds away and not a negative wait.
    """
    if not stamp:
        return None
    try:
        then = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    return max(0, int((then - datetime.now(timezone.utc)).total_seconds()))


def limit_reset(stamp: str | None) -> str:
    """'44m', '3h58m', '5d' — when a cap comes back, in five columns.

    until() is right for a lease measured in minutes and wrong here: a weekly
    window reads '128h48m', which is both too wide for the line and not how
    anybody thinks about next Monday.
    """
    secs = resets_in_s(stamp)
    return "" if secs is None else limit_reset_secs(secs)


def limit_reset_secs(secs: int) -> str:
    """The same countdown from seconds rather than from a stamp.

    The pacing verdict carries `resets_in_s` because a caller wants to compare
    it, not print it; this is how it gets printed, and it is the same function
    the bar uses so the two cannot come to spell 47 minutes differently.
    """
    if secs <= 0:
        return "now"
    if secs < 3600:
        return f"{secs // 60}m"
    if secs < 48 * 3600:
        return f"{secs // 3600}h{(secs % 3600) // 60:02d}m"
    return f"{secs // 86400}d{(secs % 86400) // 3600}h"


def limit_colour(percent: int, severity: str = "normal") -> str:
    """Green, yellow, red — and the endpoint's own severity can only escalate it.

    Thresholds here rather than trusting severity alone: severity says 'normal'
    at 62% of a window that six seats will finish inside the hour, and the point
    of the line is to see that coming.
    """
    if severity in ("critical", "exceeded", "blocked") or percent >= 90:
        return "red"
    if severity == "warning" or percent >= 70:
        return "yellow"
    return "green"


def limit_bar(percent: int, width: int) -> str:
    """A bar `width` cells wide. Full blocks for what is spent, light for the rest."""
    width = max(1, width)
    filled = max(0, min(width, int(round(percent / 100 * width))))
    # Anything spent shows at least one cell: a bar that reads empty at 3% and
    # empty at 0% has thrown away the only distinction that matters early on.
    if percent > 0 and filled == 0:
        filled = 1
    return "█" * filled + "░" * (width - filled)


def limit_cells(limits: list[dict], width: int) -> list[tuple[str, str, str, str, str]]:
    """[(label, bar, '62%', '3h50m', colour), …], sized to fit `width` columns.

    The layout lives here so the two dashboards cannot drift on it, and the
    styling does not, so each stays in charge of how it draws a colour.
    """
    if not limits or width < 20:
        return []
    gap = 2                                       # between segments
    # label + space + … + space + '100%' + space + reset, per segment.
    fixed = sum(len(l["label"]) for l in limits) + len(limits) * (1 + 1 + 4 + 1 + 5) + \
        gap * (len(limits) - 1)
    bar = (width - fixed) // len(limits)
    if bar < 4:                                   # too narrow for bars: drop them
        bar = 0
    bar = min(bar, 16)
    return [(l["label"], limit_bar(l["percent"], bar) if bar else "",
             f"{l['percent']}%", limit_reset(l["resets"]),
             limit_colour(l["percent"], l["severity"])) for l in limits]


# ---- pacing: the ceiling, read by something other than a bar -----------------
#
# The caps above are drawn and nothing consults them (#275). The whole gap is
# that the fleet's one hard ceiling — one subscription, every machine, every
# project — is enforced by a human noticing a bar go red, and a seat is a pane
# nobody is watching.
#
# WHERE THE NUMBER LIVES, WHICH IS NOWHERE NEW. `pace()` adds no store. The cap
# is the usage endpoint's fact; `fetch_limits()` already holds the only copy
# there is, in a machine-wide cache that exists to keep three dash panes from
# rate-limiting each other, and this reads THAT. No second file, no board row,
# no figure of its own to go stale behind the endpoint's back. A verdict that
# kept its own number would be a second source of truth about a ceiling nobody
# here sets, which is the one thing a governor must not be.
#
# WHAT THIS IS NOT. It does not throttle, park, resume, or pick work. It says
# what the window says, in words a caller can act on. #276 owns the actuator
# (the dials, board-side, expiring), #55 owns the ceiling this repo CHOOSES, and
# #232/#227 own what to run next. The decision here is only "how does the fleet
# stand", and the value of having it as a function is that the answer stops
# being a colour on a screen somebody has to be looking at.

#: The bar's colours, read as decisions. Derived from `limit_colour` rather than
#: restated with thresholds of its own: the display and the verdict disagreeing
#: about what 88% means is exactly the failure a human debugging this at 2am
#: cannot see, and the endpoint's `severity` — the one input that knows about
#: caps this fleet has not modelled — only reaches the verdict because that
#: function already honours it.
PACE_OF_COLOUR = {"green": "go", "yellow": "slow", "red": "hold"}
#: Worst cap wins. Never averaged: a 7d window at 12% does not buy back a 5h
#: window at 94%, and the thing about to stop is the one that binds.
PACE_RANK = {"go": 0, "slow": 1, "hold": 2}


def _pace(verdict: str, source: str, reason: str, cap: dict | None = None) -> dict:
    """One shape for every answer, so a caller never has to ask which kind it got."""
    return {
        "verdict": verdict,
        "source": source,
        "reason": reason,
        "cap": (cap or {}).get("label"),
        "percent": (cap or {}).get("percent"),
        "severity": (cap or {}).get("severity"),
        "resets_in_s": resets_in_s((cap or {}).get("resets")),
    }


def pace(caps: tuple[list[dict], str | None] | None = None) -> dict:
    """How the shared subscription stands, as a verdict a caller can act on.

    {verdict: go|slow|hold|unknown, source, cap, percent, severity,
     resets_in_s, reason}

    `caps` is a `fetch_limits()` answer, for a caller that already has one (the
    dashboards do). Left out, this fetches — which costs nothing extra, because
    `fetch_limits` is floored at one call every three minutes across every
    process on the machine and hands back the cache in between.

    The four answers, and why the fourth exists:

    * **go** — room, or nothing to pace against. An API-key install has no
      subscription caps at all, so it gets `go` and says so; that is the same
      rule as the dash's, where no token means one line fewer rather than an
      error.
    * **slow** / **hold** — the bar's yellow and red, which is to say 70% and
      90%, or sooner when the endpoint's own severity says so. `hold` carries
      `resets_in_s` and is therefore a WAIT, not a stop: a five-hour window at
      95% comes back, and the caller that treats it as terminal has thrown away
      the only fact that makes it survivable.
    * **unknown** — the figures could not be obtained at all. NOT `go`, which
      would be a governor reporting clear on an input it never read (#244), and
      not `hold`, which would let a dropped network park the fleet. Unknown is
      the honest word and it leaves the decision with the caller.

    Figures that are merely OLD are a third case and they are not thrown away:
    a failed call keeps the last answer, and caps move over hours, so minutes-old
    ones are still the right ones to act on. What staleness costs is the right to
    say `go` — a `go` on figures nobody could refresh is the one verdict that
    could be confidently wrong about a window that emptied while the network was
    down. It does NOT escalate a `slow` to a `hold`: staleness is uncertainty
    about the number, and parking work over it would be a claim about the window
    made on the strength of a hiccup.
    """
    limits, err = caps if caps is not None else fetch_limits()
    if not limits:
        if err:
            return _pace("unknown", "unreadable",
                         f"the usage endpoint could not be read ({err}) and no "
                         f"cached figures survive — the ceiling is unknown, not clear")
        return _pace("go", "absent",
                     "no subscription caps to read: this install has no OAuth "
                     "token, so there is no shared window to pace against")

    stale = err is not None
    graded = [(PACE_RANK[PACE_OF_COLOUR[limit_colour(l["percent"], l["severity"])]], l)
              for l in limits]
    # Percent breaks a tie so the reported cap is the one nearest its ceiling,
    # not whichever the endpoint happened to list first.
    cap = max(graded, key=lambda g: (g[0], g[1]["percent"]))[1]
    verdict = PACE_OF_COLOUR[limit_colour(cap["percent"], cap["severity"])]
    reason = f"{cap['label']} at {cap['percent']}%"
    if cap["severity"] not in ("", "normal"):
        reason += f" ({cap['severity']})"
    if not stale:
        return _pace(verdict, "live", reason, cap)
    reason += f", on figures that could not be refreshed ({err})"
    if verdict == "go":
        return _pace("slow", "stale", reason + " — too old to say go on", cap)
    return _pace(verdict, "stale", reason, cap)


def pace_line(verdict: dict) -> str:
    """The verdict on one line, in one place.

    Shared rather than formatted at each call site for the same reason
    `limit_cells` is: `qb-pace` prints this, `qb-seat` prints this before it
    starts an agent, and two spellings of one judgement is how a fleet ends up
    arguing with itself about whether it is allowed to spend.
    """
    out = f"pace: {verdict['verdict'].upper()} — {verdict['reason']}"
    secs = verdict.get("resets_in_s")
    if secs is not None and verdict["verdict"] != "go":
        out += f"; resets in {limit_reset_secs(secs)}"
    return out


# THE ESTIMATE IS IN TOKENS AND IT STOPS THERE, ON PURPOSE.
#
# "Does this job fit in what is left" is the question worth answering, and it
# needs a rate — how much of a five-hour window one seat-run actually spends.
# Nothing records that. The board knows what a run cost in TOKENS; the endpoint
# knows what the window has spent in PERCENT; no row anywhere pairs them, which
# is #275's own first sequencing step (sample the caps either side of a run) and
# it belongs to whatever drives the run rather than here.
#
# So this reports the two halves and refuses to multiply them. A fit prediction
# derived from a made-up rate would be the exact failure #275 names — a governor
# that guesses rather than saying it cannot read its input — and it would be
# believed, because it would arrive in the same sentence as two real numbers.

def subscription_cost(client=None, reviewer: str = "claude",
                      days: int = 30) -> tuple[dict | None, str | None]:
    """What one seat-run of `reviewer` costs, from the board's own record.

    ({tokens_per_run, runs, models}, None) or (None, why-not).

    Only the seats billing to THIS subscription count, which is why there is a
    reviewer argument with `claude` as its default: the five-hour and weekly caps
    are the Anthropic subscription's, and `codex`, `antigravity` and `pi` bill to
    OpenAI, a Google account and OpenRouter. A four-seat panel is not four seats
    of pressure on this window (#276 makes the same distinction for its shed).
    """
    # The client is BUILT in here rather than taken as read, because resolving a
    # board config is itself one of the ways this question goes unanswered — a box
    # with no config is the ordinary case, not an error, and a caller that had to
    # guard the constructor separately would report it in a different voice from
    # the board being down.
    try:
        # `judged_only=false`, unlike every other reader of this endpoint. The
        # rest of the page is about a reviewer's precision, which only an
        # adjudicated run can measure; this is about what a run COST, and an
        # unjudged round spent its tokens exactly the same.
        data = (client or board_client()[0]).get(
            "/review/stats", {"days": days, "judged_only": "false"})
    except Exception as exc:                      # noqa: BLE001 — no board is not a failure
        return None, f"the board did not answer ({type(exc).__name__})"
    # `reviewer`, which is what the RESPONSE calls the column the query labels
    # `name`. Read off a live answer rather than off the SELECT, because the two
    # differ and the difference is silent: a filter on the wrong key matches
    # nothing and reports "no measured history", which is indistinguishable from
    # a board that genuinely has none.
    rows = [r for r in (data.get("by_model") or [])
            if (r.get("reviewer") or "") == reviewer and r.get("total_tokens")
            and r.get("billable_runs")]
    if not rows:
        return None, (f"the board has no measured token history for the "
                      f"'{reviewer}' seat in the last {days} days")
    tokens = sum(int(r["total_tokens"]) for r in rows)
    runs = sum(int(r["billable_runs"]) for r in rows)
    # Summed and divided once rather than averaging the rows' own averages: the
    # groups are (reviewer, model, effort) and they have wildly different run
    # counts, so a mean of means weights a single opus run like forty sonnet ones.
    return {"tokens_per_run": round(tokens / runs), "runs": runs,
            "models": sorted({r.get("model") or "?" for r in rows})}, None


def pace_estimate(verdict: dict, cost: dict | None, seats: int,
                  rounds: int = 1) -> dict:
    """What a job of `seats` × `rounds` costs, beside what the window has left.

    {tokens, per_run, runs, headroom_pct, resets_in_s, fits, why} — where `fits`
    is **always None today** and `why` says why. See the note above: the
    tokens-to-window rate is not recorded anywhere yet, so the honest answer to
    "will this finish" is that it cannot be predicted, not a number.
    """
    per_run = (cost or {}).get("tokens_per_run")
    percent = verdict.get("percent")
    return {
        "seats": seats,
        "rounds": rounds,
        "per_run": per_run,
        "runs": (cost or {}).get("runs"),
        "tokens": per_run * seats * rounds if per_run else None,
        "headroom_pct": None if percent is None else max(0, 100 - percent),
        "resets_in_s": verdict.get("resets_in_s"),
        "fits": None,
        "why": ("no recorded rate from tokens to window percent — nothing samples "
                "the caps either side of a run yet (#275 step 1), so whether this "
                "job fits cannot be answered without guessing"),
    }
