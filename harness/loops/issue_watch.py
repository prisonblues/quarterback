#!/usr/bin/env python3
"""The issue watcher — #63. What it mostly does is decline, and that is the point.

Watch a repo's open issues and say, for each one, what it is waiting on: a human
decision, a dependency, a triage ruling, or nothing at all. It **reports**. It
writes no code, opens no PR and starts no session — see "IT STARTS NOTHING".

WHY THE REFUSAL IS THE DELIVERABLE.

#63 measured it: of the twenty-one issues filed here on one day, several existed
precisely to force a decision — an explicit *Open questions* section, or three
options and no recommendation. An agent handed one of those with `/fix-issue`
does not stop. It picks an architecture, implements it, opens a PR, and the
decision has been made by whichever model was cheapest that morning. #51 was
perfectly implementable and must not have been implemented.

So acting is what needs justifying here and refusing does not, and the two halves
of that are deliberately separate questions:

    THE GATE          May a loop CHOOSE this issue at all? Not this module's
                      question — `appetite.pickup_verdict` already answers it,
                      and there is exactly one such gate on the fleet.
    DECISION OWED     Has a human settled WHAT TO DO? `epic.triage` answers
                      "can an agent do this", which is a different question:
                      that is why an implementable issue can still be one no
                      agent may touch.

Both must say yes before an action is even *named*. The survey half runs
regardless, because "these six are waiting on you, here is what each needs" is
useful work on a repo whose gate is shut — which is every repo, by default.

IT STARTS NOTHING UNLESS TWO SEPARATE PARTIES BOTH SAY SO.

It used to start nothing at all, and the docstring here said so as a structural
property: the only command the module could run was `gh`. That was the first cut
of #63 on purpose — the survey half is useful on a repo whose gate is shut, which
is every repo by default — and this is the follow-up it deferred, so the property
is now narrower and worth stating exactly.

`--start` hands an actionable issue to `qb-start` (#277) and nothing else does.
Reaching a session takes FOUR yeses, and no two of them are written down in the
same place:

    THE REPO      `issue_pickup.enabled`, false in `harness_rules.DEFAULTS` and
                  in this repo's `.harness-rules.sample`. Decides whether a loop
                  may CHOOSE work here at all.
    THE ISSUE     no held signal, and `epic.triage` confirming an agent can do
                  it. Decides whether THIS issue is settled.
    THE OPERATOR  `--start`, which is off, plus `--start-max`. Decides whether
                  THIS RUN may act.
    THE MACHINE   `qb-start`'s policy file, which lives in the user's config
                  directory outside any repository, ships absent, and fails
                  closed. Decides whether this BOX starts sessions.

BE PRECISE ABOUT WHAT THE LAST ONE BUYS, because it is easy to oversell and an
earlier draft of this docstring did. It said a poisoned repo that flipped every
switch it owns still starts nothing. That is true of everything the repo can
WRITE and false of what the repo can SAY: a `CLAUDE.md` is repository content
too, and it is read by an agent that holds a shell as this user — an agent that
could create `spawn.json` itself, shadow `qb-start` on `PATH`, or simply run the
agent binary directly and skip all of this.

So the property is narrower and worth stating in the form that is actually true:
**nothing here reads repository-controlled content as policy.** No file in a
checkout is consulted to decide whether a session may start. What that stops is
the tracker becoming an authorisation channel — the specific thing #63 was filed
about. What it cannot stop is a party that already has arbitrary execution as
this user, and no same-UID permission gate can; `qb-start`'s own notes make this
argument about `XDG_CONFIG_HOME` and it applies here unchanged. Moving that
boundary means an authority outside this UID, which is a different issue.

THE AUDIT IS A DRIFT GUARD, NOT A SECURITY CONTROL. `test_issue_watch.py` reads
every subprocess call off this module's syntax tree and permits exactly `gh` and
`qb_start_path()`. It catches the accident this file is one careless edit away
from — a command assembled out of a computed string — and it caught exactly that
while this was being written. It is not a boundary against a hostile committer,
who would edit the test in the same commit, and `from subprocess import call`
walks past it. Worth having, worth not mistaking for more than it is.

The judge is the one thing that costs money, so it runs LAST: only for an issue
the gate admitted and no signal held. That ordering is also the security
property. This repo is public and anyone may open an issue; under a watcher that
text becomes an agent's instructions, and the mitigation is
`issue_pickup.allowed_authors` — an allowlist, not a filter, because a filter is
a list of the phrasings somebody already thought of. A stranger's issue is
surveyed and reported and its text is never shown to a model.

Usage:
    python3 issue_watch.py --repo quarterback              # survey the backlog
    python3 issue_watch.py --repo quarterback --issue 63    # one issue
    python3 issue_watch.py --repo quarterback --json        # machine-readable
    python3 issue_watch.py --repo quarterback --announce     # say so on the board
    python3 issue_watch.py --repo quarterback --comment      # ...and on the issue
    python3 issue_watch.py --repo quarterback --start --dry-run   # every refusal, nothing started
    python3 issue_watch.py --repo quarterback --start        # ...and act on the actionable
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from appetite import (  # noqa: E402
    Verdict,
    author_verdict,
    events_needed,
    gh_json,
    label_events,
    label_names,
    pickup_verdict,
    refusal_verdict,
    unattended_writes_allowed,
)
from epic import DEP_RE, IssueWork, resolve_ceiling, triage  # noqa: E402
from harness_rules import RepoNotFound, describe, resolve_repo, unattended  # noqa: E402
from needs_human import announce, class_or_none, digest  # noqa: E402

#: Everything one `gh issue list` can hand back, which is the whole survey. The
#: comments come with it — `--json comments` is populated on `issue list`, so the
#: obvious loop of one `issue view` per issue is a call per issue for nothing.
SURVEY_FIELDS = "number,title,body,author,labels,comments,url,state"

#: How many issues a survey reads unless told otherwise. A ceiling rather than
#: everything: a watcher on a timer against a thousand-issue backlog is a
#: different program, and the honest version of that is paging, not a bigger
#: number here.
DEFAULT_LIMIT = 30

#: How many sessions ONE `--start` run may begin. One, deliberately, and it is
#: not the same ceiling as `qb-start`'s own.
#:
#: `qb-start` caps how many spawns may be LIVE on a box; this caps how many a
#: single sweep may ask for, and the two answer different questions. Without
#: this, a first run against a backlog where the gate has just been opened would
#: ask for a session per actionable issue and be refused by the cap thirty times
#: — thirty board posts, thirty claims attempted, to start the one session the
#: box had room for. The number that cannot surprise anybody is the smallest one
#: that does something, which is `qb-start`'s argument for its own default and
#: is more forceful here: this is the end of the chain that reads a public
#: tracker.
DEFAULT_START_MAX = 1

#: How many SPAWN REQUESTS one run may make — a request that started a session
#: and one that was refused both count.
#:
#: "Spawn requests", not "invocations of qb-start", and the distinction is real
#: rather than pedantic: `spawning_enabled` asks `qb-start --policy` once per
#: run and that call is NOT counted. It is a question, not a request — it posts
#: nothing, claims nothing, starts nothing, and reads only a local file — so
#: counting it would spend a budget that exists to bound board posts and claim
#: attempts on something that makes neither. An earlier draft of this comment
#: said "may call qb-start at all", which was simply false: `--attempt-max 5`
#: permitted six calls. A run whose budget is zero now returns before the probe,
#: so the one case where "asks nothing" is claimed is the one case it is true.
#:
#: A second budget rather than a bigger first one, because `--start-max` counts
#: the wrong thing to bound this. A refusal that is about a single issue —
#: somebody else holds it — correctly does not stop the sweep and correctly
#: starts no session, so it spends none of `--start-max`. Thirty held issues
#: therefore made thirty `qb-start` invocations and thirty board posts while
#: `--start-max 1` appeared to be holding: exactly the runaway that flag's own
#: docstring claimed to prevent. Measured, not theorised — a codex review of
#: this file caught the contradiction between the two.
#:
#: Five, not one: the point is to bound a runaway, not to give up on the first
#: issue a peer happens to hold. Small enough that the pathological case is a
#: handful of posts rather than a backlog's worth.
DEFAULT_ATTEMPT_MAX = 5

#: Left in the comment this writes, so a second run recognises its own work. It
#: carries a digest of the signals, not just a marker: an issue whose open
#: questions have been answered and replaced by new ones is a NEW thing to say,
#: and a bare marker would suppress it forever.
COMMENT_MARKER = "<!-- quarterback:issue-watch"


# ------------------------------------------------------------------- reading


@dataclass
class Signal:
    """One reason a human decision is still owed, and the evidence for it.

    The evidence is not decoration. "A decision is owed" with nothing behind it
    is the confident assertion #67 warns about, and on a queue it costs somebody
    an interruption; `needs_human.announce` refuses a flag that carries no
    reason for the same reason.
    """

    name: str
    detail: str

    def as_dict(self) -> dict:
        return {"signal": self.name, "detail": self.detail}


@dataclass
class Assessment:
    """What one issue is waiting on, and what — if anything — could be run on it."""

    number: int
    title: str
    author: str
    url: str = ""
    labels: list[str] = field(default_factory=list)
    gate: Verdict | None = None
    signals: list[Signal] = field(default_factory=list)
    doable: bool | None = None
    reason: str = ""
    human_class: str = ""
    action: str = "none"
    why: str = ""
    #: What became of `action` — the record #63 asks for by name, so that "why
    #: did nothing happen" is answerable after the fact rather than reconstructed
    #: from a pane that may not exist. Empty on a survey, because a run that was
    #: never asked to start anything did not decline to: absent and refused are
    #: different answers and this field must not conflate them.
    started: str = ""
    #: The thread as `gh` handed it over, kept so `--comment` can recognise this
    #: watcher's own earlier comment without a second round trip. Not in
    #: `as_dict`: it is the input the assessment was made from, and a JSON
    #: consumer asking what an issue is waiting on did not ask for the issue.
    comments: list[dict] = field(default_factory=list, repr=False)

    @property
    def held(self) -> bool:
        return self.action == "none"

    def as_dict(self) -> dict:
        return {
            "number": self.number, "title": self.title, "author": self.author,
            "url": self.url, "labels": list(self.labels),
            "gate": self.gate.as_dict() if self.gate else None,
            "signals": [s.as_dict() for s in self.signals],
            "doable": self.doable, "triage_reason": self.reason,
            "needs_human": self.human_class,
            "action": self.action, "why": self.why,
            "started": self.started,
        }


def strip_code(text: str) -> str:
    """`text` with fenced and inline code removed.

    Every detector below is a regex over prose, and a repro block is prose-shaped
    noise: a traceback holds question marks, a config sample holds the word
    "option", and a shell snippet holds `#123`. Counting those is how a defect
    with a good repro — the one issue class this whole module exists to let
    through — reads as a decision nobody has made.

    Fenced and inline only, deliberately not markdown's four-space indented
    block: on GitHub that indentation is far more often a nested list item, and
    stripping those would swallow the bullets UNDER an "Open questions" heading —
    a miss, in the one direction this module must not miss in.
    """
    out = re.sub(r"```.*?```", " ", text or "", flags=re.S)
    return re.sub(r"`[^`\n]*`", " ", out)


def utterances(issue: dict) -> list[tuple[str, str]]:
    """Everything a person said on the issue, oldest first, as `(who, what)`.

    The body is the author's first utterance and is treated as one, so "the last
    thing said here is a question" has an answer on an issue with no comments.

    **This watcher's own comments are dropped**, and that is a correctness fix
    rather than tidiness. Its refusal comment would otherwise be the newest thing
    on the thread, so the question it was posted to point out would read as
    answered — by the refusal itself — and the next run would find the issue
    actionable. A loop that clears its own hold by complaining about it is the
    self-approval hole `require_human_triage` exists to close, one surface along.
    """
    said = [(str((issue.get("author") or {}).get("login", "")),
             strip_code(issue.get("body") or ""))]
    for c in issue.get("comments") or []:
        text = c.get("body") or ""
        if COMMENT_MARKER in text:
            continue
        who = str((c.get("author") or {}).get("login", ""))
        said.append((who, strip_code(text)))
    return said


def trusted(cfg: dict, who: str) -> bool:
    """May this login's words CLEAR a hold on an issue?

    `issue_pickup.allowed_authors` again, and the asymmetry is the whole point:
    **anyone's text may raise a signal, and only an allowlisted author's may
    settle one.** Anyone can comment on an approved author's issue — #63's own
    decision comment says so — so a rule that only bounded who may OPEN an issue
    would leave "**Decided:** option B" as a sentence a stranger can write to
    make an agent start work. Raising a hold costs a report line; clearing one
    spends money and writes code.
    """
    return author_verdict(cfg, who).allowed


# ------------------------------------------------- the decision-owed signals
#
# The five #63 names, plus the label. Each is a regex over prose and each is
# deliberately eager: a false positive costs a report line a human reads and
# waves through, and a false negative costs an unwanted PR against a question
# nobody answered. Those are not the same price.

#: An explicit "Open questions" section — a heading, a bold line, or a list
#: header. #63's own evidence: #51 asks six of them, #55 asks what spend is
#: measured in.
OPEN_QUESTIONS_RE = re.compile(
    r"^\s*(?:#{1,6}\s*|\*{1,2}\s*|[-*+]\s*)*"
    r"(?:open|unresolved|outstanding|remaining)\s+questions?\b", re.I | re.M)

#: "Option A" / "**Option 2**" / "- option three". Two distinct ones with no
#: recommendation is #43's shape: three options and none recommended.
OPTION_RE = re.compile(
    r"^\s*(?:[-*+]\s*|\d+[.)]\s*)?(?:#{1,6}\s*)?\*{0,2}\s*"
    r"option\s+([A-Za-z0-9]+)\b", re.I | re.M)

#: What counts as somebody having decided. Deliberately NARROW — a loose reading
#: of "recommendation" is what stops this signal firing, and not firing is the
#: direction that lets an undecided issue through. A passing "we could recommend
#: …" must not read as a ruling, so it wants a line that announces itself.
DECIDED_RE = re.compile(
    r"(?:^\s*\**\s*(?:decided|decision|recommendation|resolved|chosen|verdict)\b"
    r"\s*\**\s*:?"
    r"|\bI recommend\b|\bwe(?:'re| are| will)? go(?:ing)? with\b"
    r"|\bthe answer is\b)", re.I | re.M)

#: A sentence that asks something. Bounded at both ends: shorter than eight
#: characters is punctuation rather than a question, and a "sentence" longer than
#: 240 without terminal punctuation is a paragraph that happens to end in one.
QUESTION_RE = re.compile(r"[^.!?\n]{8,240}\?")

#: The issue's own shape — "which of these three?" rather than "X is broken, here
#: is the repro". Phrases that put a choice to a reader, not merely uncertainty.
CHOICE_RE = re.compile(
    r"\b(?:which of (?:these|the|them)|should we\b|should I\b|or should\b"
    r"|do we want\b|is it worth\b|worth doing\b|pick one\b"
    r"|one of (?:these|the following)|either way\b|open question\b)", re.I)

#: A well-specified defect states how to see it and how to know it is fixed.
#: Only ever used to ESCALATE from `/investigate` to `/fix-issue`, so a miss
#: costs a read-only run and a false hit costs a PR — hence headings a person
#: wrote on purpose, not a keyword anywhere in the prose.
SPEC_RE = re.compile(
    r"^\s*(?:#{1,6}\s*|\*{1,2}\s*)*"
    r"(?:acceptance(?:\s+criteria)?|steps?\s+to\s+reproduce|reproduction|repro"
    r"|expected(?:\s+(?:behaviour|behavior|result))?"
    r"|actual(?:\s+(?:behaviour|behavior|result))?)\b", re.I | re.M)


def open_questions(body: str) -> Signal | None:
    """An explicit *Open questions* section."""
    m = OPEN_QUESTIONS_RE.search(body)
    if not m:
        return None
    return Signal("open_questions",
                  f"the body has an {m.group(0).strip().lstrip('#*- ')!r} section")


def unrecommended_options(cfg: dict, said: list[tuple[str, str]]) -> Signal | None:
    """Two or more options laid out, and nobody who may decide has picked one.

    Read over the whole thread and not just the body, because deciding is what
    comments are for — #63's own decision arrived as a comment beginning
    "**Decided:**", and an issue whose question has since been answered must stop
    reading as one that has not.

    Only a `trusted` author's ruling counts. Anyone can comment on anyone's
    issue, so without that the phrase "**Decided:** option B" is a sentence a
    stranger can write on a public repo to take the brake off.
    """
    body = said[0][1] if said else ""
    names = {m.group(1).casefold() for m in OPTION_RE.finditer(body)}
    if len(names) < 2:
        return None
    if any(DECIDED_RE.search(text) for who, text in said if trusted(cfg, who)):
        return None
    listed = ", ".join(sorted(names))
    return Signal("unrecommended_options",
                  f"{len(names)} options ({listed}) and nothing in the thread "
                  f"reads as a ruling on them")


def questions(text: str) -> list[str]:
    """The question sentences in `text`, in order, whitespace collapsed."""
    # Leading punctuation trimmed because a match begins wherever the previous
    # sentence ended, so a quote otherwise opens mid-clause on a stray bullet or
    # emphasis mark and reads as a truncation bug rather than a fragment.
    return [" ".join(q.split()).strip(" *_-,;:)>") for q in QUESTION_RE.findall(text)]


def unanswered_question(cfg: dict, said: list[tuple[str, str]]) -> Signal | None:
    """The newest question on this issue, with nobody who could answer it replying.

    Not "a question mark appears somewhere": a question answered three comments
    ago is settled, and treating it as open would hold every issue that ever
    discussed anything. So the last question in the thread is found, and it
    counts as answered only if a `trusted` author has said something after it.

    Both halves of that matter. A reply from anyone at all would let a stranger
    close a question on a public repo by saying anything underneath it; and this
    watcher's own refusal comment is not in `said` at all, or it would answer
    every question it was posted to point out.
    """
    asked = [(i, questions(text)) for i, (_who, text) in enumerate(said)]
    asked = [(i, qs) for i, qs in asked if qs]
    if not asked:
        return None
    at, qs = asked[-1]
    if any(trusted(cfg, who) for who, _text in said[at + 1:]):
        return None
    who = said[at][0]
    where = ("the body" if at == 0 else
             "the latest comment" if at == len(said) - 1 else "a comment")
    by = f" by {who}" if who else ""
    return Signal("unanswered_question",
                  f"{where}{by} asks something nobody has answered: "
                  f"{qs[-1][-160:]!r}")


def decision_shape(title: str, body: str) -> Signal | None:
    """The issue asks *which*, rather than reporting *what is broken*.

    The choice phrase has to appear inside a QUESTION, not merely somewhere in
    the prose. "which of the" turns up in ordinary description — a defect report
    explaining which of two branches wins reads as a decision request otherwise,
    and a signal that fires on most of the backlog tells a reader nothing about
    the six issues that are genuinely waiting on them.
    """
    if title.rstrip().endswith("?"):
        return Signal("decision_shape", "the title is a question")
    for q in questions(body):
        if CHOICE_RE.search(q):
            return Signal("decision_shape",
                          f"the body puts a choice rather than a defect: {q[-160:]!r}")
    return None


def dep_numbers(body: str, number: int) -> list[int]:
    """Issues this one says it depends on — `epic.DEP_RE`, not a second parser.

    That expression already reads "depends on #N", "blocked by #N", "once #N
    lands" and the three other spellings epics use, and #63 asks for exactly
    those. A parallel copy here is two parsers that agree today.
    """
    seen: list[int] = []
    for raw in DEP_RE.findall(strip_code(body)):
        n = int(raw)
        if n != number and n not in seen:
            seen.append(n)
    return seen


def dep_states(repo: str, numbers: list[int]) -> dict[int, str]:
    """`{number: OPEN|CLOSED}` for each dependency, omitting what could not be read.

    An unreadable state is left OUT rather than guessed, and `open_dependencies`
    treats absence as unresolved. A dependency whose state we failed to fetch is
    not a dependency we know is closed, and the difference decides whether an
    issue is held.
    """
    out: dict[int, str] = {}
    for n in numbers:
        got = subprocess.run(
            ["gh", "issue", "view", str(n), "--repo", repo, "--json", "state"],
            capture_output=True, text=True)
        if got.returncode != 0 or not got.stdout.strip():
            continue
        try:
            state = json.loads(got.stdout).get("state", "")
        except json.JSONDecodeError:
            continue
        if isinstance(state, str) and state:
            out[n] = state.upper()
    return out


def open_dependencies(number: int, body: str, states: dict[int, str]) -> Signal | None:
    """A dependency that has not landed — including one whose state is unknown."""
    deps = dep_numbers(body, number)
    if not deps:
        return None
    pending = [n for n in deps if states.get(n, "OPEN") != "CLOSED"]
    if not pending:
        return None
    listed = ", ".join(f"#{n}" for n in pending)
    return Signal("open_dependency",
                  f"the body says it depends on {listed}, which "
                  f"{'is' if len(pending) == 1 else 'are'} not closed")


def refusing_label(cfg: dict, labels, number: int) -> Signal | None:
    """A `needs-human/*` label a person applied — via `appetite.refusal_verdict`.

    Not re-implemented here, and not merely because the glob matching is fiddly:
    the skip list is repo policy and a second reader of it is a second policy.

    `unlabelled_refuses=False` on purpose. `skip_when_unlabelled` answers "may I
    select this out of an untriaged backlog", which is the GATE's question and
    `pickup_verdict` has already asked it. Asking it again here would report every
    unlabelled issue as one a human owes a decision on, which is not what an
    absent label means and would bury the six that do.
    """
    v = refusal_verdict(cfg, labels, number=number, unlabelled_refuses=False)
    if v.allowed:
        return None
    return Signal("needs_human_label", v.reason)


def decision_signals(cfg: dict, issue: dict, *,
                     states: dict[int, str] | None = None) -> list[Signal]:
    """Every reason a human decision is still owed on this issue.

    Pure: `states` is `dep_states`' output, passed in so every branch is testable
    without reaching GitHub and the one caller that must reach it does so once.
    """
    said = utterances(issue)
    body = said[0][1] if said else ""
    title = str(issue.get("title") or "")
    number = int(issue.get("number") or 0)
    found = [
        refusing_label(cfg, issue.get("labels"), number),
        open_questions(body),
        unrecommended_options(cfg, said),
        unanswered_question(cfg, said),
        open_dependencies(number, issue.get("body") or "", states or {}),
        decision_shape(title, body),
    ]
    return [s for s in found if s]


# ------------------------------------------------------------- what to run


def specified(body: str) -> bool:
    """Does the issue say how to see the defect and how to know it is fixed?"""
    return bool(SPEC_RE.search(strip_code(body)))


def choose_action(gate: Verdict, signals: list[Signal], doable: bool | None,
                  reason: str, body: str, state: str = "OPEN") -> tuple[str, str]:
    """The action this issue justifies, and why — `none` unless everything says yes.

    The ladder is #63's, and `/investigate` is the default rung rather than
    `/fix-issue`: it produces understanding and writes no code, so it is the
    safer answer whenever the choice is close. Escalating wants evidence that
    somebody wrote down what "fixed" means.
    """
    # Before the gate, because it is the one refusal that survives any config: a
    # backlog sweep only ever sees open issues, but `--issue 63` names one and
    # `gh issue view` answers about a closed one just as readily. Nothing is to
    # be implemented on an issue somebody has already closed.
    if state and state.upper() != "OPEN":
        return "none", f"the issue is {state.lower()}"
    if not gate.allowed:
        setting = f" [{gate.setting}]" if gate.setting else ""
        return "none", f"the pickup gate refused: {gate.reason}{setting}"
    if signals:
        named = ", ".join(s.name for s in signals)
        return "none", f"a human decision is owed ({named})"
    if doable is None:
        return "none", reason or "no triage judgement was possible"
    if doable is False:
        return "none", f"not agent-doable: {reason}"
    if specified(body):
        return "/fix-issue", (f"specified and agent-doable: {reason}"
                              if reason else "specified and agent-doable")
    return "/investigate", ("agent-doable, but nothing in the body says how to "
                            "know it is fixed — understanding first")


def assess(cfg: dict, issue: dict, *, events: list[dict] | None = None,
           states: dict[int, str] | None = None, model: str = "",
           judge=None) -> Assessment:
    """Everything this watcher can say about one issue, in the order that is safe.

    Gate, then signals, then — only if both said yes — the judge. That ordering
    is not tidiness: the judge is the only step that costs money and the only
    step that shows the issue's text to a model, and a stranger's issue must
    reach neither. `epic.py` orders its own two the same way and says so.
    """
    labels = label_names(issue.get("labels"))
    number = int(issue.get("number") or 0)
    author = str((issue.get("author") or {}).get("login", ""))
    title = str(issue.get("title") or "")
    body = str(issue.get("body") or "")
    # Absent means OPEN: `gh issue list --state open` does not always echo the
    # field back, and refusing everything a stub or an older `gh` did not report
    # would make the watcher silent rather than careful.
    state = str(issue.get("state") or "OPEN")

    a = Assessment(number=number, title=title, author=author,
                   url=str(issue.get("url") or ""), labels=labels,
                   comments=list(issue.get("comments") or []))
    a.gate = pickup_verdict(cfg, {"number": number, "labels": labels,
                                  "author": author}, events=events or [])
    a.signals = decision_signals(cfg, issue, states=states)

    if a.gate.allowed and not a.signals and state.upper() == "OPEN":
        # `ask`, not `run`: the audit in test_issue_watch.py reads every call
        # named `run` off this module's syntax tree to prove it launches nothing
        # but `gh`, and a local shadowing that name is a hole in the audit that
        # looks like a naming preference.
        ask = judge if judge is not None else triage
        work = IssueWork(num=number, title=title, checked=False,
                         issue_state=state.upper(), pr_number=None, pr_state=None,
                         stage="implement", body=body)
        a.doable, a.reason, _impl_model, a.human_class = ask(work, model)

    a.action, a.why = choose_action(a.gate, a.signals, a.doable, a.reason, body,
                                    state)
    if a.held and not a.human_class:
        a.human_class = held_class(a)
    return a


def held_class(a: Assessment) -> str:
    """Which of #279's six a held issue is waiting on.

    From the label when a person applied one — they said which kind, and
    overriding that with a guess is how a vocabulary grows a second meaning. Only
    then from the signals, and `decision` is right for all of them: every one is
    "which of these, or whether at all".
    """
    for name in a.labels:
        if name.casefold().startswith("needs-human/"):
            got = class_or_none(name.split("/", 1)[1])
            if got:
                return got
    return "decision" if a.signals else ""


# ---------------------------------------------------------------- the survey


def survey(cfg: dict, *, limit: int = DEFAULT_LIMIT, only: int | None = None,
           model: str = "", use_judge: bool = True, judge=None) -> list[Assessment]:
    """Assess the repo's open issues, newest first."""
    repo = cfg["github"]
    if only is not None:
        issues = gh_json(["issue", "view", str(only), "--json", SURVEY_FIELDS], repo)
        issues = [issues] if isinstance(issues, dict) else list(issues)
    else:
        issues = gh_json(["issue", "list", "--state", "open", "--limit", str(limit),
                          "--json", SURVEY_FIELDS], repo)

    # One event-log call per issue, and only where the answer can turn on it —
    # `require_human_triage` off, or the gate off entirely, and it cannot.
    want_events = events_needed(cfg)
    out = []
    # One state read per DEPENDENCY, not per mention: a backlog where six issues
    # all wait on the same one is six calls otherwise, and they cannot disagree.
    # `asked` and not `seen.keys()`, because an unreadable state is absent from
    # `seen` — retrying it once per mentioning issue would make the one failure
    # mode that costs calls also the one that costs the most.
    seen: dict[int, str] = {}
    asked: set[int] = set()
    for issue in issues:
        number = int(issue.get("number") or 0)
        deps = dep_numbers(issue.get("body") or "", number)
        fresh = [n for n in deps if n not in asked]
        seen.update(dep_states(repo, fresh))
        asked.update(fresh)
        states = {n: seen[n] for n in deps if n in seen}
        events = label_events(repo, number) if want_events else []
        out.append(assess(cfg, issue, events=events, states=states, model=model,
                          judge=(judge if use_judge else _no_judge)))
    return out


