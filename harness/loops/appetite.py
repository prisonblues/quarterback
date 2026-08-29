#!/usr/bin/env python3
"""The appetite gates — what a loop may pick up, and how much it may file.

Two brakes on an autonomous loop, in the two directions it can run away:

    PICKING UP   a loop that acts on whatever it finds "actionable" works the
                 backlog in whatever order it happens to enumerate it — not an
                 order anybody chose.
    FILING       a loop applying a consistent standard with no ceiling produces
                 a backlog nobody reads. Nine issues in one day, every one of
                 them a response to something real, which is what makes it a
                 risk rather than a bug.

The policy lives in the repo's rules file (`issue_pickup`, `issue_filing`, both
in harness_rules.DEFAULTS); this module is what reads it and answers.
Deliberately split that way: #63's watcher is one consumer, and the PR watcher
(#54), the epic driver and whatever acquires an appetite next want the same
brakes. A brake implemented per-consumer is three brakes that will disagree.

WHERE THE LINE IS — the one thing to get right before wiring a caller in.

There are two questions here and they are not the same question, which is why
there are two entry points rather than one:

    pickup_verdict()   May a loop CHOOSE this issue, out of a backlog, with no
                       human having named it? The whole gate: on/off, author
                       allowlist, label allowlist, human triage, and the
                       refusals below.
    refusal_verdict()  Regardless of who chose it — is this an issue a person
                       has marked as one no diff can settle? The `skip_labels`
                       half only.

`epic.py --execute 42` names an epic on the command line and the human typing it
is the authorisation, so gating that on `issue_pickup.enabled` (default false)
would break every repo's executor while preventing nothing. But an epic expands
one issue a human named into many sub-issues nobody named individually, and a
`needs-human/ui` label on one of those does not stop meaning what it says because
it arrived inside an epic. So the epic driver calls `refusal_verdict` and not
`pickup_verdict`, and `skip_when_unlabelled` — a statement about SELECTING from
an untriaged backlog — deliberately does not apply to it.

Call `pickup_verdict` where nothing named the issue but the loop itself.

SELF-APPROVAL, which is the whole point of `require_human_triage`.

An allowlist of labels is decorative if the agent can apply the labels. That is
#78's `judge_model` problem one level out: the thing authorising the work must
not be the thing doing it. So the check does not ask "is the label present" — it
reads the issue's label EVENTS and asks who put it there. A Bot-type actor never
counts as human triage, and `issue_pickup.agent_actors` names any further logins
that do not.

The hole this leaves is worth stating rather than discovering: an agent
authenticating as its human's own GitHub account is indistinguishable here from
that human. The check is sound against bots and against a named agent account.
It is not a substitute for agents having their own identity.

WHY A LABEL AND NOT A CLASSIFIER, for the refusal half.

An agent could be asked to judge whether an issue needs human judgement. That is
a self-referential test and it fails in the expensive direction: the issues most
needing a human are the ones a model is most confident it understands. #64 is the
measured shape — three of six confirmed findings were conditionals from a
reviewer that had *declared it could not assess the condition*, and it raised
them anyway. So: a label a person applied, same reasoning as human triage.

The default skip list is `needs-human/*`, which is #279's closed vocabulary
(`decision | taste | ui | environment | auth | chore | other`, the last of them
from #578) rather than a second, parallel set of words for the same idea. #86
proposed `design`/`ui`/`decision-owed`/`needs-scoping`, written when this repo
had no labels at all; those seven exist now and a producer already writes the
class into the board, so the gate reads the labels that exist instead of asking
anyone to maintain two vocabularies that mean the same thing.

Usage:
    python3 appetite.py pickup 85 --repo quarterback      # may I work this?
    python3 appetite.py file --title "..." --run $SESSION # may I file this?
    python3 appetite.py file --title "..." --run $SESSION --record   # I did
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness_rules import (  # noqa: E402
    DEFAULTS, RULES_FILENAME, RepoNotFound, describe, resolve_repo, unattended,
)

# Shared with epic.py's run state, for the same reason it exists there: a CLI
# invoked once per candidate cannot count its own run without somewhere to keep
# the tally. Overridable so the tests never touch a real one.
STATE_DIR = Path(os.environ.get("LOOPS_STATE_DIR")
                 or Path.home() / ".local/state/loops")


@dataclass
class Verdict:
    """Allowed or not, and WHY — the reason is not decoration.

    A gate that returns a bare False teaches the next agent to route around it,
    because "no" and "misconfigured" look identical from the caller's side. Every
    refusal here names the setting that refused and what would change it.
    """

    allowed: bool
    reason: str
    setting: str = ""
    detail: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"allowed": self.allowed, "reason": self.reason,
                "setting": self.setting,
                **({"detail": self.detail} if self.detail else {})}


def gh_json(args: list[str], repo: str):
    """`gh ... --repo <repo> --json ...`, parsed. Empty output is not an error:
    `gh issue list` with no matches prints nothing rather than `[]`."""
    out = subprocess.run(["gh", *args, "--repo", repo],
                         capture_output=True, text=True, check=True).stdout
    return json.loads(out) if out.strip() else []


def _rules(cfg: dict, block: str) -> dict:
    """One rules block, falling back to DEFAULTS per key rather than to an open
    gate.

    A cfg assembled by hand — several tests, and any caller predating these
    blocks — must land on the safe end, not on "no block, so no rules". That
    distinction is the difference between a missing config refusing and a missing
    config waving everything through.
    """
    got = cfg.get(block)
    return {**DEFAULTS[block], **(got if isinstance(got, dict) else {})}


def _flag(rules: dict, block: str, key: str) -> bool:
    """A boolean setting, refusing anything that is not actually a boolean.

    JSON has a `false` and a `"false"` and only one of them is falsy. This exact
    trap has been sprung here before — `harness_rules._overlay_problem` checks
    the VALUES of overlay keys and not just their names precisely because
    `"enabled": "false"` is a non-empty string, therefore truthy, and therefore
    the natural hand-edit did the opposite of what the file existed for.

    Every switch in these two blocks defaults CLOSED, so the direction of that
    mistake is always "gate silently open". A hard error rather than a coercion:
    guessing what `"no"` or `0` meant is how a config nobody re-read becomes a
    gate nobody notices is off, and the same refusal already guards
    `skip_labels`.
    """
    v = rules.get(key)
    if not isinstance(v, bool):
        raise SystemExit(f"{RULES_FILENAME}: `{block}.{key}` must be true or "
                         f"false, got {v!r} — a quoted \"false\" is a non-empty "
                         f"string and would read as TRUE, opening this gate")
    return v


def _names(rules: dict, block: str, key: str) -> list[str]:
    """A list-of-strings setting, refusing anything else.

    The trap is that a bare string is *iterable*, so `[str(x) for x in "claude"]`
    quietly yields `['c','l','a','u','d','e']` — a plausible-looking list that
    matches no login and no label. For `only_labels` and `allowed_authors` that
    fails closed and merely confuses; for `agent_actors` it fails **open**, and
    the named agent account is no longer recognised as an agent, so the one
    setting whose job is to stop self-approval stops doing it. Refuse all four
    the same way rather than leave a reader to work out which is which.

    A hard error, unlike a misspelled KEY — which leaves the setting at its
    default, and every default here is the safe end. A malformed VALUE instead
    leaves the gate reading as configured while matching nothing at all: an open
    door that looks shut. `preland.disabled_checks` refuses for the same reason.
    """
    v = rules.get(key)
    if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
        raise SystemExit(f"{RULES_FILENAME}: `{block}.{key}` must be a list of "
                         f"strings, got {v!r} — a bare string here iterates as "
                         f"single characters and matches nothing")
    return [x.strip() for x in v if x.strip()]


def _cap(rules: dict) -> int | None:
    """`issue_filing.max_per_run`: a non-negative int, or null for no cap.

    Refused rather than coerced for the same reason as `_flag`. A string here
    would compare against an int and raise deep inside the check, where the
    traceback names neither the setting nor the repo.
    """
    v = rules.get("max_per_run")
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, int) or v < 0:
        raise SystemExit(f"{RULES_FILENAME}: `issue_filing.max_per_run` must be a "
                         f"non-negative integer or null, got {v!r}")
    return v


def label_names(labels: Any) -> list[str]:
    """The label names on an issue, from whatever `gh --json labels` handed us.

    Accepts the API shape (`[{"name": "needs-human/ui", …}, …]`) and a plain list
    of strings, because the two producers differ and the difference is not
    interesting to a caller: `lander.py` already unwraps its own copy and a second
    unwrapping at every future call site is how one of them ends up comparing
    dicts to strings and matching nothing, silently.

    Blank names are dropped rather than counted. An empty label cannot exist on
    GitHub, so `[""]` only ever arrives from a malformed stub or a hand-built
    fixture — and counting it as a label would quietly satisfy
    `skip_when_unlabelled`, turning the gate off in exactly the case it exists
    for.
    """
    out = []
    for item in labels or []:
        name = item.get("name") if isinstance(item, dict) else item
        if isinstance(name, str) and name.strip():
            out.append(name.strip())
    return out


# --------------------------------------------------------------- picking up

def refusal_verdict(cfg: dict, labels: Any, *, number: int | None = None,
                    unlabelled_refuses: bool = True) -> Verdict:
    """Has a person marked this issue as one no diff can settle?

    The half of the pickup gate that applies however the issue was chosen — see
    the module docstring on why the epic driver calls this and not
    `pickup_verdict`. `unlabelled_refuses=False` is how that caller declines
    `skip_when_unlabelled`: a human named the epic, so its sub-issues are not
    "untriaged by anyone" in the sense that setting is about.

    Patterns are matched as globs, so `needs-human/*` keeps working when #279's
    `other` escape hatch grows the vocabulary a word — which its own docstring
    says is how the vocabulary is meant to grow. An exact name still matches
    itself, so a repo listing plain `design` loses nothing.
    """
    rules = _rules(cfg, "issue_pickup")
    pats = _names(rules, "issue_pickup", "skip_labels")
    who = f"#{number}" if number is not None else "this issue"

    names = label_names(labels)
    if not names:
        if unlabelled_refuses and _flag(rules, "issue_pickup", "skip_when_unlabelled"):
            return Verdict(False,
                           f"{who} carries no labels — nothing has triaged it, so "
                           f"nothing has established it is safe to act on "
                           f"unattended",
                           "issue_pickup.skip_when_unlabelled")
        return Verdict(True, f"{who} carries no refusing label", "")

    # Case-insensitively, because GitHub preserves the case a label was created
    # with and a rules file naming `needs-human/ui` must still catch `Needs-Human/UI`.
    #
    # Exact equality as well as the glob, because `fnmatch` reads `[`, `]` and `?`
    # as metacharacters and GitHub allows them in a label name. A repo listing a
    # literal `blocked?` would otherwise configure a pattern that does not match
    # the very label it was copied from — and on a SKIP list, failing to match is
    # the direction that lets work through.
    def refuses(name: str) -> bool:
        low = name.casefold()
        return any(low == p.casefold() or fnmatch.fnmatchcase(low, p.casefold())
                   for p in pats)

    hit = sorted(n for n in names if refuses(n))
    if hit:
        return Verdict(False,
                       f"{who} is labelled {', '.join(repr(h) for h in hit)} — a "
                       f"human judgement is owed before code is written",
                       "issue_pickup.skip_labels", {"matched": hit})
    return Verdict(True, f"{who} carries no refusing label", "")


def label_events(repo: str, number: int) -> list[dict]:
    """Who applied which label to this issue, oldest first.

    Reads the issue's event log rather than its current labels, because the
    current labels cannot answer the only question that matters here: whether
    the authorisation came from someone other than the worker. Returns dicts of
    {label, actor, actor_type} — `gh issue view --json labels` would be one call
    cheaper and would tell us nothing.
    """
    out = subprocess.run(
        ["gh", "api", "--paginate", f"repos/{repo}/issues/{number}/events"],
        capture_output=True, text=True)
    if out.returncode != 0 or not out.stdout.strip():
        # A 404 here is an unreadable event log, not an empty one, and the
        # difference decides a gate. Callers get "no events" and the triage
        # check treats that as NOT triaged — the safe direction.
        return []
    try:
        events = json.loads(out.stdout)
    except json.JSONDecodeError:
        return []
    # `unlabeled` is kept, not filtered out, and that is not tidiness. Keeping
    # only `labeled` makes the log append-only in the wrong way: a human applies
    # `p0`, it is removed, an agent re-applies it, and the stale human event is
    # still sitting there authorising the agent's own label. See `human_triaged`.
    return [{"label": (e.get("label") or {}).get("name", ""),
             "actor": (e.get("actor") or {}).get("login", ""),
             "actor_type": (e.get("actor") or {}).get("type", ""),
             "event": e.get("event", "")}
            for e in events if e.get("event") in ("labeled", "unlabeled")]


def human_triaged(events: list[dict], qualifying: set[str],
                  agent_actors: list[str]) -> tuple[bool, str]:
    """Did a human — someone who is not the worker — apply a qualifying label?

    Returns (verdict, who/why). Case-insensitive on both label and login: GitHub
    treats logins case-insensitively, and a repo that writes "P0" in its rules
    and "p0" on the issue has configured the gate, not defeated it.

    Only the CURRENT application of each label counts — the last event naming it,
    which the API returns oldest-first. Scanning for any historical `labeled` by a
    human would let a removed-and-re-applied label inherit an authorisation the
    human gave to a different application of it: a person applies `p0`, someone
    takes it off, an agent puts it back, and the agent is now working under the
    person's signature. That is the self-approval hole this function exists to
    close, one step further round.
    """
    agents = {a.casefold() for a in agent_actors}
    wanted = {q.casefold() for q in qualifying}

    # label -> the last event that touched it. `unlabeled` overwrites, so a label
    # currently off the issue leaves an `unlabeled` here and authorises nothing.
    current: dict[str, dict] = {}
    for e in events:
        name = e["label"].casefold()
        if name in wanted:
            current[name] = e

    unknown = False
    for e in current.values():
        if e.get("event") != "labeled":
            continue
        if e["actor_type"] == "Bot" or e["actor"].casefold() in agents:
            continue
        if not e["actor"]:
            # A deleted account, or the API omitting the actor. "Somebody applied
            # this and we cannot see who" is precisely the state this check exists
            # to distinguish, and treating it as a human is the same
            # unreadable-means-yes mistake as a corrupt tally meaning zero. An
            # agent whose actor block failed to serialise would authorise itself.
            unknown = True
            continue
        return True, f"{e['label']!r} applied by {e['actor']}"
    if unknown:
        return False, ("the qualifying label's `labeled` event names no actor, so "
                       "who applied it cannot be established")

    if not events:
        return False, "no label events readable on this issue"
    # Four different states, and a caller acting on the wrong one acts wrongly:
    # nothing labelled it, it was labelled and un-labelled again, an agent
    # labelled it, or the log is unreadable. Saying "a bot did it" when in fact no
    # such event exists sends whoever reads the refusal looking for a bot that was
    # never there.
    if not current:
        return False, ("the qualifying label is on the issue but no `labeled` "
                       "event records who applied it")
    if all(e.get("event") == "unlabeled" for e in current.values()):
        return False, ("every qualifying label was REMOVED after it was applied — "
                       "the issue's labels and its event log disagree")
    return False, ("the current application of every qualifying label was made by "
                   "a bot or a configured agent actor")


def author_allowed(rules: dict, author: str) -> tuple[bool, str]:
    """Is this issue's author on the allowlist? `(ok, why)`.

    #63's security section, and it applies to every consumer of this gate:
    `prisonblues/quarterback` is public, anyone can open an issue, and under a
    watcher that text becomes the instructions for an agent with a full shell.
    The mitigation has to be an ALLOWLIST, not a filter — a filter is a list of
    the phrasings somebody already thought of, and the attack is the phrasing
    nobody thought of.

    Empty means nobody, not everybody. Same reasoning as `only_labels`: turning
    the gate on is one decision and saying whose issues may come through it is
    another, and a repo that has made only the first has not said "anyone's".
    """
    allowed = _names(rules, "issue_pickup", "allowed_authors")
    if not allowed:
        return False, ("issue_pickup.allowed_authors is empty — no author's issues "
                       "qualify. This is an allowlist and not a filter on purpose: "
                       "a public repo lets anyone write an agent's instructions")
    if not author:
        return False, "the issue's author could not be read"
    if author.casefold() in {a.casefold() for a in allowed}:
        return True, f"opened by {author}"
    return False, (f"opened by {author}, who is not in "
                   f"issue_pickup.allowed_authors")


def author_verdict(cfg: dict, author: str) -> Verdict:
    """May this author's text drive anything in this repo?

    :func:`author_allowed` against the repo's config rather than against a rules
    block a caller had to dig out itself. Public because the allowlist bounds
    more than picking work up: #63's watcher writes a comment saying what
    decision an issue is waiting on, and whose issue it will write on is the same
    question with the same answer. A consumer reading `allowed_authors` for
    itself is a second reading of the one setting that must not have two.
    """
    ok, why = author_allowed(_rules(cfg, "issue_pickup"), author)
    return Verdict(ok, why, "issue_pickup.allowed_authors")


def pickup_verdict(cfg: dict, issue: dict, *,
                   events: list[dict] | None = None) -> Verdict:
    """May a loop pick this issue up of its own accord?

    `issue` is {number, labels: [...], author: login}. `events` is
    label_events()' output; passing it in keeps this function pure — every branch
    is testable without reaching GitHub, and the one caller that must reach
    GitHub does it once.

    The order of the checks is the cheap-and-broad ones first, so a refusal names
    the most fundamental reason rather than an incidental one: a repo with the
    gate off should be told the gate is off, not that the issue lacked a label.
    """
    rules = _rules(cfg, "issue_pickup")
    num = issue.get("number")

    if not _flag(rules, "issue_pickup", "enabled"):
        return Verdict(False, f"issue pickup is off for this repo (#{num})",
                       "issue_pickup.enabled")

    who = author_verdict(cfg, str(issue.get("author") or ""))
    if not who.allowed:
        return Verdict(False, f"#{num}: {who.reason}", "issue_pickup.allowed_authors")

    # Before the allowlist, not after: a `p0` issue that a person has also marked
    # `needs-human/decision` is refused for the reason that actually matters, and
    # an operator who reads "it lacked p0" would go and add p0.
    refusal = refusal_verdict(cfg, issue.get("labels"), number=num)
    if not refusal.allowed:
        return refusal

    only = _names(rules, "issue_pickup", "only_labels")
    if not only:
        # Not the same as "no restriction". Turning the gate on is one decision;
        # saying what may come through it is another, and a repo that has made
        # only the first has not said "anything".
        return Verdict(False,
                       "issue_pickup.only_labels is empty — nothing qualifies",
                       "issue_pickup.only_labels")

    have = {n.casefold() for n in label_names(issue.get("labels"))}
    matched = [x for x in only if x.casefold() in have]
    if not matched:
        return Verdict(False,
                       f"#{num} carries none of {', '.join(sorted(only))}",
                       "issue_pickup.only_labels",
                       {"labels": sorted(label_names(issue.get("labels")))})

    if not _flag(rules, "issue_pickup", "require_human_triage"):
        return Verdict(True, f"#{num} labelled {', '.join(matched)}",
                       "issue_pickup.only_labels", {"matched": matched})

    ok, why = human_triaged(events or [], set(matched),
                            _names(rules, "issue_pickup", "agent_actors"))
    if not ok:
        return Verdict(False, f"#{num} is labelled but not human-triaged: {why}",
                       "issue_pickup.require_human_triage", {"matched": matched})
    return Verdict(True, f"#{num} human-triaged: {why}",
                   "issue_pickup.require_human_triage", {"matched": matched})


def events_needed(cfg: dict) -> bool:
    """Can `pickup_verdict`'s answer turn on the label event log for this repo?

    `label_events` is a paginated API call per issue and a watcher sweeping a
    backlog makes one per candidate, so callers skip it where it cannot change
    the verdict. Answered here rather than at each call site because "which
    settings make the log matter" is a fact about this gate: a third setting
    that consulted the log would otherwise have to be remembered in three places,
    and the failure of forgetting is a gate that silently stops reading who
    applied the label.
    """
    rules = _rules(cfg, "issue_pickup")
    return (_flag(rules, "issue_pickup", "enabled")
            and _flag(rules, "issue_pickup", "require_human_triage"))


def unattended_writes_allowed(cfg: dict) -> bool:
    """May an unattended run write to this repo's tracker?

    `issue_filing.unattended` restates #40's standing decision as config, and a
    comment posted by a loop nobody is watching is the same decision as an issue
    filed by one. Exposed rather than left to each consumer's own reading of the
    block: #63's watcher wants the answer and a second switch meaning the same
    thing is how the two come to disagree.
    """
    return _flag(_rules(cfg, "issue_filing"), "issue_filing", "unattended")


# ------------------------------------------------------------------- filing

def duplicate_search(repo: str, title: str, limit: int = 10) -> list[dict] | None:
    """Open issues whose text already covers this title, or None if it could not
    be searched for.

    Deliberately a search over the title's own words rather than an exact match:
    the duplicate that matters is the one somebody phrased differently, and an
    exact-title check would pass every single time and read as a working gate.

    None rather than `[]` when the title yields no usable terms, and the
    distinction is a bypass if it is got wrong: `[]` means "searched, found
    nothing" and OPENS the gate, so returning it here would let any filer defeat
    `require_dedup_check` by choosing a title of short words. `check()` reads None
    as "not searched" and refuses. Searching for the empty string instead is not
    the fix — `gh` answers that with the whole backlog, refusing every filing for
    a reason nobody could act on.
    """
    words = [w for w in "".join(c if c.isalnum() else " " for c in title).split()
             if len(w) > 3][:8]
    if not words:
        return None
    # `gh` failing is left to propagate on purpose. Search being unavailable
    # must not read as "no duplicates": that is the answer that lets filing
    # proceed, and an outage would silently open the gate this setting exists to
    # close. A crash is the honest outcome — the check could not be performed.
    return gh_json(["issue", "list", "--state", "open", "--limit", str(limit),
                    "--search", " ".join(words), "--json", "number,title"], repo) or []


class FilingBudget:
    """How many issues this run may still file, and whether it may file at all.

    Holds the count in the object for an in-process loop. A CLI invoked once per
    candidate has no such continuity, so `run_id` persists the tally to
    STATE_DIR — without it `max_per_run` would be self-reported by the very
    process it constrains, which is not a gate.
    """

    def __init__(self, cfg: dict, *, repo: str = "", run_id: str = "",
                 is_unattended: bool | None = None):
        self.rules = _rules(cfg, "issue_filing")
        self.repo = repo or cfg.get("github", "")
        self.run_id = run_id
        self.unattended = unattended() if is_unattended is None else is_unattended
        self._filed = 0

    # -- tally -------------------------------------------------------------

    @property
    def _state_path(self) -> Path:
        safe = "".join(c if c.isalnum() or c in "-_" else "-"
                       for c in f"{self.repo}-{self.run_id}")
        return STATE_DIR / f"appetite-{safe}.json"

    def _read_tally(self) -> tuple[int | None, str]:
        """`(count, problem)`. A count of None means the tally could not be read.

        None and 0 are emphatically not the same answer. A truncated or
        hand-mangled state file read as 0 hands back a FULL budget to a run that
        may have spent it — the corruption silently reopens the gate, which is the
        direction every default in this module leans against. So an unreadable
        tally refuses, exactly as an unavailable duplicate search does.
        """
        if not self.run_id:
            return self._filed, ""
        p = self._state_path
        if not p.is_file():
            return 0, ""
        try:
            got = json.loads(p.read_text())
            n = got["filed"]
        except (json.JSONDecodeError, KeyError, TypeError, OSError) as e:
            return None, f"{p.name} could not be read ({type(e).__name__})"
        if isinstance(n, bool) or not isinstance(n, int) or n < 0:
            return None, f"{p.name} holds a nonsense count ({n!r})"
        return n, ""

    @property
    def filed(self) -> int:
        """The tally, with an unreadable one reported as at-cap rather than zero.

        Kept as an int because callers print it. `check()` uses `_read_tally`
        directly so it can say WHY it refused; this is the blunt form, and it
        errs closed.
        """
        n, _ = self._read_tally()
        if n is None:
            cap = _cap(self.rules)
            return cap if cap is not None else 1
        return n

    def record(self, title: str = "") -> int:
        """Count one filed issue against this run's budget. Returns the new total.

        Written via a temp file and `os.replace`, which is atomic on POSIX: a
        crash mid-write would otherwise leave the truncated JSON that `_read_tally`
        now has to refuse, turning a killed run into a blocked one.

        Read-increment-write is still not atomic ACROSS processes, and two filers
        sharing a `--run` can both pass a cap of 1 before either records. Not
        locked, because the cheap fix (an O_EXCL lock file) introduces a stale-lock
        failure mode that is worse than the race it closes: the loops that file are
        sequential today, and the honest statement is that this bounds an
        enthusiastic loop rather than a concurrent one.
        """
        n, _ = self._read_tally()
        total = (n if n is not None else self.filed) + 1
        self._filed = total
        if self.run_id:
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            tmp = self._state_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(
                {"repo": self.repo, "run": self.run_id, "filed": total,
                 "last_title": title}))
            os.replace(tmp, self._state_path)
        return total

    # -- the gate ----------------------------------------------------------

    def check(self, title: str, *, duplicates: list[dict] | None = None) -> Verdict:
        """May this run file an issue titled `title`?

        `duplicates` is duplicate_search()' output. None means "not searched" —
        which fails the check when `require_dedup_check` is on, rather than
        passing it. An unchecked search and an empty one are the same value in
        Python and must not be the same answer here.
        """
        if self.unattended and not _flag(self.rules, "issue_filing", "unattended"):
            return Verdict(False,
                           "unattended runs may not file issues in this repo — "
                           "report what you would have filed instead",
                           "issue_filing.unattended")

        cap = _cap(self.rules)
        already, problem = self._read_tally()
        if already is None:
            return Verdict(False,
                           f"this run's filing tally could not be read, so the cap "
                           f"cannot be enforced: {problem}. Delete the file to start "
                           f"the run's budget over",
                           "issue_filing.max_per_run", {"problem": problem})
        if cap is not None and already >= cap:
            return Verdict(False,
                           f"this run has filed {already} issue(s) and the cap is "
                           f"{cap} — hand the rest to a human rather than the backlog",
                           "issue_filing.max_per_run",
                           {"filed": already, "max_per_run": cap})

        if _flag(self.rules, "issue_filing", "require_dedup_check"):
            if duplicates is None:
                return Verdict(False,
                               "no duplicate search was run, and this repo requires one",
                               "issue_filing.require_dedup_check")
            if duplicates:
                listed = ", ".join(f"#{d['number']}" for d in duplicates[:5])
                return Verdict(False,
                               f"the backlog may already hold this: {listed} — "
                               f"add to one of those, or file with a title that "
                               f"says how this differs",
                               "issue_filing.require_dedup_check",
                               {"candidates": duplicates[:5]})

        # An allow says which form of the cap it was granted under. Without a run
        # id the count is this process's alone, so a caller invoking the CLI once
        # per candidate gets `1/1` every time and the cap enforces nothing — a
        # bypass by omission, and one nobody would notice, because the weak form
        # and the strong form print the same "within budget" otherwise.
        weak = "" if self.run_id else " — NOT persisted: pass --run to enforce this " \
                                     "across separate invocations"
        return Verdict(True,
                       f"within budget "
                       f"({already + 1}/{cap if cap is not None else '∞'}){weak}",
                       "issue_filing.max_per_run",
                       {"filed": already, "max_per_run": cap,
                        "persisted": bool(self.run_id)})


# ---------------------------------------------------------------------- CLI

def _load(repo_spec: str | None) -> dict:
    try:
        return resolve_repo(repo_spec)
    except RepoNotFound as e:
        sys.exit(str(e))


def _emit(v: Verdict, as_json: bool) -> int:
    if as_json:
        print(json.dumps(v.as_dict(), indent=2))
    else:
        print(f"{'ALLOWED' if v.allowed else 'REFUSED'}: {v.reason}"
              + (f"  [{v.setting}]" if v.setting else ""))
    # Non-zero on refusal so a shell caller can gate on it without parsing.
    return 0 if v.allowed else 3


def cmd_pickup(args) -> int:
    cfg = _load(args.repo)
    if not args.as_json:
        print(describe(cfg))
    issue = gh_json(["issue", "view", str(args.number),
                     "--json", "number,labels,author"], cfg["github"])
    labels = label_names(issue.get("labels"))
    author = (issue.get("author") or {}).get("login", "")
    # Only fetched when it can change the answer — the event log is a paginated
    # call per issue, and a watcher sweeping a backlog makes one per candidate.
    events = label_events(cfg["github"], args.number) if events_needed(cfg) else []
    return _emit(pickup_verdict(cfg, {"number": args.number, "labels": labels,
                                      "author": author}, events=events),
                 args.as_json)


def cmd_file(args) -> int:
    cfg = _load(args.repo)
    if not args.as_json:
        print(describe(cfg))
    budget = FilingBudget(cfg, repo=cfg["github"], run_id=args.run)
    dups = None
    if _flag(_rules(cfg, "issue_filing"), "issue_filing", "require_dedup_check"):
        dups = duplicate_search(cfg["github"], args.title)
    v = budget.check(args.title, duplicates=dups)
    if v.allowed and args.record:
        budget.record(args.title)
    return _emit(v, args.as_json)


def main(argv: list[str] | None = None) -> int:
    # The shared flags are accepted on EITHER side of the subcommand. argparse
    # puts a top-level option before the subcommand only, which makes the obvious
    # `appetite.py pickup 85 --repo x` an unrecognized-arguments error — and a
    # usage failure for a gate reads as the gate being broken, so the next agent
    # works around it rather than reordering the words.
    #
    # The subcommand copies default to SUPPRESS on purpose. A parent parser's
    # ordinary default is re-applied by the subparser and would overwrite what
    # the top-level parse already put there, so `--repo x pickup 85` would silently
    # become repo=None — the flag accepted, ignored, and the gate answering about
    # the wrong repo. SUPPRESS leaves the attribute alone unless the flag is
    # actually given after the subcommand.
    def shared(parser, *, suppress=False):
        extra = {"default": argparse.SUPPRESS} if suppress else {}
        parser.add_argument("--repo", **extra,
                            help="repo path, or a name under ~/source (default: cwd)")
        parser.add_argument("--json", action="store_true", dest="as_json", **extra,
                            help="machine-readable verdict on stdout")
        return parser

    common = shared(argparse.ArgumentParser(add_help=False), suppress=True)

    ap = argparse.ArgumentParser(
        description="The appetite gates: what a loop may pick up, and how much "
                    "it may file (issue_pickup / issue_filing in the rules file).")
    shared(ap)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("pickup", parents=[common],
                       help="may a loop work this issue unprompted?")
    p.add_argument("number", type=int)
    p.set_defaults(func=cmd_pickup)

    f = sub.add_parser("file", parents=[common],
                       help="may this run file an issue with this title?")
    f.add_argument("--title", required=True)
    f.add_argument("--run", default="", metavar="ID",
                   help="run identifier the per-run tally is kept under. Without "
                        "it max_per_run cannot be enforced across separate "
                        "invocations and only this process's count applies.")
    f.add_argument("--record", action="store_true",
                   help="count this against the run's budget (pass after filing)")
    f.set_defaults(func=cmd_file)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