def _no_judge(_work, _model) -> tuple[bool | None, str, str, str]:
    """`--no-triage`: no judgement was made, said as such.

    `None` and not `False`: nobody ruled this undoable, we declined to ask. The
    reason lines in `epic.triage` exist to stop a verdict nobody obtained being
    claimed, and skipping the judge must not manufacture one either.

    No `needs_human` class for the same reason. `epic` files an unavailable judge
    as `environment` — is this box broken — and that is a real diagnosis about a
    real failure. Here the judge was not run because somebody said not to, and
    nobody is waiting on anything.
    """
    return None, "the triage judge was not run (--no-triage)", "", ""


# ------------------------------------------------------------- saying so


def announce_held(a: Assessment, cfg: dict, repo: str) -> str:
    """Tell the board what decision #`a.number` is waiting on. Returns a line."""
    reasons = "; ".join(f"{s.name}: {s.detail}" for s in a.signals) or a.why
    return announce(
        cls=a.human_class or "decision", reason=a.why,
        summary=f"#{a.number} {a.title[:80]} — not started",
        repo=repo, cfg=cfg,
        # The signal NAMES are in the key, not just the issue: an issue whose
        # open questions get answered and replaced by a dependency is a new thing
        # to say, and a key of issue-plus-class would swallow it for a day.
        key=f"issue-watch:{repo}:{a.number}:"
            f"{digest(a.human_class, *(s.name for s in a.signals))}",
        detail=(f"The issue watcher will not hand #{a.number} to an agent.\n\n"
                f"Why: {a.why}\n\n{reasons}\n\n"
                f"Nothing happens to this issue until a human answers it."),
        refs=[{"kind": "issue", "value": str(a.number), "repo": repo}])


def comment_body(a: Assessment) -> str:
    """What the watcher says on the issue itself.

    Silence is indistinguishable from a broken watcher — #63's own diagnosis of
    half the tracker — so a refusal names the decision that is missing, on the
    issue, where the person who can answer it is already looking.
    """
    lines = [f"{COMMENT_MARKER} "
             f"{digest(*(s.name for s in a.signals), a.action)} -->",
             "**Not started — a decision is owed.**", "",
             f"{a.why}.", ""]
    for s in a.signals:
        lines.append(f"- **{s.name}** — {s.detail}")
    lines += ["", "Nothing will run on this issue until that is answered. This "
                  "comment is from the issue watcher (`harness/loops/"
                  "issue_watch.py`), which reports and does not act."]
    return "\n".join(lines)


def already_said(a: Assessment, body: str) -> bool:
    """Has this exact thing already been said on this issue?

    Keyed on the digest in the marker rather than on the marker alone: a watcher
    that never repeats itself also never mentions the NEW question, and one that
    ignores its own comments posts the same paragraph every hour.
    """
    want = body.split("-->", 1)[0].strip()
    return any(want in (c.get("body") or "") for c in a.comments)


def post_comment(repo: str, a: Assessment, body: str) -> str:
    """Leave the comment. Returns a line to print."""
    got = subprocess.run(["gh", "issue", "comment", str(a.number), "--repo", repo,
                          "--body", body], capture_output=True, text=True)
    if got.returncode != 0:
        return f"    comment NOT posted: {(got.stderr or '').strip()[:160]}"
    return f"    said so on #{a.number}"


def may_write(cfg: dict) -> tuple[bool, str]:
    """May this run write to the tracker at all?

    An unattended run may still REPORT — that half was never the problem — but it
    does not comment unless the repo has said unattended runs may write to its
    tracker. Reusing `issue_filing.unattended` rather than inventing a second
    switch: a repo has already answered "may a loop write here with nobody
    watching", and two switches for one question is how they come to disagree.
    """
    if not unattended():
        return True, ""
    if unattended_writes_allowed(cfg):
        return True, ""
    return False, ("this run is unattended and issue_filing.unattended is false — "
                   "reporting only")


def may_write_on(cfg: dict, a: Assessment) -> tuple[bool, str]:
    """May this run write on *this* issue?

    `issue_pickup.allowed_authors`, and only that. Not `enabled`: that setting
    answers "may a loop CHOOSE its own work", and saying on an issue what
    decision it is waiting on chooses nothing — the useful first cut of #63 is a
    watcher that reports on a repo whose pickup gate is, and stays, shut.

    The allowlist does apply, because it answers the question that is actually
    being asked here: whose text may drive this machinery. This repo is public;
    a stranger who could make the watcher comment could make it quote them under
    the repo owner's account, and could make it comment a thousand times.
    """
    who = author_verdict(cfg, a.author)
    if who.allowed:
        return True, ""
    return False, f"{who.reason} [{who.setting}]"


# ------------------------------------------------------------------- acting


#: The one program this module may start besides `gh`, named once so that the
#: audit in `test_issue_watch.py` has a single constant to check rather than a
#: string repeated at each call site.
QB_START = "qb-start"


def qb_start_path() -> str:
    """`qb-start`, on PATH or beside this checkout's `harness/bin`.

    Takes no arguments ON PURPOSE, and that is the property the audit rests on
    rather than a style choice. A resolver with a parameter is one an issue body
    could eventually reach through — the caller passes a name, the name comes
    from somewhere, and "somewhere" on a public tracker is a stranger.

    What that buys is exactly one thing: the program NAME is fixed here and no
    caller can influence it. It is NOT "only one program can ever be returned" —
    an earlier draft said that and it was wrong, since `which` answers out of
    `PATH` and the fallback is a file on disk. Anyone who can shadow `PATH` or
    write into this checkout can change what runs, and they could equally run it
    themselves; see the module docstring on where that boundary actually sits.

    Same two-step resolution as `qb-start.sibling` and for its reason: a
    home-manager install has PATH, a bare checkout has only the file.
    """
    from shutil import which
    return which(QB_START) or str(
        Path(__file__).resolve().parent.parent / "bin" / QB_START)


#: What each of `qb-start`'s refusals means here, and — the load-bearing half —
#: whether it is worth trying the NEXT issue after seeing it.
#:
#: The distinction is per-issue versus per-machine. `HELD` means somebody else
#: has that one issue and the next may be free; `NOT_ALLOWED` means this machine
#: permits some commands and not this one, so an `/investigate` may still start
#: where a `/fix-issue` did not. Everything else is a fact about the box — not
#: enabled, out of slots, paced, no tmux — and re-asking it once per issue would
#: turn one refusal into thirty identical board posts, which is how a watcher
#: becomes the thing people mute.
STOP, GO = True, False

#: The two outcomes that mean a session exists (or would). Named rather than
#: written out at each comparison: `run_starts` decides whether a budget was
#: spent by matching on these, and a literal repeated at three sites is one that
#: can be reworded at two of them.
STARTED = "started"
WOULD_START = "would start (--dry-run)"

START_EXITS = {
    0: (STARTED, GO),
    2: ("qb-start rejected the arguments — that is a bug here, not a refusal", STOP),
    3: ("spawning is not enabled on this machine", STOP),
    4: ("this machine's policy does not allow that command", GO),
    5: ("this machine is at its spawn cap", STOP),
    6: ("the shared window is spent", STOP),
    7: ("this repo's in-flight window is full", STOP),
    8: ("somebody already holds that work", GO),
    9: ("could not start it — no tmux, or the pane could not be stamped", STOP),
}


def start_one(a: Assessment, repo_path: str,
              dry_run: bool = False) -> tuple[str, bool]:
    """Ask `qb-start` for a session on `a.action`. Returns `(what happened, stop)`.

    Every gate that matters is somebody else's and is left that way. The pickup
    gate already said this issue may be chosen, the judge already said an agent
    can do it, and no signal is holding it — that is what `a.held` being false
    means. What is NOT re-decided here is whether this machine may start a
    session at all: that is `qb-start`'s policy file, it lives outside any
    repository, and a second copy of the check in here would be a second thing to
    keep in step with it. So this asks and reports the answer.
    """
    # The list is written out HERE rather than built above and passed in, and
    # that is the audit's requirement rather than a preference: a call whose argv
    # arrived as a variable cannot be read off the syntax tree, so
    # `test_issue_watch.py` refuses one — which is the whole value of the check.
    try:
        code = subprocess.run(
            [qb_start_path(), "--via", "watch", a.action, str(a.number),
             "--repo-path", repo_path, "--quiet",
             *(("--dry-run",) if dry_run else ())],
            stdin=subprocess.DEVNULL, timeout=120).returncode
    except Exception as e:                                        # noqa: BLE001
        # Named rather than swallowed: "nothing started" with no reason is the
        # silent-watcher failure #63 is largely about, and a missing install
        # reads very differently from a refusal.
        return f"qb-start did not run — {e.__class__.__name__}: {e}", STOP
    label, stop = START_EXITS.get(code, (f"qb-start exited {code}", STOP))
    if code == 0 and dry_run:
        return WOULD_START, GO
    return label, stop


def run_starts(assessments: list[Assessment], cfg: dict, *,
               limit: int = 1, attempts_max: int = DEFAULT_ATTEMPT_MAX,
               dry_run: bool = False) -> None:
    """Hand the actionable issues to `qb-start`, in order, within both budgets.

    TWO budgets, because one cannot express this. `limit` counts SESSIONS
    STARTED and `attempts_max` counts SPAWN REQUESTS made (the one-off
    `--policy` probe is a question, not a request, and is not counted), and the gap between
    them is where the first version of this was wrong: a refusal that is about
    one issue (somebody holds it) correctly does not stop the sweep, so with a
    single budget counting successes, thirty held issues produced thirty
    invocations and thirty board posts while `--start-max 1` looked like it was
    holding. That is precisely the runaway the cap was written to prevent, so it
    now has a counter that a refusal actually spends.

    A command refused by the machine's policy is remembered rather than
    re-asked. Exit 4 is a fact about (this box, this command) and not about the
    issue, so asking it once per issue is the same mistake in miniature — and
    unlike a held issue there is no chance at all that the next one differs.

    Records the outcome on every actionable assessment — including the ones it
    never reached, which get `not attempted` rather than being left blank. That
    is the difference between "the watcher declined this" and "the watcher ran
    out of room before it got here", and #63's acceptance asks for the second to
    be answerable too.
    """
    actionable = [a for a in assessments if not a.held]
    if not actionable:
        return
    # "Start it with a human watching" is the plan's own instruction about this
    # feature, and this is where it is encoded rather than left as advice. An
    # unattended run may still survey, report and announce — none of that starts
    # anything — but it does not spawn unless the repo has said loops may act
    # here with nobody looking. Reusing `may_write` rather than adding a switch:
    # a repo has answered this question once already, and two switches for one
    # question is how they come to disagree.
    # Before the policy probe, because a run whose budget is zero has nothing to
    # ask about. `spawning_enabled` is cheap and read-only, but a freeze that
    # still went and knocked would make "asks nothing" false — and that sentence
    # is the whole of what an operator typing `--start-max 0` is buying.
    if limit <= 0 or attempts_max <= 0:
        spent = "--start-max" if limit <= 0 else "--attempt-max"
        for a in actionable:
            a.started = (f"not attempted — this run's {spent} of "
                         f"{limit if limit <= 0 else attempts_max} is spent")
        return
    attended, unattended_why = may_write(cfg)
    if not attended:
        for a in actionable:
            a.started = f"not started — {unattended_why}"
        return
    enabled, why = spawning_enabled()
    if not enabled:
        for a in actionable:
            a.started = why
        return
    started = attempts = 0
    stopped = ""
    refused: dict[str, str] = {}
    for a in actionable:
        if stopped:
            a.started = f"not attempted — {stopped}"
            continue
        if started >= limit:
            a.started = f"not attempted — this run's --start-max of {limit} is spent"
            continue
        if attempts >= attempts_max:
            a.started = ("not attempted — this run's --attempt-max of "
                         f"{attempts_max} is spent")
            continue
        if a.action in refused:
            a.started = f"not attempted — {refused[a.action]}"
            continue
        a.started, stop = start_one(a, str(cfg.get("path") or "."),
                                    dry_run=dry_run)
        attempts += 1
        if a.started in (STARTED, WOULD_START):
            started += 1
        elif a.started == START_EXITS[4][0]:
            refused[a.action] = f"{a.started} ({a.action})"
        if stop:
            stopped = a.started


def spawning_enabled() -> tuple[bool, str]:
    """Does this machine start sessions at all? Asked once, before the loop.

    Takes no repo path because the answer does not depend on one: the policy is
    a property of the machine, and a repository cannot grant it.

    `qb-start --policy` exists for exactly this caller — it is the question a
    trigger asks before it offers somebody a button, it consults nothing but the
    policy file, and it is the SAME `read_policy` the spawn path uses, so it can
    never answer yes to a machine the spawn would refuse. Asking it once turns
    the commonest outcome by far — a machine that never opted in — into one line
    instead of one refusal per actionable issue.
    """
    try:
        code = subprocess.run([qb_start_path(), "--policy", "--quiet"],
                              stdin=subprocess.DEVNULL, timeout=30).returncode
    except Exception as e:                                        # noqa: BLE001
        return False, f"qb-start did not run — {e.__class__.__name__}: {e}"
    if code == 0:
        return True, ""
    return False, ("spawning is not enabled on this machine (qb-start --policy "
                   f"exited {code}) — nothing will be started")


# ------------------------------------------------------------------ output


def render(assessments: list[Assessment], repo: str,
           starting: bool = False) -> str:
    """The report a human reads: what each issue is waiting on.

    The closing line is computed rather than fixed. It read "Nothing was
    started" unconditionally while nothing could be, which was true then and
    would be a lie the moment `--start` existed — and a footer that says the
    opposite of the lines above it is worse than no footer, because it is the
    part somebody skims.
    """
    actionable = [a for a in assessments if not a.held]
    lines = [f"{repo} — {len(assessments)} open issue(s), "
             f"{len(actionable)} actionable"]
    for a in assessments:
        lines.append("")
        lines.append(f"  #{a.number}  {a.action:<12} {a.title[:70]}")
        lines.append(f"      {a.why}")
        for s in a.signals:
            lines.append(f"      · {s.name}: {s.detail}")
        if a.started:
            lines.append(f"      → {a.started}")
    if not starting:
        lines += ["", "Nothing was started: this run was not asked to (--start). "
                      "Acting on an issue is `qb-start`'s job."]
    else:
        began = [a for a in assessments if a.started == STARTED]
        lines += ["", f"{len(began)} session(s) started of {len(actionable)} "
                      "actionable."]
    return "\n".join(lines)


def _load(spec: str | None) -> dict:
    try:
        return resolve_repo(spec)
    except RepoNotFound as e:
        sys.exit(str(e))


def _ceiling(text: str) -> int:
    """A non-negative ceiling, refused at the CLI rather than absorbed.

    A negative `--start-max` used to be accepted, and it FAILED CLOSED — the
    first `started >= limit` was already true — so it started nothing and
    reported "this run's --start-max of -1 is spent". Safe, and still wrong: a
    ceiling that silently reinterprets a typo as a freeze is one nobody can tell
    from a working one, and the operator who wrote `-1` meaning "no limit" got
    the opposite of what they asked for without being told. Zero is a legitimate
    freeze and stays legal; below zero is a mistake and is named as one.
    """
    try:
        got = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not a whole number")
    if got < 0:
        raise argparse.ArgumentTypeError(
            f"{got} is negative — use 0 to start nothing")
    return got


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Survey a repo's open issues: what each one is waiting on, "
                    "and what — if anything — could be run on it. Reports only.")
    ap.add_argument("--repo", help="repo path, or a name under ~/source (default: cwd)")
    ap.add_argument("--issue", type=int, metavar="N",
                    help="assess one issue rather than the backlog")
    ap.add_argument("--limit", type=int, default=DEFAULT_LIMIT,
                    help=f"how many open issues to read (default {DEFAULT_LIMIT})")
    ap.add_argument("--model", default="",
                    help="triage judge tier (default: the repo's epic.model_ceiling)")
    ap.add_argument("--no-triage", action="store_false", dest="triage",
                    help="skip the judge — no issue is confirmed doable")
    ap.add_argument("--announce", action="store_true",
                    help="post held issues to the board as needs-human")
    ap.add_argument("--comment", action="store_true",
                    help="say on the issue itself what decision is missing")
    ap.add_argument("--json", action="store_true", dest="as_json",
                    help="machine-readable assessments on stdout")
    ap.add_argument("--start", action="store_true",
                    help="hand each actionable issue to qb-start. Off by "
                         "default, and refused anyway unless this machine's "
                         "spawn policy is enabled — see qb-start --policy")
    ap.add_argument("--start-max", type=_ceiling, default=DEFAULT_START_MAX,
                    metavar="N",
                    help=f"how many sessions one run may start (default "
                         f"{DEFAULT_START_MAX}; 0 starts nothing)")
    ap.add_argument("--attempt-max", type=_ceiling, default=DEFAULT_ATTEMPT_MAX,
                    metavar="N",
                    help=f"how many spawn requests one run may make, started "
                         f"or refused (default {DEFAULT_ATTEMPT_MAX}); the "
                         f"one-off policy probe is not one")
    ap.add_argument("--dry-run", action="store_true",
                    help="with --start: make every refusal and print what would "
                         "run, but start nothing")
    args = ap.parse_args(argv)

    cfg = _load(args.repo)
    repo = cfg["github"]
    if not args.as_json:
        print(describe(cfg))
    got = survey(cfg, limit=args.limit, only=args.issue,
                 model=resolve_ceiling(cfg, args.model), use_judge=args.triage)

    # BEFORE either report, so that what happened to each issue is in the report
    # rather than in a second stream a reader has to interleave by hand.
    if args.start:
        run_starts(got, cfg, limit=args.start_max,
                   attempts_max=args.attempt_max, dry_run=args.dry_run)

    if args.as_json:
        print(json.dumps({"repo": repo,
                          "issues": [a.as_dict() for a in got]}, indent=2))
    else:
        print(render(got, repo, starting=args.start))

    if args.announce or args.comment:
        attended, unattended_why = may_write(cfg)
        for a in got:
            # Only an issue a SIGNAL held. A gate refusal is the repo's standing
            # answer and not a question about this issue, so announcing one per
            # issue would post the whole backlog to the board the first time a
            # watcher ran — and would say "a human decision is owed" about issues
            # where none is.
            if not (a.held and a.signals):
                continue
            allowed, why = may_write_on(cfg, a)
            if not allowed:
                print(f"    #{a.number}: not written on — {why}")
                continue
            if args.announce:
                said = announce_held(a, cfg, repo)
                if said:
                    print(f"    {said}")
            if not args.comment:
                continue
            if not attended:
                print(f"    #{a.number}: {unattended_why}")
                continue
            body = comment_body(a)
            print(f"    #{a.number}: already said" if already_said(a, body)
                  else post_comment(repo, a, body))

    # A single-issue run is a verdict and exits like one, so a shell caller can
    # gate on it without parsing — `appetite.py`'s convention. A survey is not a
    # verdict: a backlog with nothing actionable is the healthy state, and
    # exiting non-zero for it would read as the watcher being broken.
    if args.issue is not None and got and got[0].held:
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
