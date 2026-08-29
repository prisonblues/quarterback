#!/usr/bin/env python3
"""qb-dash-tui — the fleet dashboard, clickable.

The same two tables as qb-dash — AGENTS and WORK — but as a Textual app, so rows
respond to the mouse. What a click does depends on what you clicked:

  a seat        jump the tmux cursor to that seat's pane — the dashboard is a
                switcher, which is the whole reason to have it beside the seats.
                Its ✕ closes the pane; the ＋ under the last row adds one. Both
                go through qb-seat-click, so they mean exactly what the same
                widgets on the tmux seat bar mean
  an agent      its cwd, branch, model and session id, in the detail line
  a claim       the note the claiming agent left, which for a claim whose agent
                has gone is the only record of it left
  a work row    why it is where it is — a plan item's note and what it waits on,
                or what review is waiting for, or the issue on GitHub — and its
                verb column takes the issue (⚒) or starts the round (⚖)
  a dial        why it is set, or its ✎ to set or clear it

TWO TABLES WHERE THERE WERE SEVEN (#589). SEATS, FLEET and CLAIMED were three
views of one subject — a seat is an agent with a pane in front of you, a claim is
what an agent holds — and PLANS, OPEN PRs, REVIEW QUEUE and ISSUES were four
answers to one question, two of which printed the same rows: the review queue is
DERIVED from the open-PR list, so it was a subset by construction. `qb-dash.py`'s
module docstring has the measurement and what is deliberately still not merged.

The ⚒ goes through `qb-start` (#371), so what it starts is counted by `qb-admit`,
holds a claim taken before the process exists, is endable by session id from the
moment the pane appears, and is recorded on the board as `via dash`. It therefore
also inherits `qb-start`'s gate: on a machine that has not opted in — which is
every machine by default — the ⚒ refuses and names the one line of nix that turns
it on. It does not fall back to starting an uncounted session;
`Dash.spawn_refusal` is where that decision is argued.

The ⚖ still starts its review directly, and that is not an oversight: a panel
review lands in a PANE of the seat row, beside the work it is about, and
`qb-start` makes windows. Giving it a placement argument is a bigger change than
#371, and the ⚒ is where the loop needed a beginning.

Keys: r refresh now, o open the selected row, p panel-review it, f take its
issue, w only what a person owes an answer about, b the backlog nothing is
waiting on, s widen or narrow the scope, q quit.

It opens NARROW: the rows of the project this screen is for (`--repo`, else
QB_DASH_REPOS, else the cwd's origin), with the repo column dropped, because on a
one-project screen that column is the same word on every row and the pane is
78 columns wide (#261). `s` widens it to the whole fleet and brings the column
back; QB_DASH_SCOPE=all opens that way. It also opens with the backlog HIDDEN —
the open PRs review has finished with, and the issues nobody has taken — because
those are a catalogue rather than state; `b` shows them and QB_DASH_BACKLOG=1
opens that way, and their counts are on the header line regardless.

It also opens in ONE COLUMN, and lays the panels out in two above 157 of them —
which is two of the 78 a table wants before it wraps, plus the gutter. What the
second column buys is height, and with two tables it buys a great deal more of it
than it did with seven. `QB_DASH_WIDE` moves the threshold; below it nothing
about the layout changes.

Textual requests mouse tracking from the terminal, and tmux forwards events to
a pane that asks for them — so this needs no tmux configuration beyond the
`mouse on` that makes borders draggable. Hold Shift to reach tmux's own mouse
behaviour (selecting text) instead of the app's.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import time

from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.containers import Vertical
from textual.coordinate import Coordinate
from textual.events import Click, Resize
from textual.screen import ModalScreen
from textual.widgets import DataTable, Footer, Input, OptionList, Static

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import qbdata as qd                                             # noqa: E402


def sibling(name: str) -> str:
    """`name` on PATH, or beside this file.

    `qb-start.sibling`'s resolution, for its reason: a home-manager install has
    both, a checkout has only the second, and a partial install has only the
    first. The dashboard has always called `qb-seat-click` by its bare name and
    got away with it because it is launched from a shell that has the harness on
    PATH — but the ⚒ now runs the one tool whose absence must not be mistaken for
    a machine that has not opted in, and `which` returning None is a different
    answer from `spawn.json` being absent.
    """
    from shutil import which
    return which(name) or os.path.join(os.path.dirname(os.path.abspath(__file__)), name)


def env_columns(name: str, fallback: int) -> int:
    """A column count from the environment, or the fallback — never an exception.

    The one knob here is a layout threshold, and a dashboard that refused to
    start over `QB_DASH_WIDE=wide` would be trading the panel somebody is trying
    to read for a typo in a tuning variable. A value that is not a positive
    number of columns is not honoured and is not fatal; it is simply not there.
    """
    raw = (os.environ.get(name) or "").strip()
    if not raw.isdigit() or int(raw) < 1:
        return fallback
    return int(raw)


def spawn_answer(name: str, done: "subprocess.CompletedProcess") -> str:
    """The detail line after `qb-start` has answered, in `qb-start`'s own words.

    Module level, and not a method, because it is the one part of the ⚒ that is
    pure: a completed process in, the sentence a human reads out. That is what
    makes "a refusal is reported rather than swallowed" testable without a board,
    a tmux server or a policy file.

    The LAST two lines of stderr, because that is where `qb-start` puts its
    verdict: the gates it ran print theirs first — `qb-claim` naming the holder,
    `qb-admit` listing the slots — and then it says `refused: …` and its detail.
    Taking the last two keeps the verdict and its remedy and drops the noise
    above them, and the whole of it is still on the pane the click came from.
    """
    try:
        answer = json.loads(done.stdout or "")
    except ValueError:
        answer = {}
    if done.returncode == 0 and answer.get("started"):
        session = str(answer.get("session") or "")
        return (f"started '{name}' — session {session[:8]} · Ctrl-b n to watch it · "
                f"`qb-end {session}` to stop it")
    lines = [ln.strip() for ln in (done.stderr or "").splitlines() if ln.strip()]
    return "⚒ " + (qd.clip(" — ".join(lines[-2:]), 240)
                   or f"qb-start exited {done.returncode} and said nothing")


def holders(held: dict[str, dict]) -> dict[str, str]:
    """{'owner/repo#n' → who holds it}, which is all of a claim this dashboard shows."""
    return {n: (c.get("holder") or "?") for n, c in held.items()}


class Confirm(ModalScreen[bool]):
    """Yes/no before something expensive and outward-facing.

    A panel review costs real money, comments on a public PR and pushes a fix
    commit, so a stray click on a 78-column pane should not be able to start
    one. It shows the exact command, because the answer to "what will this do"
    should not require trusting a sentence about it. QB_DASH_CONFIRM=0 turns it
    off for anyone who wants the single click and means it.
    """

    BINDINGS = [("escape", "no", "cancel"), ("n", "no", "cancel"),
                ("y", "yes", "run"), ("enter", "yes", "run")]

    CSS = """
    Confirm { align: center middle; }
    #box { width: 90%; max-width: 70; height: auto; padding: 1 2;
           background: $panel; border: thick $accent; }
    #cmd { color: $text-muted; padding: 1 0; }
    """

    def __init__(self, prompt: str, command: str, cwd: str) -> None:
        super().__init__()
        self.prompt, self.command, self.cwd = prompt, command, cwd

    def compose(self) -> ComposeResult:
        with Vertical(id="box"):
            yield Static(Text(self.prompt, style="bold"))
            yield Static(Text(f"$ {self.command}\n  in {self.cwd}", style="dim"), id="cmd")
            yield Static(Text("enter/y — run     esc/n — cancel", style="bold $accent"))

    def action_yes(self) -> None:
        self.dismiss(True)

    def action_no(self) -> None:
        self.dismiss(False)


class DialEdit(ModalScreen[dict | None]):
    """Set or clear one dial, from the pane — the write half of #477.

    The dashboard could always READ what was in force; turning one was a browser
    action, because `POST /dials` takes `app.auth.delegated` (a person, or an
    agent one has delegated to — #591) and this program
    authenticates with the machine bearer token every agent on the box holds.
    What changed is the credential, not the gate: :class:`qbdata.HumanClient`
    presents a person's own key to the agent host, so the person at this keyboard
    writes as themselves and the board records `human/<user>` as it always has.

    **What that costs is written down rather than implied** — prisonblues/quarterback#479
    is the record. The key is readable by everything running as this user, so
    "the dash can set a dial" and "anything on this box can set a dial" are one
    fact, and the second is the one to design against.

    ## The vocabulary is on screen, because it has to be somewhere (#539)

    The first cut of this modal had four empty boxes and one placeholder each, and
    the value placeholder read `P3, 2, true, null` — four value kinds in one line,
    because it had to cover all 29 dials at once and therefore could not answer the
    only question a person actually has, which is what THIS one takes. Nothing said
    which dials exist, what this one is set to now, or which way it may move. A
    misspelt name saved clean: the board stores `dial` as opaque text on purpose,
    so the refusal arrives from a round three hours later, on the old value.

    Everything needed was two directories away the whole time. `harness_rules`
    owns the dial table, `qbdata.dial_vocabulary` reads it at call time rather than
    copying it, and this screen renders what it gets:

      * **dial** — the dotted path, with the names filtered under it as you type
        (`↓` to walk them, enter or a click to take one). **Scrolling them says what
        each one does**: the line under the value box describes the name under the
        CURSOR, not the name in the box, because reading down 29 dotted paths is the
        moment "which of these did I mean" is being asked and answering it after the
        choice is made answers it too late. Fixed when editing a row that exists; a
        dial is identified by its name, so letting this be edited would silently
        create a second dial rather than change the one on screen.
      * **value** — JSON where it parses, the string it looks like otherwise
        (`qbdata.parse_dial_value`, and `dials.html` does the same). Once a name is
        chosen the list retires and the line under this box grows the rest of the
        answer: what the dial accepts, what it defaults to, what is in force and at
        which scope. The two states are the two questions, and they do not both fit
        a 78x24 pane. `ctrl+s` refuses a value the harness would not apply — in the
        box, naming the field, instead of storing it and finding out later.
      * **reason** — required, by the board and here. A dial whose argument was
        never written down is one nobody can decide to remove.
      * **for** — `30m`, `4h`, `7d`, or empty for a dial with no end. Empty is a
        real answer and not a missing one, and it is parsed here now rather than
        after the modal closes, so a mistyped duration costs a keystroke instead
        of the other three fields.

    **A bad VALUE is a refusal; an unknown NAME is a warning and then a write.**
    The table is the harness beside THIS dashboard and the two are installed
    separately, so a hard refusal would make a box one release behind a box that
    cannot set a dial the rest of the fleet already applies — `tempo` (#474) is the
    standing case, drawn by both dashboards and absent from `BOARD_DIALS`. A value
    for a name this box DOES know gets no such benefit of the doubt: the kind came
    from the same table as the name, so there is no version of the harness in which
    `max_rounds: "2"` is a value somebody applies.

    **A box that cannot find the table still writes.** `dial_vocabulary` answers
    `{}` on a host with no `harness/loops` beside the dashboard, and then this is
    the form it always was: free text, no picker, no refusal, the board taking
    both. Refusing to open would make the dashboard less useful than it was, and
    an empty vocabulary is "cannot tell", never "nothing is settable" — so it is
    said on the line under the value rather than shown as a dial list of length 0.
    """

    BINDINGS = [("escape", "cancel", "cancel"), ("ctrl+s", "save", "save"),
                ("ctrl+x", "clear", "clear"), ("down", "to_names", "names")]

    #: A 78x24 pane is the size this has to fit, and the picker cost it four rows
    #: it did not have. What paid for them: the per-field margins (the spec line
    #: reads as the value box's caption without one), the scope, which moved into
    #: the title where it is always visible rather than last where it was first to
    #: be clipped, and the refusal line, which takes no room until there is one.
    #: The list and the spec line then take turns — four rows of names while a name
    #: is being chosen, three of description once one has been — so the tall state
    #: is the only one either of them is in.
    #: `overflow-y` is the backstop for a pane shorter still — a modal that clips
    #: silently loses whichever control is last, and here that was the scope.
    CSS = """
    DialEdit { align: center middle; }
    #box { width: 90%; max-width: 76; height: auto; max-height: 100%;
           overflow-y: auto; padding: 1 2;
           background: $panel; border: thick $accent; }
    #hint { color: $text-muted; }
    /* ALIGNED WITH THE TEXT IN THE BOXES, not with the box edge. An `Input` draws
       a border and pads inside it, so its text starts three columns in; a bare
       `Static` starts at the panel's own padding, and the description sat three
       columns to the left of every field it describes. The title and the key line
       keep the edge — they frame the form rather than belonging to a field. */
    #spec { color: $text-muted; padding-left: 3; }
    #err { color: $error; padding-left: 3; }
    /* Four NAMES and no more: the names are an aid to the field above them, not
       the form. `border: none` because the default one costs two rows of the four
       — a frame around a list that sits directly under the box it belongs to, paid
       for in half the names it can show. */
    #names { height: auto; max-height: 4; border: none; padding-left: 3; }
    """

    def __init__(self, row: dict | None = None, repo: str | None = None,
                 scope_label: str = "", vocabulary: dict | None = None,
                 in_force: dict | None = None, now: str | None = None,
                 trouble: str = "") -> None:
        super().__init__()
        self.row = row or {}
        self.repo = repo
        self.scope_label = scope_label
        #: `{}` is "this box cannot read the harness's table", not "no dial is
        #: settable" — see the class docstring.
        self.vocabulary = vocabulary or {}
        #: WHY it is empty, in the caller's words (`qbdata.dial_trouble`). Handed
        #: in beside the vocabulary rather than fetched here, so the sentence and
        #: the table it explains come from one read: a screen that asked the loader
        #: a second question could answer with a state the first one never saw.
        self.trouble = trouble
        #: `GET /dials`' own answer, so the spec line can say what is in force
        #: beside what the built-in default is. Moving a floor without being told
        #: it was already moved is how one gets nudged twice.
        self.in_force = in_force or {}
        #: The BOARD's clock, for the expiry. A box an hour slow otherwise writes
        #: "in one hour" as a time already past.
        self.now = now
        #: The names currently offered, in the order they are drawn — what an
        #: option index means. Set before the list can be clicked, because an
        #: empty one is the honest state of a modal that has not mounted yet.
        self.matches: list[str] = []
        #: The unknown name this screen has already objected to once. See
        #: `action_save`: an unrecognised dial is warned about and then allowed,
        #: because the table it is being judged against is THIS BOX'S harness.
        self._insisted = ""
        #: The name under the cursor in the list, which is NOT the name in the box.
        #: Scrolling the picker describes what it lands on — a person reading down a
        #: list of 29 dotted paths is asking which one they want, and answering that
        #: only after the choice is made is answering it too late.
        self._preview = ""

    def compose(self) -> ComposeResult:
        existing = bool(self.row.get("dial"))
        with Vertical(id="box"):
            # WHICH LAYER this will be written to, on the title line and not at the
            # bottom. `fleet` and `this repo` are different settings with the same
            # name and it is the one mistake a person cannot see afterwards, so it
            # is the line that must never be the one a short pane clips.
            title = Text("set a dial" if not existing else
                         f"dial · {self.row.get('dial')}", style="bold")
            title.append(f"  ·  {self.scope_label or 'fleet (every repo)'}",
                         style="bold yellow")
            yield Static(title)
            if not existing:
                yield Input(placeholder="review_panel.fix_severity_floor", id="f_dial")
                # Populated in `on_mount` rather than here: the whole list is the
                # right first answer to "which dials are there", and it is the
                # same call every keystroke makes afterwards.
                yield OptionList(id="names")
            yield Input(value=self._value_text(), placeholder=self._value_hint(),
                        id="f_value")
            yield Static(self._spec(self._dial_name()), id="spec")
            yield Input(placeholder="why is this value in force?", id="f_reason")
            yield Input(placeholder="30m · 4h · 7d — empty for no end", id="f_expiry")
            # Not drawn at all until something is actually wrong: a refusal line
            # that is always there is one a person stops reading, and an empty one
            # would spend a row of a modal that has none to spare.
            err = Static("", id="err")
            err.display = False
            yield err
            # The keys, and only the ones that do something here. `ctrl+x` clears a
            # dial that is ON the board, so on a new one it is a key that can only
            # bell — and `↓` is where the list of names went when the line under the
            # value box started describing them instead of counting them.
            keys = ("ctrl+s save · ctrl+x clear this dial · esc cancel" if existing
                    else "↓ names · ctrl+s save · esc cancel")
            yield Static(Text(keys, style="bold $accent"), id="hint")

    # -- what the chosen dial is, and what it takes ----------------------------

    def _dial_name(self) -> str:
        """The dial this modal is about — the fixed one, or whatever is typed."""
        return (self.row.get("dial") or self._field("f_dial")).strip()

    def _spec_of(self, dial: str) -> dict:
        return self.vocabulary.get(dial) or {}

    #: The value box's placeholder before any dial is named — four spellings from
    #: four different dials, which is what a form can say when it does not know
    #: which one it is on. Everywhere else it is replaced by that dial's own.
    GENERIC_HINT = "P3, 2, true, null"

    def _value_hint(self) -> str:
        """The value box's placeholder, for THIS dial where one is known.

        The old placeholder listed four spellings from four different dials, which
        is what a form says when it cannot tell which dial it is on. With a name
        already chosen it can, and the generic line is kept only for the case
        where it is still true — a new dial with nothing typed yet.
        """
        spec = self._spec_of(self._dial_name())
        return spec.get("hint") or self.GENERIC_HINT

    def _browsing(self) -> bool:
        """Is the list up? Then the line below it is describing a row, not a choice."""
        names = self._names()
        return names is not None and names.display

    def _spec(self, dial: str, brief: bool = False) -> Text:
        """The line under the value box: what this dial takes, and where it stands.

        Four facts answering four different questions. WHAT IT DECIDES answers "is
        this the one I meant". The kind answers "what do I type". The DEFAULT
        answers "what happens if I clear it", which is the other half of every
        decision to set one. And what is IN FORCE answers "am I the second person to
        move this today" — the board returns the row it replaced for the same
        reason, and a person who reads it here does not have to undo anything to
        find out.

        `brief` is the first of those on its own, and it is what the list is drawn
        with: scrolling 29 names is the moment the first question is being asked and
        none of the other three are. It is also what makes room — the names and the
        full block cannot both be on a 78x24 pane, and the choice between them is
        settled by which question the person is currently asking.
        """
        if not self.vocabulary:
            # Said once, plainly, and NOT as an empty dial list: a form that drew
            # "0 dials settable" would state as fact the one thing it failed to
            # find out. The write still goes through; the board is the judge.
            #
            # AND IT SAYS WHICH FAILURE. An absent harness, one that will not
            # import and one older than the dial table all end here, and telling
            # all three as the first sends somebody to look for a directory that is
            # sitting right there. `qbdata.dial_trouble` carries the distinction.
            return Text(f"{self.trouble or 'the dial table cannot be read here'}, "
                        f"so the names and values are not checked here",
                        style="italic")
        spec = self._spec_of(dial)
        if not spec:
            # A name that is not in the table is a person who has probably mistyped
            # it, and it has to LOOK different before ctrl+s says so — yellow rather
            # than red, because `action_save` warns about this and then writes it:
            # the table is this box's harness, and being ahead of it is not an error.
            #
            # The empty case is the FIRST PAINT and only that: `compose` draws this
            # line before `on_mount` has filled the list, so for one frame there is
            # no name anywhere to describe. It says what the list under the box is
            # rather than nothing, because a person whose eye lands there first
            # should not have to infer it.
            if not dial:
                return Text(f"{len(self.vocabulary)} dials are settable — "
                            f"type to filter, ↓ to pick one", style="italic")
            return Text(f"nothing this box knows applies this"
                        f"{self._did_you_mean(dial)}", style="bold yellow")
        # WHAT IT DECIDES, first. The kind, the default and the layer below it are
        # all answers to "how do I set this one"; this is the answer to "is this the
        # one I meant", which is the question actually being asked at the moment a
        # name is picked out of a list of 29.
        out = Text(spec["what"] or dial, style="bold")
        if brief:
            return out
        # The HINT and not the kind beside it: `number · a number` is the kind said
        # twice, and the hint is the half written in the words that go in the box.
        out.append(f"\n{spec['hint']}", style="none")
        # `default_known` and not a bare `default`: a dial absent from DEFAULTS is a
        # bug in the harness's table, and drawing its `null` as the shipped answer
        # would hide that behind a plausible value.
        out.append(f"\ndefault {json.dumps(spec['default'])}"
                   if spec.get("default_known") else "\nno built-in default")
        row = qd.dial_of(self.in_force, dial, self.repo)
        out.append(f" · in force {qd.dial_value(row, 24)} "
                   f"({'this repo' if row.get('repo') else 'fleet'})" if row
                   else " · no board dial — this repo's own value stands")
        if spec.get("note"):
            out.append("\n" + spec["note"], style="yellow")
        return out

    def _did_you_mean(self, dial: str) -> str:
        """` — ↓ takes review_panel.max_rounds`, where that is unambiguous.

        The commonest way to arrive at an unknown name is to type the half of it a
        person actually remembers: `max_rounds` for `review_panel.max_rounds`,
        `pi.enabled` for `reviewers.pi.enabled`. A refusal that only said "not a
        dial" would be technically right and leave the answer sitting one row
        below, unmentioned — so where the filter has narrowed to exactly one, the
        refusal names it. Several matches name none: picking the first would be
        this screen guessing which dial somebody meant to move.
        """
        hit = qd.dial_matches(self.vocabulary, dial, limit=2)
        return f" — ↓ takes {hit[0]}" if len(hit) == 1 else ""

    def _value_text(self) -> str:
        """The current value, spelled the way this box would accept it back."""
        if "value" not in self.row:
            return ""
        value = self.row.get("value")
        return value if isinstance(value, str) else json.dumps(value)

    # -- the picker ------------------------------------------------------------

    def _names(self) -> OptionList | None:
        found = self.query("#names")
        return found.first(OptionList) if found else None

    def _refill(self, typed: str) -> None:
        """The names worth offering for what has been typed, redrawn."""
        names = self._names()
        if names is None:
            return
        self.matches = qd.dial_matches(self.vocabulary, typed)
        names.clear_options()
        names.add_options(self.matches)
        # HIDDEN ONCE THE NAME IS ONE OF THEM, and not only to buy back the rows the
        # spec line then spends. A list of names is help with choosing, and it has
        # stopped helping the moment a choice is made — leaving it up would keep four
        # rows of alternatives under a field that is already answered, on a form
        # whose next question is one line further down. Typing again brings it back.
        names.display = bool(self.matches) and not self._spec_of(typed.strip())
        # Highlight the first one, because `clear_options` leaves nothing
        # highlighted and an OptionList with no highlight answers `enter` by doing
        # nothing at all — which reads as a picker that does not work.
        names.highlighted = 0 if self.matches else None
        # And the line below describes it straight away. Waiting for a key would
        # leave the first row — the one a person's eye is already on — as the only
        # one in the list with nothing said about it.
        self._preview = self.matches[0] if self.matches and names.display else ""

    def on_mount(self) -> None:
        # The field a person came here to change. Editing an existing dial that is
        # its value; creating one, it is the name.
        self._refill("")
        self.query_one("#f_dial" if not self.row.get("dial") else "#f_value",
                       Input).focus()

    def on_input_changed(self, event: Input.Changed) -> None:
        """Filter as the name is typed, and re-state what the named dial takes."""
        if event.input.id in ("f_dial", "f_value") and not self._armed():
            # The two fields a refusal is ever about. Editing either is a person
            # acting on it, and a refusal left standing beside the field it has
            # stopped describing is read as a second, still-live objection.
            #
            # UNLESS THE UNKNOWN-NAME WARNING IS ARMED. That one is not a complaint
            # about the value — it says the NAME is one nothing here applies, and it
            # stays true while the name does. Clearing it on a value edit hid the
            # sentence while `_insisted` went on holding the next ctrl+s open, which
            # is a confirmation nobody can see they have given.
            self.query_one("#err", Static).display = False
        if event.input.id != "f_dial":
            return
        self._refill(event.value)
        self._redraw_spec()

    def _redraw_spec(self) -> None:
        """The line under the value box, for whatever is being looked at right now.

        The PREVIEW wins over the typed name while the list is up, and that is the
        whole of this feature: the name in the box is what will be written, and the
        name under the cursor is what is being read about. The value's placeholder
        follows the box rather than the cursor — it belongs to the field it sits in,
        and flickering it through 29 dials as somebody scrolls would be describing
        one thing in the widget for another.
        """
        browsing = self._browsing()
        dial = (self._preview if browsing and self._preview else self._dial_name())
        self.query_one("#spec", Static).update(self._spec(dial, brief=browsing))
        # AND THE VALUE BOX FOLLOWS IT TOO. An earlier cut kept the placeholder on
        # the typed name, on the reasoning that a widget should describe its own
        # field — which is the wrong way round here, because the placeholder IS the
        # guide to what may be typed, and the dial being read about is the one the
        # question is about. Scrolling the list now says what each dial decides AND
        # what it will take, which is the pair a person needs before choosing.
        spec = self._spec_of(dial)
        self.query_one("#f_value", Input).placeholder = (
            spec["hint"] if spec else self.GENERIC_HINT)

    def on_option_list_option_highlighted(
            self, event: OptionList.OptionHighlighted) -> None:
        """Scrolling the list describes what it lands on."""
        if 0 <= event.option_index < len(self.matches):
            self._preview = self.matches[event.option_index]
            self._redraw_spec()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        """A name taken from the list: fill the box and move on to the value.

        Indexed into the list this screen filtered rather than read off the
        widget's own option, so what is written into the box is the string that
        went in — a prompt is a renderable, and rendering one back to text is a
        round trip through Rich that a dotted path does not need to take.
        """
        if not (0 <= event.option_index < len(self.matches)):
            return
        field = self.query_one("#f_dial", Input)
        field.value = self.matches[event.option_index]
        # `on_input_changed` refills and redraws off the back of that assignment;
        # the focus move is what is left, and it goes to the box a person picking
        # a name is on their way to.
        self.query_one("#f_value", Input).focus()

    def action_to_names(self) -> None:
        """`↓` from the NAME box walks into the list under it.

        `Input` binds neither arrow, so this key would otherwise do nothing at all
        in the one field it is the obvious gesture for. From any other field it
        still does nothing, deliberately: a `↓` typed in the reason box is somebody
        reaching for the next line, and throwing the focus three fields backwards
        is a worse answer than ignoring it.
        """
        names = self._names()
        if names is not None and names.display and self.focused is self.query_one(
                "#f_dial", Input):
            names.focus()

    # -- saving ----------------------------------------------------------------

    def _armed(self) -> bool:
        """Is the unknown-name warning outstanding for the name in the box?"""
        return bool(self._insisted) and self._insisted == self._dial_name()

    def _refuse(self, message: str) -> None:
        """Say why, and stay open.

        The alternative is what this modal did before #539: dismiss, and let the app
        say it afterwards. That spends the other three fields to report a mistake in
        one of them, and the person retypes a reason they already wrote.
        """
        err = self.query_one("#err", Static)
        err.update(Text(message, style="bold red"))
        err.display = True
        self.app.bell()

    def _field(self, name: str) -> str:
        found = self.query(f"#{name}")
        return found.first(Input).value if found else ""

    def action_save(self) -> None:
        """Everything the board would refuse, and everything the harness would
        ignore, judged here — then dismissed with what the caller asked for.

        The two are not the same list and both matter. A blank reason is refused by
        `POST /dials` and a 422 would say so; a dial name the harness does not know
        is ACCEPTED by the board, stored, and reported as in force for ever while
        nothing applies it. Only this side can catch the second, because only this
        side has the table.

        The raw strings go back to `Dash.dial_written` unparsed, so the app's own
        checks still run on the path where this screen had no vocabulary to check
        against. Parsing twice is cheaper than one of the two forgetting.
        """
        dial = self._dial_name()
        if not dial:
            self._refuse("which dial? Type a name, or press ↓ to pick one")
            return
        reason = self._field("f_reason")
        if not reason.strip():
            self._refuse("a dial needs a reason — why is this value in force? "
                         "The board refuses one without, and so does this")
            return
        try:
            value = qd.parse_dial_value(self._field("f_value"))
        except Exception as exc:                  # noqa: BLE001 — show it, don't die
            self._refuse(str(exc))
            return
        # GATED ON THE TABLE THIS SCREEN WAS GIVEN, not on whether `qbdata` can
        # find one of its own. They are the same answer in the app — the modal is
        # handed `dial_vocabulary()` — and keeping the judgement on the screen's
        # own copy is what stops a form that says "not checked here" from refusing
        # a write anyway, which is the one behaviour a person cannot argue with.
        if self.vocabulary and not self._spec_of(dial):
            # **AN UNKNOWN NAME IS A WARNING AND THEN A WRITE**, and the asymmetry
            # with the value check below is the whole argument.
            #
            # The table this is judged against is the harness beside THIS DASHBOARD,
            # and the two are installed separately: a box a release behind would
            # otherwise be a box that cannot set a dial the rest of the fleet
            # already applies. `tempo` (#474) is the standing case — both dashboards
            # draw it, `BOARD_DIALS` does not hold it, and a hard refusal here would
            # take a dial the fleet uses away from the one screen that sets it.
            #
            # A value for a name this box DOES know gets no such benefit of the
            # doubt: the kind came from the same table as the name, so there is no
            # version story in which `max_rounds: "2"` is a value somebody's harness
            # applies. That one stays a refusal.
            if self._insisted != dial:
                self._insisted = dial
                self._refuse(f"nothing this box knows applies `{dial}`"
                             f"{self._did_you_mean(dial)} — ctrl+s again to set it "
                             f"anyway")
                return
        elif self.vocabulary:
            problem = qd.dial_refusal(dial, value)
            if problem:
                self._refuse(problem)
                return
            # And WHERE the row goes, which is a second question about a different
            # field and gets its own answer (#563). The board takes either scope for
            # any dial — `dial` is opaque text there and `repo` is just a column — so
            # a fleet dial written for one repo is accepted, stored, reported as in
            # force, and read by nothing. That is a misspelt name's failure arriving
            # through the scope line, and it is caught in the same place for the same
            # reason: this side is the only one that knows what a dial IS.
            problem = qd.dial_scope_refusal(dial, self.repo)
            if problem:
                self._refuse(problem)
                return
        try:
            # Parsed for its refusal only — the app parses it again against the
            # same board clock when it writes.
            qd.parse_dial_expiry(self._field("f_expiry"), self.now)
        except (ValueError, OverflowError) as exc:
            self._refuse(str(exc))
            return
        self.dismiss({
            "dial": dial,
            "value": self._field("f_value"),
            "reason": reason,
            "expiry": self._field("f_expiry"),
            "repo": self.repo,
        })

    def action_clear(self) -> None:
        """Take it off the board. Only for a dial that IS on the board — clearing
        one that was never set is a no-op the board accepts, and offering it while
        creating one would be a button that cannot mean anything."""
        if not self.row.get("dial"):
            self.app.bell()
            return
        self.dismiss({"dial": self.row["dial"], "repo": self.repo, "clear": True})

    def action_cancel(self) -> None:
        self.dismiss(None)


class ClickTable(DataTable):
    """A DataTable where a single click acts on the row under the pointer.

    Two things make this necessary. DataTable treats a click on any row but the
    cursor's as "move the cursor" and selects nothing, so a first click on a row
    does nothing visible. And it consumes the Click rather than letting it bubble,
    so a handler on the App never runs — this has to be on the widget itself.
    """

    def add_row(self, *cells, key: str | None = None, **kwargs):
        """`DataTable.add_row`, except a key this table already holds is
        suffixed rather than raised on.

        DataTable answers a repeated key with DuplicateKey, which does not
        degrade the row — it takes the whole dashboard down, and this is the
        component a human looks at when something is ALREADY wrong. So it is
        the one that must survive unexpected input rather than replace six
        panels with a traceback (#209).

        Every panel below computes a key it believes is unique, and after #208
        and #209 those keys are right. This is the backstop for the next panel,
        whose duplicates nobody has thought of yet: two rows that collide are
        kept as two rows, the second under a `~2` key.

        **Degrading is not the same as reporting, and this had to be said out
        loud.** A row key is never rendered, so the `~2` is invisible; and in
        the case the backstop was written for — two plan items arriving with no
        `item_id` — two rows is also exactly what CORRECT data looks like. Left
        at that, a keying bug this once crashed loudly would now produce nothing
        at all. So the collision is written to the app log, which is the only
        place it can be reported from.

        Returns the key actually used. Callers must file their record under
        THAT — `dispatch_row` looks a row up by key, so a suffixed row would
        otherwise display fine and do nothing when clicked.
        """
        if key is not None:
            # Compared on the string rather than by constructing a RowKey: the
            # key type is Textual's private business and this survives it
            # changing. The tables here are tens of rows, not thousands.
            taken = {rk.value for rk in self.rows}
            if key in taken:
                n = 2
                while f"{key}~{n}" in taken:
                    n += 1
                asked, key = key, f"{key}~{n}"
                try:
                    self.log.warning(
                        f"{self.id or type(self).__name__}: duplicate row key "
                        f"{asked!r} — kept as {key!r}. The panel's key is not "
                        f"unique across every repo and screen it can show.")
                except Exception:                   # noqa: BLE001
                    # The backstop exists so this table cannot take the
                    # dashboard down. A logger that is not there (no running
                    # app) must not be the thing that finally does.
                    pass
        return super().add_row(*cells, key=key, **kwargs)

    def on_click(self, event: Click) -> None:
        if not self.row_count:
            return
        # The cell comes off the CLICK, not off hover_coordinate. Hover is set by
        # a separate mouse-move message, so reading it here is a race: a click
        # arriving without a preceding move — the first click into a pane, or any
        # click a test synthesises — reads whatever the last move left behind and
        # acts on the wrong cell. This is the same source DataTable itself uses.
        meta = event.style.meta
        if "row" not in meta or "column" not in meta:
            return
        row, column = meta["row"], meta["column"]
        if not 0 <= row < self.row_count:
            return                                  # the header, or past the last row
        key = self.coordinate_to_cell_key(Coordinate(row, 0)).row_key
        # The COLUMN goes with it: an action icon in its own column is how one
        # row offers more than one verb — click the ⚖ to review, the rest of the
        # row to open.
        self.app.dispatch_row(str(key.value), column)


class Dash(App):
    """Two tables, the dials above them, and a detail line."""

    CSS = """
    Screen { background: $surface; }
    /* Hidden until the first fetch says there is something to show — and since
       #426 "something" is the caps OR the tallies that ride beside them, so an
       install with no subscription token still gets the queue depth rather than
       losing the row it sits on. See render_limits. */
    #limits { height: 1; padding: 0 1; background: $panel; color: $text;
              display: none; }
    #head { height: 1; padding: 0 1; background: $panel; color: $text; }
    #detail { height: auto; min-height: 1; padding: 0 1; background: $panel;
              color: $text-muted; }
    .title { height: 1; padding: 0 1; background: $boost; color: $accent; }

    /* THE SHARE IS ON THE PANEL, NOT ON THE TABLE. Each title and its table are
       one `.panel` so that the wide layout has something to place: a grid puts
       CELLS in columns, and a title in one column with its table in the other is
       what happens if the pairs are left loose in the container. The table then
       takes `1fr` of its own panel — everything the title left — so the shares
       below still read as shares of the pane. */
    #body { layout: vertical; }
    .panel { layout: vertical; }
    .panel > DataTable { height: 1fr; }

    /* A share of the pane each, and each scrolls inside its share. With
       `height: auto` the tables simply stack past the bottom of a 42-row pane:
       the rows at the end then cannot be clicked, because they are not on
       screen — which is how the click test caught it.

       TWO TABLES WHERE THERE WERE SEVEN (#589). SEATS, FLEET and CLAIMED were
       three views of one subject and are now AGENTS; PLANS, OPEN PRs, REVIEW
       QUEUE and ISSUES were four answers to one question and are now WORK. The
       shares are the old ones added up: AGENTS carries what SEATS, FLEET (2fr)
       and CLAIMED (1fr) carried, and WORK what the other four did.

       AGENTS IS NOT CONTENT-SIZED, and SEATS' exemption does not survive into
       it. SEATS could be `auto` because it was bounded — MAX_SEATS panes plus
       the ＋ — and this table also holds the fleet and every claim nobody
       answers for, which is as long as the board is. That is the exact
       unboundedness that put a table off the bottom of the pane the last time
       something here was sized to its content. */
    /* Sized to its CONTENT, which SEATS was and for SEATS' reason: a fleet with
       nothing set is two rows, and an fr share would spend the rest on blank
       space that comes straight off WORK. The cap is where it stops growing and
       starts scrolling, which is the right way round here: the printed renderer
       has to stop listing and count the rest (DIAL_ROWS), and this one does not,
       so nothing is hidden by it. 7 is four dials and the row that says where to
       turn one; a fleet with more than that in force has a configuration
       question rather than a layout one. */
    #p_dials  { height: auto; }
    #dials    { height: auto; max-height: 7; }
    #p_agents { height: 2fr; }
    /* The longer of the two and by some way: the plan is every repo's list, and
       with the backlog on it carries every open issue as well. */
    #p_work   { height: 3fr; }

    /* ---- and the same panels in two columns, when there are columns to spare.
       Textual has no media query, so the class is set from `on_resize` and the
       whole of the wide layout is this block. `layout` is a property like any
       other, which is why the narrow layout above says `vertical` explicitly
       rather than leaning on the default: the two rules have to be able to
       disagree.

       WHAT TWO COLUMNS BUY IS HEIGHT, not width — and with two tables instead of
       seven it buys a great deal more of it. Seven panels dividing one column's
       rows is why CLAIMED and REVIEW QUEUE were two rows tall on a pane nobody
       would call short; AGENTS and WORK side by side each get the pane's whole
       height, which is three to five times what any of their parts had.

       DIALS SPANS BOTH and keeps its place at the top, for the reason it was put
       there: it is the configuration every row below it is running under. It is
       also the only `auto` row, so the two tables divide everything left.

       ONE GRID ROW OF TABLES, not three. The narrow weights were paired to stop a
       short panel riding with another short one and leaving a row of dead space;
       with two panels there is no pairing left to do, and both of them are long. */
    #body.-wide { layout: grid; grid-size: 2; grid-rows: auto 1fr;
                  grid-gutter: 0 1; }
    #body.-wide .panel { height: 100%; }
    #body.-wide #p_dials { column-span: 2; height: auto; }
    """

    BINDINGS = [
        ("q", "quit", "quit"),
        ("r", "refresh_now", "refresh"),
        ("o", "open_pr", "open"),
        ("p", "panel_pr", "panel"),
        ("f", "fix_issue", "fix"),
        ("s", "toggle_scope", "scope"),
        # The board's dials PAGE, which is still worth a key now that the panel
        # can write: the page shows every repo's dials at once and this panel
        # shows the screen's own, and a person who wants to compare two projects
        # wants the page. The ✎ on a row is the control; this is the map.
        ("d", "open_dials", "dials"),
        # The two lists nothing is waiting on. Off by default because they are a
        # catalogue rather than state — twelve rows of issue list was the single
        # biggest consumer of the old frame — and one key rather than two because
        # they are the same kind of thing: work nobody has started and work nobody
        # is waiting on. Their counts stay on the header line either way.
        ("b", "toggle_backlog", "backlog"),
        # The one door, in the terminal. Not folded into `b`: the backlog is work
        # nobody has started and this is work nobody can start, and a reader
        # looking for one is not looking for the other.
        ("w", "toggle_waiting", "waiting"),
        ("z", "expand", "expand"),
        ("question_mark", "help", "keys"),
    ]

    # WHEN THE PANELS GO TWO ACROSS, in columns of the pane. Not a taste: 78 is
    # what one of these tables wants before it wraps — it is `QB_SEATS_DASH_SIZE`'s
    # default, and quoted from there — so two of them side by side plus the gutter
    # between is the narrowest screen on which the second column is not paid for
    # out of the first. `QB_DASH_WIDE` moves it, which is how a terminal whose
    # font makes 157 columns comfortable can have the wide layout sooner.
    WIDE_COLUMNS = 157

    # WHERE THE VERB LIVES. One constant where there were four — PANEL_COLUMN,
    # KILL_COLUMN, FIX_COLUMN and EDIT_COLUMN, every one of them 1 and every one
    # of them documented as "the same column everywhere on purpose". With two
    # tables instead of seven that is no longer a convention four places have to
    # keep: it is one column per table, carrying whatever verb the row under it
    # wants, and "click the icon, not the row" is one habit rather than five.
    VERB_COLUMN = 1

    def __init__(self, interval: float = 4.0, gh_interval: float = 90.0,
                 plan_interval: float = 15.0, scope: "qd.Scope | None" = None,
                 repo: str | None = None) -> None:
        super().__init__()
        # Which project's rows this is about, and whether the repo column is worth
        # its eleven columns — one object, because the two answers have to agree
        # (qbdata.Scope). `s` swaps it for its opposite; everything below asks it
        # rather than deciding for itself.
        self.scope = scope if scope is not None else qd.resolve_scope()
        self.interval = interval
        self.gh_interval = gh_interval
        # Its own clock, between the two: the plan is a call to the board rather
        # than to GitHub, so it is cheap, but it changes when a human reorders it
        # or an agent claims an item — neither of which happens every four
        # seconds.
        self.plan_interval = plan_interval
        # Slower again: a five-hour window does not move in four seconds, and
        # this one is a call to Anthropic rather than to the board.
        self.limits_interval = qd.LIMITS_EVERY
        self.limits: list[dict] = []
        self.limits_err: str | None = None
        self.client = None
        self.cfg = None
        self.rows: dict[str, dict] = {}       # row key → the record behind it
        # The last board answer, kept so `s` can redraw from it. A toggle that had
        # to wait for the next poll would look like a key that did nothing for four
        # seconds, and one that re-fetched would spend a request on a decision the
        # client had already made.
        self.board: dict = {}
        self.seats: list[dict] = []           # the seat PANES, off tmux
        # session id -> the board's live agent, which is the id the pane carries
        # as `@qb_session`. One key and no narrowing: it was (machine, scope, seat
        # number) while a pane could only be identified through the agent's name,
        # and all three were needed because none of them was unique on its own
        # (#540).
        self.seat_states: dict[str, dict] = {}
        self.prs: list[dict] = []
        # The derived review queue, kept for the same reason as `board`: `s`
        # redraws from it, and it rides the gh clock so a re-fetch on a toggle
        # would cost a `gh pr list` for a decision already made.
        self.queue: dict = {}
        # NONE UNTIL `gh` HAS ANSWERED, for the reason `self.held` is (#433): an
        # empty list is an answer this panel counts and sorts, so standing it in
        # for "not asked yet" is how a panel comes to state something it does not
        # know. `render_issues` paints when BOTH answers are in and not before.
        self.issues: list[dict] | None = None
        self.issue_err: str | None = None
        # What the board says is in force. `{}` UNTIL IT HAS ANSWERED, and
        # `fetch_dials` sets `asked` when it has — the header cell renders nothing
        # at all until then, because "no dial is set" and "nobody has asked" are
        # different facts and the first is the one a person acts on (#244).
        self.dials: dict = {}
        #: The credential the writes go out on, or None until `on_mount` has a
        #: config to build it from. See `qd.HumanClient` — and #479 for what it
        #: costs, which is not this file's to re-argue but is this file's to say.
        self.human: "qd.HumanClient | None" = None
        self.plan: dict = {}                      # the whole /plan envelope
        self.plan_err: str | None = None
        self.plan_sig: tuple | None = None
        # 'owner/repo#n' → the claim on it, and NONE UNTIL THE BOARD HAS ANSWERED:
        # `{}` means "the board says nothing is held", which is an answer this
        # panel sorts on. See `render_issues` for what the distinction buys and
        # for its limit — it is the FIRST paint that is protected, not every one.
        self.held: dict | None = None
        # Why `held` looks the way it does. Set while the last poll FAILED, so a
        # `{}` that means "the board is unreachable" is never read as a `{}` that
        # means "nothing is claimed" — see render_board.
        self.claims_err: str | None = None
        self.detail_text = ""
        #: The dial a write is in flight for, or None. Read on the UI thread and
        #: written only there — `run_dial_write` clears it through
        #: `call_from_thread`. It exists so a second press can be REFUSED rather
        #: than supersede an `exclusive` worker that has not reported yet (#577).
        self.dial_writing: str | None = None
        self.last_dispatch: tuple[str, float] | None = None
        # Where launched work runs, what it runs, and whether it asks first.
        # A DIRECTORY, and that is why `--repo` cannot simply be assigned to it:
        # the flag takes a slug as readily as a checkout, and a slug names a repo
        # this process may have no checkout of. `--repo <checkout>` therefore moves
        # this too — the point of pointing a screen at a project is that the ⚒
        # starts work IN it — while `--repo <slug>` leaves it where it was and the
        # guards below refuse the rows it cannot reach, out loud.
        self.repo = repo or os.environ.get("QB_DASH_REPO") or os.getcwd()
        # WHICH repo that directory is, because /fix-issue takes a bare number
        # and resolves the repository from the checkout it runs in. The panels
        # list several repos now, so "issue #12" is not an address on its own.
        self.repo_slug = qd.repo_slug(self.repo)
        # WHICH AGENT THE ⚖ RUNS, and it is the dash's own knob rather than a
        # seat's. It read `QB_SEAT_AGENT` until #540 retired that family, which
        # would have left this the last reader of a variable nothing else sets and
        # no documentation mentions — a knob that looks live and is not.
        #
        # NOT `QB_SEAT_INITIAL_CMD`, which is the nearest surviving thing and is
        # the wrong shape: that is a whole command LINE and may carry a prompt of
        # its own, so composing it with the one below would produce
        # `claude-yolo -- /get-involved -- /panel-review-pr 42`. A binary is what
        # this needs and a binary is what it asks for.
        self.agent_bin = os.environ.get("QB_DASH_AGENT", "claude")
        # The ⚒ runs this rather than the agent directly (#371). Resolved once
        # and kept, so that a test can point it somewhere and so that the two
        # calls the button makes — `--policy` before the click and the spawn
        # after it — cannot end up asking two different binaries.
        self.start_bin = sibling("qb-start")
        self.confirm = os.environ.get("QB_DASH_CONFIRM", "1") != "0"
        self.pr_err: str | None = None
        # WHICH LAYOUT IS UP, and it starts narrow because that is what the pane
        # `qb-seats` splits off is. `relayout` compares against this rather than
        # against the class, so a resize that does not cross the threshold — which
        # is most of them, since attaching a client resizes every pane on the
        # screen — costs a comparison and no reflow.
        self.wide = False
        self.wide_at = env_columns("QB_DASH_WIDE", self.WIDE_COLUMNS)
        # Whether WORK also lists the open PRs review has finished with and the
        # open issues nobody has planned or taken. `b` toggles it; the printed
        # renderer takes `--backlog`, which is the same decision made once at
        # launch because that one has no keyboard.
        self.backlog = os.environ.get("QB_DASH_BACKLOG", "").strip().lower() in (
            "1", "true", "yes", "on")
        # Only the rows a person owes an answer about — the terminal half of
        # #274's one door. `None` until the board has answered, so the header cell
        # can tell "nobody is waiting" from "nobody has been asked" (#244).
        self.blockers: dict | None = None
        self.waiting = os.environ.get("QB_DASH_WAITING", "").strip().lower() in (
            "1", "true", "yes", "on")

    # ---- layout ---------------------------------------------------------

    def compose(self) -> ComposeResult:
        # Above the board line, because it governs every pane below it: the seats
        # spend one subscription between them, and the window they are working
        # towards is the one number none of the tables can show.
        yield Static("", id="limits")
        yield Static("quarterback — connecting…", id="head")
        # A PANEL PER TABLE, and the pairing is structural rather than visual: a
        # grid places cells, so a title loose in the container is a cell of its
        # own and lands in a different column from the table it names. Wrapping
        # them changes nothing about the narrow screen — one `Vertical` inside
        # another lays out identically — and is the whole of what the wide one
        # needed from the tree.
        with Vertical(id="body"):
            # Above the seats, and above everything the seats then do: this is the
            # configuration every panel below is running under, which is the caps
            # line's own argument for being at the top. It is two rows when nothing
            # is set — the printed renderer makes the same choice in the same place
            # (qb-dash.panel_dials).
            with Vertical(classes="panel", id="p_dials"):
                yield Static("DIALS", classes="title", id="t_dials")
                yield ClickTable(id="dials", cursor_type="row")
            with Vertical(classes="panel", id="p_agents"):
                yield Static("AGENTS", classes="title", id="t_agents")
                yield ClickTable(id="agents", cursor_type="row", zebra_stripes=False)
            with Vertical(classes="panel", id="p_work"):
                yield Static("WORK", classes="title", id="t_work")
                yield ClickTable(id="work", cursor_type="row")
        yield Static("click: seat→pane, ✕→close it, ＋→add one, agent→where it "
                     "is, claim→its note, work row→why it is where it is, "
                     "⚖→panel review, ⚒→fix issue, ✎→set or clear a dial"
                     "   b for the backlog, ? for keys",
                     id="detail")
        yield Footer()

    def on_mount(self) -> None:
        # BEFORE the board client, which is allowed to fail: a machine with no
        # board configured still gets a laid-out dashboard saying so, and a
        # `return` above this would leave a wide pane in the narrow layout for as
        # long as it stayed exactly that wide.
        self.relayout()
        # NOT in build_columns, and that is the one table here for which that is
        # right: its scope cell names the LAYER a value came from — fleet or this
        # repo — rather than a project, and that is half of what a dial's answer
        # is. See qbdata.dial_where.
        self.query_one("#dials", DataTable).add_columns(
            "", "✎", "dial", "value", "in force", "left", "why")
        self.build_columns()
        try:
            self.client, self.cfg = qd.board_client()
            # Built unconditionally and asked later whether it can do anything.
            # `why_not()` is configuration rather than a live check, so a box with
            # no session still gets an object that explains itself — which is what
            # lets the ✎ say why instead of going missing.
            self.human = qd.HumanClient(self.cfg)
        except Exception as exc:                  # noqa: BLE001
            self.query_one("#head", Static).update(
                Text(f"no board configured: {type(exc).__name__}", style="bold red"))
            return
        self.refresh_seats()
        self.refresh_limits()
        self.refresh_board()
        self.refresh_plan()
        self.refresh_prs()
        self.refresh_issues()
        self.set_interval(self.limits_interval, self.refresh_limits)
        self.set_interval(self.interval, self.refresh_seats)
        self.set_interval(self.interval, self.refresh_board)
        self.set_interval(self.plan_interval, self.refresh_plan)
        self.set_interval(self.gh_interval, self.refresh_prs)
        self.set_interval(self.gh_interval, self.refresh_issues)

    def build_columns(self) -> None:
        """Give the two tables their columns, per the current scope.

        Called on mount and again on every `s`, because the repo cell is not a
        setting on a row — it is a whole column, and adding or removing one means
        rebuilding the table. `clear(columns=True)` first: without it the second
        call appends a duplicate set and every row is drawn against the wrong
        headers.

        THE ACTION ICON STAYS IN COLUMN 1, and with two tables that rule finally
        means what it always said. There were four constants for it — PANEL,
        FIX, KILL, EDIT — all equal to 1 and all documented as "the same column
        everywhere on purpose"; there is now one column per table carrying
        whatever verb the row under it wants, so the toggling repo cell still has
        to sit after it, and nothing else indexes past.
        """
        repo = ("repo",) if self.scope.column else ()
        for table_id, columns in (
            # `stage` goes between `state` and the toggling repo cell: it is a
            # fixed column, so it must sit ABOVE the one that comes and goes for
            # the same reason the action icon does (#262).
            ("#agents", ("", "✕", "who", "state", "stage", *repo, "what", "ttl")),
            # `kind` before the repo cell for the same reason, and it is the
            # column that makes one table legible where four panels needed none:
            # `iss` and `pr` were the panel you were looking at (#272).
            ("#work", ("", "⚒", "kind", *repo, "rank", "ref", "title", "who")),
        ):
            table = self.query_one(table_id, DataTable)
            table.clear(columns=True)
            table.add_columns(*columns)

    def repo_cell(self, repo: str) -> list[Text]:
        """The repo cell, or no cell at all — one place, so the four tables agree.

        A list rather than an optional value because that is how it is spliced into
        a row: an empty one contributes nothing, and a row built by concatenation
        cannot get the cell count wrong the way a row built by `if` can.
        """
        return [] if not self.scope.column else [
            Text(qd.clip(repo, 11), style=qd.repo_colour(repo))]

    # ---- data (threads, so a slow board never freezes the ui) -----------

    @work(thread=True, exclusive=True, group="seats")
    def refresh_seats(self) -> None:
        seats = qd.tmux_seats()
        self.call_from_thread(self.render_seats, seats)

    @work(thread=True, exclusive=True, group="limits")
    def refresh_limits(self) -> None:
        limits, err = qd.fetch_limits()
        self.call_from_thread(self.render_limits, limits, err)

    @work(thread=True, exclusive=True, group="board")
    def refresh_board(self) -> None:
        """Presence, claims and the open questions, on one clock.

        A blocker is raised and answered by people and agents acting NOW, so it
        belongs on the fast clock beside presence rather than the ninety-second
        `gh` one: a surface that lagged it would show work as stuck that somebody
        has just unstuck, on the one surface #274 asks a person to trust.
        """
        data = qd.fetch_board(self.client)
        self.call_from_thread(self.render_board, data)
        self.call_from_thread(self.render_blockers, qd.fetch_blockers(self.client))

    @work(thread=True, exclusive=True, group="plan")
    def refresh_plan(self) -> None:
        """The plan and the dials, on one clock and in one worker.

        Both are board calls answering a question a PERSON changed — a reorder, a
        floor moved, a tempo turned down — rather than something the fleet does to
        itself every few seconds, so neither wants the four-second clock and both
        want the same one. `refresh_prs` pairs the review queue with the PR list
        for the same kind of reason.
        """
        plan, err = qd.fetch_plan(self.client)
        self.call_from_thread(self.render_plan, plan, err)
        self.call_from_thread(self.render_dials, qd.fetch_dials(self.client))

    @work(thread=True, exclusive=True, group="prs")
    def refresh_prs(self) -> None:
        """The PR list and the queue derived from it, in one worker.

        The queue is a BOARD call and still rides the gh clock, because it is a
        join over the very PR rows fetched on the line above. Given its own timer
        it would answer about PRs up to ninety seconds newer than the ones OPEN
        PRs is drawing, and the two panels would disagree about any PR that moved
        in between. `qb-dash.fetch_gh` makes the same choice for the same reason.

        The PR table is rendered BEFORE the queue is fetched rather than after,
        so a slow or dead board costs the queue panel and not the PR panel.
        """
        prs, err = qd.fetch_prs()
        self.call_from_thread(self.render_prs, prs, err)
        # `pr_err` and not a bare list: `fetch_prs` answers a failed `gh` with
        # ([], err), and deriving from that empty list would have the board
        # honestly report a drained queue for a repo with eight PRs waiting.
        queue = (qd.fetch_review_queue(self.client, prs, pr_err=err)
                 if self.client is not None else {})
        self.call_from_thread(self.render_queue, queue)

    @work(thread=True, exclusive=True, group="issues")
    def refresh_issues(self) -> None:
        issues, err = qd.fetch_issues()
        self.call_from_thread(self.render_issues, issues, err)

    # ---- rendering -------------------------------------------------------

    def render_seats(self, seats: list[dict]) -> None:
        """The panes of the screen this dashboard is sitting in.

        They are rows of AGENTS now rather than a panel of their own, and the
        merge is what SEATS was always half of: tmux knows which panes exist and
        only the board knows what the agent in one is doing, and the join between
        them was drawn by eye across a border. A seat whose agent has exited still
        has a pane, still shows, and is still exactly the one worth closing.
        """
        self.seats = seats
        self.render_agents()


    def render_limits(self, limits: list[dict], err: str | None,
                      width: int | None = None) -> None:
        """Claude Code's own caps, as bars — `5h ████░░ 64% 3h57m  7d ██░ 41% 5d8h`.

        A failed call keeps the last figures rather than blanking the line: they
        are minutes old and still roughly true, and a line that vanished on every
        hiccup would read as "no limits", which is the opposite of what it means.

        THE ROW IS HIDDEN WHEN IT HAS NOTHING TO SAY, and since #426 that is both
        halves empty rather than the caps half. An install with no subscription
        token has no cells — and neither does a pane under 20 columns, which is
        `limit_cells`' own floor and one `C-q <` away — so gating the whole row on
        the caps took the review depth off the screen in exactly the two cases the
        queue cell was put here to survive.
        """
        if limits:
            self.limits = limits
        self.limits_err = err
        try:
            bar = self.query_one("#limits", Static)
        except Exception:                         # noqa: BLE001 — a resize before mount
            return
        # `width` when a resize handed one over — see on_resize for why the app's
        # own size is a resize behind in there. Everywhere else it is unset and
        # the app's size is the current one.
        pane = self.size.width if width is None else width
        cells = qd.limit_cells(self.limits, max(20, pane - 2))
        tempo = qd.tempo_cell(self.dials)
        anything = bool(cells or self.queue or tempo or self.prs or self.pr_err
                        or self.issues is not None or self.issue_err
                        or self.blockers is not None)
        bar.display = anything
        if not anything:
            return
        text = Text()
        for i, (label, glyphs, pct, reset, colour) in enumerate(cells):
            if i:
                text.append("  ")
            text.append(label, style="bold grey70")
            if glyphs:
                text.append(f" {glyphs}", style=colour)
            text.append(f" {pct}", style=f"bold {colour}")
            if reset:
                text.append(f" {reset}", style="grey50")
        # The `?` annotates the caps, so it needs caps to annotate: on a line
        # carrying only the queue cell it would be a mark against a number that
        # is not there.
        if err and cells:
            text.append(" ?", style="grey50")
        # The queue's depth and age ride the caps line beside the budget they
        # would be spent out of — a panel round costs tokens, and "3 waiting" is
        # only actionable next to how much is left. A depth of ZERO still draws:
        # "nothing is waiting" is the answer this panel exists to make reachable,
        # and a cell that vanished when it was true could not be told apart from
        # a dashboard that never asked.
        if self.queue:
            label, depth, age, colour = qd.queue_cell(self.queue)
            if text.plain:                        # no caps to sit beside: no gap
                text.append("   ")
            text.append(label, style="bold grey70")
            text.append(f" {depth}", style=f"bold {colour}")
            if age:
                text.append(f" {age}", style="grey50")
        # THE ONE DOOR, and it goes FIRST. Every other number on this line is
        # about what the fleet is doing to itself; this one is the only thing on
        # the pane that is somebody's to act on, and #274's argument is that it
        # needs one place a person always sees it. Ten correct escalations went
        # unread for two days on this repo (#569) — not because nobody looked, but
        # because looking meant opening something.
        cell = qd.blocker_tally(self.blockers, self.scope)
        if cell is not None:
            word, colour = cell
            label, _, count = word.partition(" ")
            if text.plain:
                text.append("   ")
            text.append(label, style="bold grey70")
            text.append(f" {count}", style=f"bold {colour}")
        # THE TWO TALLIES THE TOGGLED LISTS LEFT BEHIND (#589). OPEN PRs and
        # ISSUES were read for their headline numbers as much as for their rows,
        # and the CI tally is the one thing on that pair which the review queue
        # cannot supply: a PR can be green and unreviewed, or red and already
        # signed off. A `b` that took the number away with the list would be a way
        # of forgetting the work exists.
        if self.prs or self.pr_err:
            if text.plain:
                text.append("   ")
            text.append("PRs", style="bold grey70")
            if self.pr_err:
                text.append(" ?", style="bold red")
            else:
                text.append(f" {len(self.prs)}", style="bold grey70")
                for word, colour in qd.pr_tally(self.prs):
                    text.append(" · ", style="grey50")
                    text.append(word, style=colour)
        if self.issues is not None or self.issue_err:
            if text.plain:
                text.append("   ")
            text.append("ISSUES", style="bold grey70")
            if self.issue_err:
                text.append(" ?", style="bold red")
            else:
                text.append(f" {len(self.issues or [])}", style="bold grey70")
                # Not "N free" while the board is unreachable: `free` is counted off
                # claims that are stale or were never fetched, and a count taken from
                # no claims at all is how a seat gets sent into work somebody holds.
                # `claims_err` as well as `held is None`: the first means the
                # board has not answered yet and the second that its last answer
                # failed, and a free count taken from either is a count taken from
                # no claims at all — which is how a seat is sent into work somebody
                # already holds. The table's title has kept this guard since #433;
                # the header line was counting without it.
                if self.held is not None and not self.claims_err and self.issues:
                    free = sum(1 for i in self.issues
                               if qd.issue_key(i) not in self.held)
                    text.append(" · ", style="grey50")
                    text.append(f"{free} free", style="green")
        # And the throttle beside the budget it protects (#477). The caps say what
        # the seats MAY spend; this says whether they are supposed to be spending
        # it at all, and a reader glancing at one is asking about the other. Every
        # state draws once the board has answered, `unset` included — a cell that
        # vanished when nothing was set could not be told from a dashboard that
        # never asked, and "nothing is throttling the fleet" is exactly the answer
        # somebody is looking for at 94% of a window.
        if tempo:
            label, value, life, colour = tempo
            if text.plain:
                text.append("   ")
            text.append(label, style="bold grey70")
            text.append(f" {value}", style=f"bold {colour}")
            if life:
                text.append(f" {life}", style="grey50")
        bar.update(text)

    def on_resize(self, event: Resize) -> None:
        """Re-lay to the new width — the bars are sized to the pane, the panels
        are one column or two by it, and the dash pane is resized every time the
        screen is.

        THE WIDTH COMES OFF THE EVENT, NEVER OFF `self.size`, and that is not
        style. Measured on textual 8.2: the handler runs BEFORE the app's own
        size is updated, so a `self.size.width` read here is the width the pane
        had before the resize being handled. The caps bar has been laying itself
        out one resize behind ever since it was sized to the pane — invisible,
        because dragging a border emits a stream of them and the last-but-one is
        near enough — and a layout threshold read the same way is not invisible
        at all: it would take two crossings to go two-across, and a pane that
        crossed once and stopped would sit in the wrong layout indefinitely.
        """
        self.relayout(event.size.width)
        self.render_limits(self.limits, self.limits_err, width=event.size.width)

    def relayout(self, width: int | None = None) -> None:
        """One column or two, decided by the width the pane actually has.

        Textual has no media query, so this is the media query: `on_resize` fires
        on every width the pane is given — a client attaching, a seat closing, a
        `C-q >`, a zoom — and the class it sets is the whole of what `#body.-wide`
        keys off.

        THE ORDER IS THE SAME IN BOTH LAYOUTS, and that is new. Seven panels over
        a two-column grid had to be re-paired by hand — a grid fills row by row in
        DOM order, so the narrow order put OPEN PRs and REVIEW QUEUE in different
        rows, and "the queue sits directly under the PRs" was the arrangement #273
        asked for rather than a coincidence of the order they were added in. With
        DIALS spanning both columns and AGENTS and WORK beside each other there is
        no pairing left to arrange, so this is the class and nothing else (#589).

        Losing the `move_child` is worth saying out loud, because it was load
        bearing in the other direction too: it ran BEFORE `build_columns` in
        `on_mount`, so when it raised on a panel this change had removed, the
        tables were never given their columns at all and every row after it failed
        with "More values provided than there are columns" — a layout call taking
        down the data path four functions away.
        """
        wide = (self.size.width if width is None else width) >= self.wide_at
        if wide == self.wide:
            return
        self.wide = wide
        self.query_one("#body", Vertical).set_class(wide, "-wide")

    def render_board(self, data: dict) -> None:
        """Presence and claims: the AGENTS table, and what WORK needs from claims."""
        self.board = data
        every = data.get("agents", [])
        head = self.query_one("#head", Static)
        if data.get("error"):
            head.update(Text(f"● board unreachable — {qd.clip(data['error'], 60)}",
                             style="bold red"))
        else:
            # NO COUNTS HERE. This line stated a live count and a seat count worked
            # out from its own join, while the table below stated the same two off
            # the rows it had actually drawn — and on the first frame with a seat
            # in it they disagreed: "4 live · 1 seats" over a table headed "5 live
            # · 3 seats". `qbdata.agent_tally` is the one place they are counted
            # now, and this line says what it alone knows: which board, and which
            # slice of it.
            head.update(Text(f"● {self.cfg.base_url}   {self.scope.label()}",
                             style="green"))

        # Keyed on the SESSION id, which is what the pane carries. An agent with no
        # session is dropped rather than filed under "": a pane with no agent looks
        # up the empty string, and a bucket there would answer it with somebody.
        # FROM EVERY AGENT, not the ones this scope shows — a seat pane belonging
        # to another project's screen is still a pane in front of you.
        self.seat_states = {s: a for a in every if (s := a.get("session"))}
        self.render_agents()

        # Who holds which issue comes off the same claims, and only the holder is
        # displayed — so compare on that, not on the whole claim. A claim renewing
        # changes its expiry every time, and rebuilding for that would move a row
        # out from under a click.
        # From every claim, NOT the ones this scope shows: an issue on this screen
        # held by an agent working out of another repo's checkout is still held.
        held = qd.claims_by_issue(data.get("claims", []))
        # A FAILED POLL ANSWERS NOTHING ABOUT CLAIMS, and it arrives shaped exactly
        # like an answer: `fetch_board` reports an outage as `{"claims": [],
        # "error": …}`, which is indistinguishable here from "nobody holds
        # anything". Taking it as one overwrites a real prior answer with `{}`,
        # repaints every issue as free, and defeats the ⚒'s guard — which reads
        # `held` and cannot see WHY it is empty.
        stale, self.claims_err = self.claims_err, data.get("error")
        if self.claims_err:
            # The last good answer stands; only its freshness changed. Except when
            # there is no last good answer — then this is the first one, and the
            # backlog has to be released rather than left waiting on a board that
            # is down. `{}` is the only order available then, and `claims_err` is
            # what stops it being read as knowledge: the title says so and the ⚒
            # refuses.
            if self.held is None:
                self.held = {}
            self.render_work()
            return
        # `self.held is None` is its own reason to render: the FIRST answer has to
        # reach the table even when it is empty, and comparing holders alone cannot
        # see that transition — {} and {} are equal. A board that has just COME
        # BACK is the same shape of transition, hence `stale`.
        if self.held is None or stale or holders(held) != holders(self.held):
            self.held = held
        self.render_work()

    def render_plan(self, plan: dict, err: str | None) -> None:
        """The board's plan — every repo's list, in the board's own order.

        Both tables, not one: WORK is ordered by it, and AGENTS needs it to say
        what a claim is ON. A claim keyed `item:<uuid>` is 36 hex characters
        without the item behind it, and an issue number is a number without the
        words (`qbdata.claim_summary`).
        """
        self.plan, self.plan_err = plan, err
        self.render_agents()
        self.render_work()


    def render_dials(self, dials: dict) -> None:
        """Which dials are in force, which layer answered, why, and for how long — #477.

        A dial is a setting: the repo supplies a default, the board states the
        value IN FORCE, and the layer that answered is part of the answer (#305).
        Nothing a person or an agent looks at showed one until this panel — the
        value governing every round on the fleet was set from a browser endpoint,
        read back by one function in `panel_seats.py`, and invisible everywhere
        else.

        **The last row is always the door.** `POST /dials` takes
        `app.auth.delegated`
        and this dashboard holds a machine bearer token, which is precisely the
        credential that gate exists to refuse — every agent on a box holds it, and
        nothing inside a request distinguishes one from a person. So the row that
        opens the board's dials page is drawn whether or not there is a dial above
        it, because the reader who most needs it is the one who has just found out
        that nothing is set.
        """
        self.dials = dials
        table = self.query_one("#dials", DataTable)
        table.clear()
        self.rows = {k: v for k, v in self.rows.items() if not k.startswith("dial:")}
        rows = dials.get("dials") or []
        # ASKED ONCE PER PAINT, not per row: it is the same answer for every one
        # of them, and it decides whether the ✎ is a control or an explanation.
        # A verb that looks available and fails on the click is the shape that
        # reads as a broken button — and this one would fail against a board that
        # is perfectly healthy, because what is missing is on this host.
        cannot = self.human.why_not() if self.human else "no board configured"
        pencil = "grey30" if cannot else "bold cyan"
        for row in rows:
            where, where_style = qd.dial_where(row)
            life, life_style = qd.dial_life(row)
            by = " · ".join(x for x in (row.get("set_by"), qd.ago(row.get("set_at"))) if x)
            key = f"dial:{row.get('repo') or 'fleet'}:{row.get('dial')}"
            key = table.add_row(
                Text("●", style="cyan"),
                Text("✎", style=pencil),
                Text(qd.clip(row.get("dial"), 34), style="bold white"),
                Text(qd.dial_value(row, 14), style="cyan"),
                Text(where, style=where_style),
                Text(life, style=life_style),
                # The argument for the value, which the board requires and which is
                # the difference between a dial somebody can decide to remove and
                # one nobody dares touch. Clipped here, in full on a click.
                Text(qd.clip((row.get("reason") or "") + (f"  ({by})" if by else ""), 40),
                     style="grey50"),
                key=key,
            ).value
            self.rows[str(key)] = row

        err = dials.get("error")
        if err:
            # A ROW and not a suffix on the title, for the same reason the review
            # queue puts its errors in one: the title is bounded by the pane and
            # would clip the one message that says why the panel is empty.
            table.add_row(Text("!", style="red"), Text(""), Text(""), Text(""),
                          Text(""), Text(""), Text(qd.clip(err, 40), style="red"),
                          key="dial:error")
        if not rows and not err:
            table.add_row(Text(""), Text(""),
                          Text("every dial at its repo default" if dials.get("asked")
                               else "asking the board…", style="grey50"),
                          Text(""), Text(""), Text(""), Text(""), key="dial:none")
        # The door, always. Registered in `self.rows` so a click reaches
        # `dispatch_row` — an unregistered key is dropped, which is right for a
        # row with no verb and wrong for the only row here that has one.
        # THE LAST ROW IS THE VERB THIS PANEL DID NOT USED TO HAVE, and it says
        # which one it is: a live ✎ sets a dial from here, a dead one still opens
        # the page that can. Drawn whether or not there is a dial above it,
        # because the reader who most needs it is the one who has just found out
        # that nothing is set.
        page = table.add_row(
            Text(""), Text("✎", style=pencil),
            Text(qd.clip(f"set a dial — {cannot}" if cannot else "set a dial",
                         92), style="grey50" if cannot else "cyan"),
            Text(""), Text(""), Text(""), Text(""), key="dial:page").value
        self.rows[str(page)] = {"page": True}

        # A COUNT IS A CLAIM: "0 in force" over a board that would not answer says
        # the fleet is running on its defaults, which is the one thing an
        # unanswered read cannot establish.
        if err:
            title = "DIALS · unreadable"
        elif not dials.get("asked"):
            title = "DIALS · asking"
        else:
            title = f"DIALS · {len(rows)} in force"
        shadowed = len(dials.get("shadowed") or [])
        if shadowed:
            # A fleet dial every repo on this screen overrides. Counted rather than
            # drawn, because it is NOT in force here — and silence would leave the
            # person who set it fleet-wide unable to see what became of it.
            title += f" · {shadowed} overridden"
        self.query_one("#t_dials", Static).update(title)
        # The header cell carries the tempo off this same answer, and it is drawn
        # on the limits clock — an hour long. Without this it would keep last
        # hour's value while the panel below showed this minute's.
        self.render_limits(self.limits, self.limits_err)

    def render_prs(self, prs: list[dict], err: str | None) -> None:
        """`gh`'s open PRs, kept for WORK to draw and for the header to count."""
        self.prs, self.pr_err = prs, err
        self.render_work()
        # The header carries this list's count and CI tally, and it is drawn on the
        # limits clock — three minutes. Without this the cell up there would keep
        # the last refresh's numbers while the rows down here showed this one's.
        self.render_limits(self.limits, self.limits_err)

    def render_queue(self, queue: dict) -> None:
        """The derived review queue — the rows of WORK that are about a PR (#273).

        It has no panel of its own any more and it did not need one: OPEN PRs and
        REVIEW QUEUE printed the same PRs, because this is derived from that list.
        What it contributes to a row is the verb and the wait, which is what the
        queue was ever read for.
        """
        self.queue = queue
        self.render_work()
        # The caps line carries this same depth, and it is drawn on the limits
        # clock — an hour long. Without this the cell up there would keep last
        # hour's number while the rows down here showed this minute's.
        self.render_limits(self.limits, self.limits_err)

    def render_issues(self, issues: list[dict] | None, err: str | None) -> None:
        """`gh`'s open issues — the backlog half of WORK, behind `b`."""
        self.issues, self.issue_err = issues, err
        self.render_work()
        self.render_limits(self.limits, self.limits_err)   # the count on the header


    def render_agents(self) -> None:
        """Who is here and how they are doing — SEATS, FLEET and CLAIMED as one
        table (#589).

        Three panels drew one subject. A seat is an agent with a pane in front of
        you; a claim is what an agent is holding; `render_board`'s claims half
        already re-derived its holder by splitting `holder` on `/` exactly the way
        its fleet half did. The border between them was the only reason the join
        was never drawn, and a reader asking "what is jasper-moss doing" joined
        three tables by eye.

        The rows this could not draw before are the ones worth drawing. A claim
        whose holder no live agent answers for is work somebody holds that nobody
        is doing, and in CLAIMED it looked exactly like a live one — see
        `qbdata.CLAIM_ONLY_STATE` for the two ways that happens and why they are
        told apart rather than collapsed.

        Rebuilt on every tick, which is what FLEET and CLAIMED both did: every row
        here carries a countdown, so there is nothing for a signature to protect
        (that argument belongs to WORK, and lives in `qbdata.work_sig`).
        """
        rows, hidden = qd.agent_rows(self.board, self.scope,
                                     qd.plan_items(self.plan), self.seats)
        table = self.query_one("#agents", DataTable)
        table.clear()
        for row in rows:
            what, what_style = row["what"]
            # `＋1` rather than a second line: a row is one agent, and an agent
            # holding two things is one agent holding two things. The rest is one
            # click away, in the detail line.
            if row.get("extra"):
                what = f"{what}  ＋{row['extra']}"
            seat = row["kind"] == "seat"
            key = table.add_row(
                Text("●" if row["live"] else "·", style="green" if row["live"] else "grey50")
                if seat else Text(""),
                # Only a PANE can be closed. An agent on another machine and a
                # claim nobody answers for have nothing here to shut, and a live-
                # looking ✕ on either would be the "drawn takeable, refused one by
                # one" this dashboard has spent three issues removing.
                Text("✕", style="bold red") if seat else Text("✕", style="grey30"),
                Text(qd.clip(row["who"], 13),
                     style="bold green" if seat else
                     ("bold" if row["agent"] is not None else "yellow")),
                Text(*row["state"]),
                # What `state` cannot say: `working` reads the same writing the
                # first cut and coming out of the third review round, and so do
                # repo, branch and title. This is the cell that moves (#262).
                Text(*row["stage"]),
                *self.repo_cell(row["repo"] or "—"),
                # The mark goes on the cell the dropped column widened, and marks
                # the row the scope KEPT without being able to attribute it: with
                # the repo cell gone, an agent outside any checkout otherwise reads
                # as one working here (qbdata.scope_mark).
                Text(qd.scope_mark(self.scope, row["repo"])
                     + qd.clip(what, 40 if self.scope.column else 50),
                     style=what_style),
                Text(row["ttl"], style="red" if row.get("expiring") else "grey50"),
                key=row["key"],
            ).value
            self.rows[str(key)] = row
        # The ＋ is a ROW rather than a key, because the whole point of this panel
        # is that the mouse can do it. It carries a record of its own so
        # dispatch_row has something to look up — a row key with nothing behind it
        # is dropped on the floor.
        blank = [Text("")] * (1 if self.scope.column else 0)
        add_key = table.add_row(Text(""), Text("＋", style="bold cyan"),
                                Text("add seat", style="cyan"),
                                Text(""), Text(""), *blank, Text(""), Text(""),
                                key="seat:add").value
        self.rows[str(add_key)] = {"kind": "add", "add": True}
        self.query_one("#t_agents", Static).update(
            f"AGENTS · {qd.agent_tally(rows)}{qd.elsewhere(hidden)}")

    def work_action(self, row: dict) -> tuple[str, str | None]:
        """``('⚖', 'panel')`` — the icon this row's verb column wears, and what a
        click on it does. `None` for the verb means the icon is dim.

        ASKED ONCE, BY BOTH SIDES. The renderer draws what this returns and
        `dispatch_row` runs what it returns, so an icon that looks live and then
        explains itself is not reachable from here. Four panels each decided this
        for themselves and each got a slightly different answer — the queue offered
        its ⚖ only for `review`/`re-review` while OPEN PRs offered one for any
        reachable PR, and the same PR could be on both.

        That disagreement is now a rule with one home: a PR the queue is waiting on
        offers the round the queue is waiting FOR, and nothing else. `fix`,
        `rebase` and `land` are real next actions with no button on this dashboard,
        and `answer` is owed by a human — a live ⚖ on any of them starts a round
        that is spent on the wrong thing, and a conflicting branch burns a whole
        panel round to tell you it is conflicting (#271).
        """
        # A question owed to a person is not something this dashboard can start.
        # The remedy is the person, and the row says which one.
        # NO GLYPH AT ALL on a row that is only a question. The state cell already
        # wears the ⚑ and a second beside it says the same thing twice; an empty
        # verb cell says the true thing, which is that there is no verb here — the
        # remedy is the person the row names.
        if row["kind"] == "blocker":
            return "", None
        # AND NOTHING IS OFFERED ON A ROW A PERSON OWES AN ANSWER ABOUT, whatever
        # else it is. Taking an issue whose shape is still being decided, or
        # spending a panel round on a PR somebody has been asked whether to revert,
        # is work done before the answer that governs it — which is the waste #522
        # is about, arriving through a button. The icon keeps its shape and goes
        # grey, so the row still says what it WOULD be, and the click explains.
        if row.get("blocked"):
            return ("⚖" if self.work_pr(row) else "⚒"), None
        target = self.work_pr(row)
        if target is not None:
            entry = row.get("entry") or {}
            wanted = (not entry) or entry.get("next_action") in ("review", "re-review")
            live = wanted and self.wrong_repo(target.get("repo"), "") is None
            return "⚖", ("panel" if live else None)
        issue = self.work_issue(row)
        if issue is None:
            return "⚒", None
        taken = (qd.plan_holder(row["item"]) if row.get("item")
                 else (self.held or {}).get(qd.issue_key(issue)))
        live = not taken and self.wrong_repo(issue.get("repo"), "") is None
        return "⚒", ("fix" if live else None)

    def work_pr(self, row: dict) -> dict | None:
        """``{repo, number}`` for a row that is about a pull request, else None.

        A row is about a PR three ways — it came off the review queue, it came off
        the open-PR list, or it is a plan item whose ref is a `pr` — and only the
        first two carry a `gh` row. The launcher wants a repo and a number and
        nothing else, so this hands it those rather than whichever dict happened
        to be available.
        """
        if row.get("pr"):
            return {"repo": row["pr"].get("repo"), "number": row["pr"].get("number")}
        if row.get("entry"):
            return {"repo": row["entry"].get("repo"), "number": row["entry"].get("pr")}
        item = row.get("item") or {}
        ref = item.get("ref") or {}
        if ref.get("kind") == "pr" and str(ref.get("value") or "").isdigit():
            return {"repo": qd.plan_repo(item), "number": int(ref["value"])}
        # A question ABOUT a pull request still names one, and `o` should reach it:
        # the row exists because the PR is not otherwise on this table, which makes
        # it the row least likely to be findable any other way.
        subject = row.get("subject") or {}
        if subject.get("kind") == "pr" and str(subject.get("value") or "").isdigit():
            return {"repo": row.get("repo"), "number": int(subject["value"])}
        return None

    def work_issue(self, row: dict) -> dict | None:
        """The issue behind a row — its own, or the one its plan item points at."""
        if row.get("issue"):
            return row["issue"]
        return qd.plan_issue(row["item"]) if row.get("item") else None

    def render_work(self) -> None:
        """What is in flight, in the board's order — PLANS, REVIEW QUEUE, OPEN PRs
        and ISSUES as one table (#589).

        Four panels answered "what work is there" from four sources, and the same
        unit of work appeared on up to four of them at once. Two were the same rows
        outright: the review queue is derived FROM the open-PR list, so it is a
        subset by construction and all the PR panel added was the CI glyph — which
        is this table's state cell for a PR row, per #272.

        **The order is the board's and is not re-derived.** Work the plan does not
        carry is appended below it, unranked: the board deliberately refuses to
        rank the queue (#232 owns the order), and inventing a position here is the
        second-answer-about-the-plan defect `render_plan` already had removed.

        NOTHING FROM `gh`'s ISSUE LIST IS PAINTED UNTIL THE BOARD HAS SAID WHAT IS
        CLAIMED — #433, and the same rule the ISSUES panel kept: an issue list
        sorted before the claims are in is a list this table is about to rearrange,
        and a reader picks a row by looking at it. The plan and the queue do not
        wait, because neither is sorted on claims; that is a strictly better answer
        than the panel gave, where a slow board held back rows it had no bearing on.
        """
        # `issues=None` until the board has answered: `work_rows` then appends no
        # issue rows at all, which is the wait above expressed where it belongs.
        rows, hidden = qd.work_rows(
            self.plan, self.prs, self.queue,
            self.issues if self.held is not None else None,
            self.held, self.scope, self.backlog,
            blockers=(self.blockers or {}).get("blockers"),
            waiting_only=self.waiting)
        # ONE SIGNATURE WHERE THERE WAS ONE AND THREE REBUILDS. PLANS had a guard
        # and OPEN PRs, REVIEW QUEUE and ISSUES each cleared and rebuilt on every
        # tick of their worker, so this table is steadier than three of the four it
        # replaces. Everything the TITLE reports that no row carries is in it, for
        # the reason the hidden count is: the board's answer about what to pick up
        # can move while every row on the pane stays exactly as it was.
        sig = (hidden, self.backlog, self.waiting, self.plan_err, self.pr_err,
               (self.blockers or {}).get("error"), self.issue_err,
               self.claims_err, self.held is None, qd.plan_next_id(self.plan),
               tuple(sorted((self.plan.get("counts") or {}).items())),
               (self.plan.get("order_trust") or {}).get("unchosen"),
               self.plan.get("truncated"), qd.work_sig(rows))
        if sig == self.plan_sig:
            return
        self.plan_sig = sig

        table = self.query_one("#work", DataTable)
        table.clear()
        for row in rows:
            glyph, colour = row["glyph"]
            why, why_colour = row["why"]
            rank, rank_colour = row["rank"]
            icon, verb = self.work_action(row)
            key = table.add_row(
                Text(glyph, style=colour),
                Text(icon, style="bold cyan" if verb else "grey30"),
                Text(qd.work_kind(row), style="grey50"),
                *self.repo_cell(qd.short_repo(row["repo"] or "fleet")),
                Text(rank, style=rank_colour),
                Text(row["ref"], style="bold grey70"),
                # A fleet-wide item has no repo to name, and with the column gone
                # it would read as one of this project's (qbdata.scope_mark).
                Text(qd.scope_mark(self.scope, row["repo"])
                     + qd.clip(row["title"], 38 if self.scope.column else 48),
                     style="grey50" if row["dim"] else "white"),
                Text(qd.clip(why, 17), style=why_colour),
                key=row["key"],
            ).value
            self.rows[str(key)] = row

        # THE STATES THAT ARE NOT ROWS. An error is a ROW and not a suffix on the
        # title: the title is bounded by the pane's width and was clipping these to
        # 24 characters, and a table whose job is saying WHY something is waiting
        # must not truncate the one message that says why it cannot tell you.
        # None is registered in `self.rows` — a key with nothing behind it is
        # dropped by dispatch_row, which is what a row with no verb wants.
        blank = [Text("")] * (1 if self.scope.column else 0)
        # FOUR SOURCES, FOUR NAMES. Both `gh` failures were called `gh` and the row
        # key carried the first 24 characters of the message, so two `gh` calls
        # failing the same way — which is the usual way for them to fail — keyed
        # two rows identically. `ClickTable.add_row` degrades that rather than
        # raising, but it also logs it as a panel whose keys are not unique, which
        # would be a true complaint about a fixable name.
        troubles = [(name, err) for name, err in
                    (("board", self.plan_err), ("queue", (self.queue or {}).get("error")),
                     ("blockers", (self.blockers or {}).get("error")),
                     ("prs", self.pr_err), ("issues", self.issue_err)) if err]
        for name, err in troubles:
            table.add_row(Text("!", style="red"), Text(""), Text(""), *blank,
                          Text(""), Text(""),
                          Text(qd.clip(f"{name}: {err}", 38 if self.scope.column else 48),
                               style="red"), Text(""), key=f"err:{name}:{err[:24]}")
        # WHICH ANSWER IS STILL OUT, and only where it costs a row: the backlog is
        # the half of this table sorted on claims, so it is the half that waits for
        # them (#433). Named rather than said generically — blaming the board for a
        # `gh` failure it already knows about sends a reader to the wrong end of the
        # problem, which is the ISSUES title's own argument kept through the merge.
        waiting = [w for w, missing in (("the board", self.held is None),
                                        ("gh", self.issues is None))
                   if missing] if self.backlog else []
        # "Nothing is in flight" and "nothing could be FETCHED" are different
        # answers, and the board supplies its own wording for a drained queue. Only
        # when nothing failed and nothing is outstanding: a table that said both
        # would be answering its own error message with a claim it has no grounds
        # for, and one that said it while an answer was in flight would be stating
        # a fact it is about to replace (#244).
        if not rows and not troubles and not waiting:
            table.add_row(Text(""), Text(""), Text(""), *blank, Text(""), Text(""),
                          Text("nothing is waiting on a person" if self.waiting else
                               ((self.queue or {}).get("idle") or "nothing in flight"),
                               style="grey50"), Text(""), key="work:idle")

        # The heading is one line and clips at the pane edge, so it is given the
        # room it has — none of which is known before the first layout, where the
        # width reads 0 and "no room" would drop every segment there is.
        items, _ = qd.in_scope(qd.plan_items(self.plan), self.scope)
        # `held is None` as well as `claims_err`: the first means the board has not
        # answered YET and the second that its last answer failed, and a free count
        # taken from either is a count taken from no claims at all — which is how a
        # seat gets sent into work somebody already holds.
        tally = qd.work_tally(rows, self.prs, self.issues, self.held, self.backlog,
                              claims_known=self.held is not None and not self.claims_err,
                              waiting_only=self.waiting)
        room = (self.query_one("#t_work", Static).size.width - 11
                - sum(len(bit) + 3 for bit in tally) - len(qd.elsewhere(hidden)))
        # The plan's counts describe the PLAN, so a filtered view does not claim
        # them: `39 open · next #1620` over two rows a person owes an answer about
        # is a title about a list the reader has just asked not to see.
        title = ("WAITING · " if self.waiting else "WORK · ") + " · ".join(
            ([] if self.waiting else
             [text for text, _ in qd.plan_head_bits(self.plan, items, hidden,
                                                    room if room > 20 else None)]) + tally)
        title += qd.elsewhere(hidden)
        if waiting:
            title += f" · backlog waiting for {' and '.join(waiting)}"
        elif self.claims_err:
            title += f" · claims unknown: {qd.clip(self.claims_err, 20)}"
        # Text(), not str: `gh`'s stderr reaches this line through the tally and a
        # bracketed token in it — `ConnectionRefusedError: [Errno 111] …` — is a
        # Rich style tag to a Static that parses markup, and the panel that exists
        # to explain a stalled state would raise MarkupError instead.
        self.query_one("#t_work", Static).update(Text(title))

    def say(self, text: str) -> None:
        # Kept on the app as well as in the widget: a Static does not hand back
        # what it was last given, and this line is the app's only visible answer
        # to "did that click do anything", so it has to be assertable.
        self.detail_text = text
        self.query_one("#detail", Static).update(Text(text, style="bold"))

    def alarm(self, text: str) -> None:
        """`say`, for the answers a person must not be able to walk past.

        The detail line is `color: $text-muted` and it is right for almost
        everything that lands there — a row's expansion, what a launch did, which
        pane took the key. A WRITE THAT DID NOT HAPPEN is the exception, and #577
        is what the exception cost: a dial write that failed on a missing
        credential said so in grey on a line the eye reads as chrome, next to a
        modal that had already dismissed as if it had worked.

        Bold red and the bell, which is exactly what `DialEdit._refuse` does one
        screen up. The two are the same event — this side could not do what was
        asked — and they had no business looking different.
        """
        self.detail_text = text
        self.query_one("#detail", Static).update(Text(text, style="bold red"))
        self.bell()

    # ---- clicks ----------------------------------------------------------

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """The keyboard path (Enter). Ignored when a click just did the same row."""
        key = str(event.row_key.value)
        if self.last_dispatch and self.last_dispatch[0] == key \
                and time.monotonic() - self.last_dispatch[1] < 0.5:
            return
        self.dispatch_row(key)

    def dispatch_row(self, key: str, column: int | None = None) -> None:
        """What a click does, by what the row IS rather than by which table it is in.

        There were six branches keyed on a table name and four constants all equal
        to 1 saying where the verb column was. With two tables the rule is what it
        always claimed to be: column 1 is the verb, everything else on the row is
        the explanation — and what the verb DOES is asked of `work_action`, which
        is the same call the renderer made when it decided whether to draw the icon
        live. An icon that looks clickable and then explains itself is not
        reachable from here.
        """
        record = self.rows.get(key)
        if record is None:
            return
        self.last_dispatch = (key, time.monotonic())
        kind = record.get("kind") or key.split(":", 1)[0]
        if kind == "add":
            self.add_seat()
        elif kind in ("seat", "agent", "claim"):
            self.dispatch_agent(record, kind, column)
        elif kind == "dial":
            # The ✎ edits; anything else on the row says what the board said, in
            # full. With no credential on this host the ✎ is the door it always
            # was — the browser — and says so rather than opening a modal whose
            # save could only fail.
            if column == self.VERB_COLUMN or record.get("page"):
                self.edit_dial(None if record.get("page") else record)
            else:
                self.say(qd.dial_detail(record))
        else:
            self.dispatch_work(record, column)

    def dispatch_agent(self, row: dict, kind: str, column: int | None) -> None:
        """A seat, an agent, or a claim nobody answers for.

        The ✕ closes a PANE and only a pane, which is why it is dim on the other
        two: an agent on another machine and an unheld claim have nothing here to
        shut. Everything else on the row explains it — where the agent is, or what
        the claiming agent said it was doing, which for a `gone` claim is the only
        record of it left.
        """
        if kind == "claim":
            claim = row["claim"]
            self.say(f"{qd.clip(claim.get('key'), 60)} — "
                     + qd.clip(claim.get("note") or "(no note on this claim)", 340))
            return
        if kind == "seat" and column == self.VERB_COLUMN:
            self.close_seat(row["seat"])
            return
        if row.get("agent") is not None:
            self.click_agent(row["agent"])
        elif row.get("seat") is not None:
            self.jump_pane(row["seat"])

    def dispatch_work(self, row: dict, column: int | None) -> None:
        """A plan item, a PR under review, or an issue nobody has taken.

        The verb column starts the round or takes the issue; the rest of the row
        says why it is where it is. A PR the queue is waiting on explains the WAIT
        rather than opening GitHub — that is what the queue row did and it is the
        more useful of the two answers — and `o` opens it either way.
        """
        if column == self.VERB_COLUMN:
            verb = self.work_action(row)[1]
            if verb == "panel":
                self.panel_pr(self.work_pr(row))
                return
            if verb == "fix":
                if row.get("item"):
                    self.fix_plan_item(row["item"])
                else:
                    self.fix_issue(self.work_issue(row))
                return
            # A dim icon that swallows the click is indistinguishable from a broken
            # one, so it falls through and says what the row is instead.
        # THE QUESTION FIRST, whatever else the row is. A row that is waiting on a
        # person is waiting whoever holds it and whatever review is owed on it, so
        # the queue's verb and the plan's note are both the less useful answer.
        if row.get("blocked"):
            self.say(qd.blocker_detail(row["blocked"]))
        elif row.get("entry"):
            self.say(qd.queue_detail(row["entry"]))
        elif row.get("item"):
            # With the envelope, so the row the board named `next` can show the
            # caveat the board attached to that recommendation.
            self.say(qd.plan_detail(row["item"], self.plan))
        elif row.get("pr"):
            self.open_pr(row["pr"])
        elif row.get("issue"):
            self.open_issue(row["issue"])

    # ---- the seats ---------------------------------------------------------
    #
    # All three of these shell out to qb-seat-click rather than driving tmux
    # here, and that is the point: the seat bar in the status line and this
    # panel are two front ends onto ONE definition of what closing or adding a
    # seat means. Closing is kill-pane AND a reflow of the row; adding is
    # qb-seats --add, which knows not to reuse the number of a seat that was
    # closed. Two copies of that would drift, and the drift would show up as a
    # ragged layout or a board that thinks a new agent is an old one returning.

    def seat_click(self, tag: str, session: str, prompt: str) -> None:
        command = f"qb-seat-click {shlex.quote(tag)} {shlex.quote(session)}"
        if self.confirm:
            self.push_screen(
                Confirm(prompt, command, self.repo),
                lambda go: self.run_seat_click(tag, session) if go
                else self.say("cancelled"),
            )
        else:
            self.run_seat_click(tag, session)

    def run_seat_click(self, tag: str, session: str) -> None:
        if not os.environ.get("TMUX"):
            self.say(f"not inside tmux — nothing to click: qb-seat-click {tag}")
            return
        try:
            done = subprocess.run(["qb-seat-click", tag, session],
                                  capture_output=True, text=True, timeout=30)
        except Exception as exc:                   # noqa: BLE001
            self.say(f"could not run qb-seat-click ({type(exc).__name__})")
            return
        if done.returncode:
            self.say(qd.clip(done.stderr.strip() or f"qb-seat-click {tag} failed", 120))
        else:
            self.say(f"{tag} — done")
        self.refresh_seats()

    def seat_state(self, seat: dict) -> dict:
        """What the board says about the agent in this pane, or {}.

        ONE LOOKUP, ON THE SESSION ID. The pane carries `@qb_session` — the
        conversation `qb-hook` stamped on it — and every agent `/active` returns
        carries the same id, so the two sides join exactly.

        This used to narrow three times and could still answer nothing. It started
        from every agent whose NAME parsed to this pane's seat number, kept the
        ones whose project matched, then the ones on this machine, and took the
        survivor only if exactly one was left. Both narrowings were needed and
        neither was sound: `list-panes -a` is the whole tmux server, so two screens
        could each hold a seat 1 (#208); the board is the whole fleet, so two
        machines could each hold a `seat-lexray-1`; and the machine half was a
        GUESS at this host's board name, which comes from the token map and need
        not be the hostname. A session id has none of those problems, and a pane
        running an agent nobody named a seat resolves too (#540).

        Empty `agent` — a pane with no agent in it — matches nothing, which is the
        answer: the state cell stays blank because there is no state.
        """
        return self.seat_states.get(seat.get("agent") or "", {})

    def seat_session(self) -> str | None:
        """Which screen to act on: the one the seats are in, not the cursor's.

        The dashboard is usually a pane of that same screen, but it does not
        have to be — and a --add aimed at the wrong session would build a second
        screen rather than growing this one.
        """
        for s in getattr(self, "seats", []):
            if s.get("session"):
                return s["session"]
        return None

    def close_seat(self, seat: dict) -> None:
        session = seat.get("session")
        if not session:
            self.say("that seat has no session — refresh and try again")
            return
        self.seat_click(f"kill{seat['seat']}", session,
                        f"close seat {seat['seat']}? the agent in it goes too")

    def add_seat(self) -> None:
        session = self.seat_session()
        if not session:
            self.say("no seat screen on this server — start one with qb-seats")
            return
        self.seat_click("add", session, f"add a seat to {session}?")

    def action_expand(self) -> None:
        """`z` — this pane to a window of its own, and back.

        THROUGH `qb-seat-click`, exactly as the ＋ and the ✕ do, and for their
        reason: the ⛶ on the top line, `C-q z` and this key are three front ends
        onto ONE definition of what expanding means, which is `qb-seat-key
        expand`. Two copies of the break-and-rejoin would be two places for the
        geometry lore to drift.

        NOT CONFIRMED, unlike the ✕. Nothing is killed, no process is touched —
        the dash keeps polling across the move — and the same key puts it back.
        """
        session = self.seat_session()
        if not session:
            self.say("no seat screen on this server — nothing to expand into")
            return
        self.run_seat_click("expand", session)

    def jump_pane(self, seat: dict) -> None:
        """Move the tmux cursor to a seat's pane, by pane id.

        By ID, because this row already knows the pane — jump_to_agent's search
        over `list-panes -a` is for the FLEET table, whose rows come off the board
        and know a session id rather than a pane.
        """
        pane = seat.get("pane")
        if not pane or not os.environ.get("TMUX"):
            self.say(f"seat {seat.get('seat')} — {seat.get('path') or '?'}")
            return
        try:
            subprocess.run(["tmux", "select-window", "-t", pane], timeout=5)
            subprocess.run(["tmux", "select-pane", "-t", pane], timeout=5)
        except Exception as exc:                   # noqa: BLE001
            self.say(f"could not jump ({type(exc).__name__})")
            return
        self.say(f"jumped to seat {seat['seat']}")

    # ---- launching work ---------------------------------------------------

    def wrong_repo(self, repo: str | None, what: str) -> str | None:
        """Why ``what`` cannot be started from this dashboard, or None if it can.

        BOTH launchers need this and for the same reason. `/fix-issue` and
        `/panel-review-pr` take a bare number and resolve the repository from the
        checkout the pane opens in, while this dashboard WATCHES repos it may have
        no checkout of — so a number off another repo's row lands on whatever issue
        or PR wears it here. For a review that is a comment and a pushed fix commit
        on a stranger's pull request, which is the one click on this pane that
        cannot be taken back; `--repo <checkout>` is how a screen gets to start
        that project's work, because it moves the directory too.

        **A guard that cannot tell refuses.** `self.repo_slug` is None whenever
        `git remote get-url origin` came back empty — a checkout whose remote is
        `upstream` (the fork case this feature's own slug comparison was written
        for), no `git` on PATH, a timeout — and reading that as "nothing to check"
        made this return None for EVERY row, silently. `gh` and `git push` resolve
        a default remote without consulting `origin`, so the review would have gone
        out anyway, against whatever that remote points at. The cost of failing
        closed is a message on a click; the cost of failing open is a comment and a
        commit on a stranger's PR.
        """
        if not repo:
            return None
        if not self.repo_slug:
            return (f"cannot tell which repo {self.repo} is — no origin remote, so "
                    f"{what} cannot be aimed from here. Set QB_DASH_REPO, or start "
                    "it from that checkout")
        # Case-folded, like every other repo comparison this feature added: `repo`
        # arrives in GitHub's canonical casing and `repo_slug` in whatever the origin
        # URL was typed as, and `PrisonBlues/quarterback` is a working remote.
        if repo.strip().lower() == self.repo_slug.strip().lower():
            return None
        return (f"{what} is in {repo}; this dashboard runs in {self.repo_slug} "
                "— start it from that checkout")

    def panel_pr(self, pr: dict) -> None:
        """Kick off /panel-review-pr for a PR, in a pane of the seat row.

        It used to open a window, on the argument that a review is not a seat
        and the row's widths mean something. Both halves are still true and the
        conclusion was still wrong: a review in a window is a review you go and
        find later, and the reason to click a PR here rather than open it on
        GitHub is to watch the thing happen. run_in_pane keeps it out of the
        seat machinery — see @qb_label there. It runs the agent with its brief
        positionally, after `--`, so a prompt beginning with `-` is read as a
        prompt rather than as a flag.
        """
        number = pr.get("number")
        # The same refusal the ⚒ on an issue row makes, for the same reason and
        # with more at stake. /panel-review-pr takes a bare number and resolves
        # the repository from the checkout it runs in, so a PR from a repo this
        # dashboard only WATCHES would review whatever wears that number HERE —
        # and this one spends money, comments on a public PR and pushes a fix
        # commit to it. Until #209 that click was unreachable, because a second
        # repo sharing a number crashed the panel before anything rendered; now
        # both rows are there, so the guard has to be too.
        if (why := self.wrong_repo(pr.get("repo"), f"PR #{number}")):
            self.say(why)
            return
        command = f"{shlex.quote(self.agent_bin)} -- {shlex.quote(f'/panel-review-pr {number}')}"
        if self.confirm:
            self.push_screen(
                Confirm(f"panel-review PR #{number}?", command, self.repo),
                lambda go: self.run_in_pane(f"panel-{number}", command) if go else
                self.say("cancelled"),
            )
        else:
            self.run_in_pane(f"panel-{number}", command)

    def fix_plan_item(self, item: dict) -> None:
        """The ⚒ on a plan row: take the issue the item points at.

        Most plan items point at nothing — a line of plan is not an issue — and
        one that is already claimed is somebody's current work. Both say so
        rather than doing nothing, because a dim icon that swallows the click is
        indistinguishable from a broken one.
        """
        issue = qd.plan_issue(item)
        if issue is None:
            self.say(f"no issue behind this item — {qd.clip(item.get('title'), 60)}")
            return
        # An item inside somebody else's HELD PLAN is taken too, and this is the
        # click that spends money on it. `claim` alone let the ⚒ start /fix-issue
        # on a line of a plan another agent had reserved as a unit — the exact
        # duplicated work the plan claim exists to prevent, from the panel that
        # exists to show who is on what.
        holder = qd.plan_holder(item)
        if holder:
            who = holder.get("holder") or "?"
            self.say(f"#{issue['number']} is already being worked by {who}"
                     if item.get("claim") else
                     f"#{issue['number']} is inside a plan {who} holds — talk to "
                     f"them rather than taking one line out of it")
            return
        self.fix_issue(issue)

    def fix_issue(self, issue: dict) -> None:
        """Kick off /fix-issue for an issue — THROUGH `qb-start`, since #371.

        It used to compose `claude -- /fix-issue N` here and hand it to tmux, and
        what that started was a session nothing could count: outside `qb-admit`'s
        in-flight window, holding no claim, and known to the board only once the
        agent's own SessionStart hook got round to saying so. `qb-start` is the
        primitive that fixes all three (#277, #360) and until now nothing pulled
        it. This is its first caller: the cheapest possible trigger, because a
        click is still a human hand and so it needs no new safety at all — the
        gates, the machine cap, the allowlist and the claim are all at the
        primitive, and this only has to ask it.

        **AND ON A MACHINE THAT HAS NOT OPTED IN IT REFUSES, WITH THE REMEDY.**
        That is the obstacle #360 named and declined to walk into: `qb-start`
        ships off, so routing a working button through it makes the button stop
        working until somebody writes one line of nix. The alternative — fall
        back to the old direct spawn when the gate says no — was rejected, and
        the argument is in `spawn_refusal`.

        A held issue is now refused rather than warned about, which reverses this
        method's own previous sentence. That sentence was right about a click
        that took NO CLAIM: warning and letting you proceed cost nothing but your
        own judgement. This click takes the claim, so proceeding is `qb-claim`
        refusing at exit 8 — a dialog whose only possible outcome is no. What was
        a warning is now the answer, and it names the way to release a claim that
        has genuinely lapsed.

        **THIS ONE READS A SNAPSHOT, AND SAYS SO.** `self.held` is the board's
        answer from up to one poll ago, so a claim released two seconds back is
        still on it and this refuses work that is in fact free — the one thing
        the atomic claim would have got right. Which is why the message names its
        source and the key that re-reads it rather than stating the holder as a
        fact: `qb-claim` remains the authority, and it is still the one that
        settles the race in the other direction, where the panel shows an issue
        as free and the spawn is refused at exit 8. What is traded is a stale
        refusal a keypress fixes, against a confirmation dialog that could only
        ever end in the same no, three board round trips later.
        """
        number = issue.get("number")
        # Somebody else's work redone under the wrong title, if this is skipped —
        # see wrong_repo, which the ⚖ shares because the mistake is the same one.
        if (why := self.wrong_repo(issue.get("repo"), f"#{number}")):
            self.say(why)
            return
        # UNKNOWN IS NOT FREE. `self.held` is None until the board answers, and
        # `or {}` here would read that as "nothing is claimed" at the one click
        # that spends money — reachable, because the PLANS ⚒ arrives on its own
        # worker and can be live while ISSUES is still waiting. The wait is a
        # poll long and `r` shortens it.
        if self.held is None or self.claims_err:
            self.say(f"#{number}: the board has not answered"
                     + (f" ({qd.clip(self.claims_err, 40)})" if self.claims_err else " yet")
                     + ", so nothing here knows whether it is claimed — `r` re-reads it")
            return
        if (holder := holders(self.held).get(qd.issue_key(issue))):
            self.say(f"#{number} is claimed by {holder} (the board's last answer, "
                     f"`r` re-reads it) — the spawn takes that claim, so "
                     f"`qb-release issue {number}` is what frees it")
            return
        self.start_work("/fix-issue", number, f"start /fix-issue on #{number}?")

    # ---- the ⚒, through qb-start ------------------------------------------

    def start_work(self, command: str, number: int | str, prompt: str) -> None:
        """Ask this machine, then ask the human, then ask `qb-start`.

        In that order, and the first one is the point. A button that raises a
        confirmation, takes the click and THEN says the machine never opted in
        has spent somebody's attention telling them something it could have
        known before it drew itself — and #371 is explicit that a button which
        appears to work and does not is worse than one that is absent.
        """
        if (why := self.spawn_refusal(command)):
            self.say(why)
            return
        # The window `qb-start` will make, spelled its way rather than this
        # panel's old `fix-<n>`: the message this ends on tells you what to go
        # and look at, and a name only the dashboard uses is one you cannot find.
        name = f"{command.lstrip('/')}-{number}"
        argv = self.spawn_argv(command, number)
        shown = " ".join(shlex.quote(a) for a in argv)
        if self.confirm:
            self.push_screen(
                Confirm(prompt, shown, self.repo),
                lambda go: self.run_spawn(name, argv) if go else self.say("cancelled"),
            )
        else:
            self.run_spawn(name, argv)

    def spawn_argv(self, command: str, number: int | str) -> list[str]:
        """What the ⚒ runs. `--via dash` is the provenance #371 asks for: it lands
        on the claim note, on the board post and on the pane as `@qb_spawn_via`,
        so a session somebody finds running can be traced to the click that asked
        for it rather than guessed at."""
        return [self.start_bin, command, str(number), "--repo-path", self.repo,
                "--via", "dash", "--json"]

    def spawn_refusal(self, command: str) -> str | None:
        """Why this machine will not start `command`, or None if it will.

        `qb-start --policy` is the question, so this is not a second reading of
        `spawn.json` — one gate, asked rather than reimplemented. It costs one
        local process and reads one file: no board, no tmux, no network. Asked on
        every click rather than cached at mount, so opting a machine in takes
        effect on the next click instead of on the next dashboard.

        **`--no-board` is what keeps that true, and it is deliberate (#563).**
        `--policy` now reads the board's `spawn.max_sessions` by default, because
        the callers that read a ceiling before acting want the one in force. This
        one is not such a caller — it asks two questions, *is this machine on* and
        *is this command allowed*, and both are answered by the file — and it runs
        on the UI thread, where a board that is down would freeze the screen for
        five seconds on every keystroke. So it opts out of the read it does not
        use, and gives up nothing: a ceiling this button never consulted is still
        applied by the spawn itself, one step later, in `qb-start`'s own words.

        **AND IT DOES NOT FALL BACK TO THE OLD SPAWN.** The tempting shape is
        obvious — refuse through `qb-start`, and when the machine has not opted
        in start the session the way this button did last week — and it is wrong
        three times over. It would make "this machine has not opted in" a fact
        about which code path ran rather than about the machine. It would put two
        behaviours behind one icon, a counted, claimed, board-recorded session on
        one box and an uncounted one on another, with nothing on screen to say
        which you got. And it would set the precedent for the next trigger, which
        will not have a human behind it. A permission with a fallback is not a
        permission; the honest cost is one line of nix, once, on the machine
        somebody wants this on.
        """
        try:
            got = subprocess.run([self.start_bin, "--policy", "--no-board", "--json"],
                                 capture_output=True, text=True, timeout=15)
        except Exception as exc:                       # noqa: BLE001
            # Fails CLOSED, and says which failure it was. `qb-start` missing is
            # a broken install, not a machine that said no, and the two want
            # different things done about them.
            return (f"⚒ cannot ask {self.start_bin} whether this machine may "
                    f"spawn ({type(exc).__name__}) — the ⚒ goes through qb-start "
                    f"now, and a gate that cannot be asked has not said yes")
        try:
            answer = json.loads(got.stdout)
        except ValueError:
            answer = {}
        if not answer.get("enabled"):
            return "⚒ " + qd.clip(
                answer.get("reason") or got.stderr.strip()
                or f"qb-start --policy exited {got.returncode} and said nothing", 240)
        if command not in (answer.get("commands") or []):
            return (f"⚒ {command} is not on this machine's allowlist — name it in "
                    f"`programs.quarterback-harness.spawn.commands` "
                    f"({answer.get('policy')} allows: "
                    f"{', '.join(answer.get('commands') or []) or 'nothing'})")
        return None

    @work(thread=True, group="spawn")
    def run_spawn(self, name: str, argv: list[str]) -> None:
        """Run `qb-start` and report what it answered.

        In a thread, unlike every other subprocess this app runs from a click:
        those are tmux calls that return in milliseconds, and this one asks
        `qb-pace`, `qb-admit` and `qb-claim` in turn — three board round trips
        before a pane exists. On the ui thread that is a dashboard that stops
        redrawing while it starts a session.

        Every refusal is reported in `qb-start`'s OWN words rather than
        translated by exit code here. There are seven of them, each with a
        different remedy, and a second copy of those sentences in this file would
        be a second copy to keep true.
        """
        # Outside tmux, `run_in_window`'s answer and its reason: the useful thing
        # to say is the exact command, and it is said AFTER the confirmation
        # because a dialog that never appears is also a dialog that cannot be
        # tested. Before `qb-start` runs, though, and that part is new — it would
        # otherwise pass every gate, take the claim and post the spawn before
        # discovering it has nowhere to put a pane, then hand both back. A
        # refusal that costs three board round trips is worse than the same
        # refusal made here.
        if not os.environ.get("TMUX"):
            self.call_from_thread(
                self.say, "not inside tmux — run it yourself: "
                          + " ".join(shlex.quote(a) for a in argv))
            return
        try:
            done = subprocess.run(argv, capture_output=True, text=True, timeout=300)
        except Exception as exc:                       # noqa: BLE001
            self.call_from_thread(
                self.say, f"could not run qb-start ({type(exc).__name__})")
            return
        self.call_from_thread(self.say, spawn_answer(name, done))


    def run_in_window(self, name: str, command: str) -> None:
        """A detached tmux window running `command`, dropping to a shell after.

        Detached (-d) so a review starting does not yank the screen away from
        whatever you were reading; `exec $SHELL` after it so the window survives
        the command and its output can still be read.
        """
        # Checked HERE, not before the confirmation: outside tmux there is still
        # a useful answer — the exact command — and a dialog that never appears
        # is also a dialog that cannot be tested.
        if not os.environ.get("TMUX"):
            self.say(f"not inside tmux — run it yourself: {command}")
            return
        shell = os.environ.get("SHELL", "/bin/bash")
        full = f"{command}; exec {shlex.quote(shell)} -i"
        try:
            done = subprocess.run(
                ["tmux", "new-window", "-d", "-n", name, "-c", self.repo, full],
                capture_output=True, text=True, timeout=10,
            )
        except Exception as exc:                       # noqa: BLE001
            self.say(f"could not start {name}: {type(exc).__name__}")
            return
        if done.returncode:
            self.say(f"tmux refused: {qd.clip(done.stderr, 60)}")
        else:
            self.say(f"started window '{name}' — Ctrl-b n to watch it")

    def run_in_pane(self, name: str, command: str) -> None:
        """`command` in a new pane of the seat row, beside the work it is about.

        A review read in a window you have to switch to is a review you go and
        look at later; one in the row is one you watch while the seats carry on.
        That is the whole argument, and it costs the running seats some width —
        `select-layout -E` spreads the row evenly again, so the cost is shared
        rather than taken out of whichever seat happened to be split.

        The pane gets @qb_label and NOT @qb_seat, which is what keeps it out of
        the way of everything else: qb-seats' --add, qb-seat-click's reflow and
        the seat bar all select on @qb_seat, so none of them counts a review as
        a seat or offers to start an agent in it. @qb_label rather than a name of
        its own because qb-seats already labels the dash and the tape that way,
        and the pane border should read one option, not two.

        With no seat row to join — the dashboard run from a bare terminal, or a
        screen whose seats have all been closed — it falls back to a window
        rather than inventing a layout.
        """
        if not os.environ.get("TMUX"):
            self.say(f"not inside tmux — run it yourself: {command}")
            return
        seats = qd.tmux_seats()
        if not seats:
            self.run_in_window(name, command)
            return
        shell = os.environ.get("SHELL", "/bin/bash")
        full = f"{command}; exec {shlex.quote(shell)} -i"
        try:
            done = subprocess.run(
                ["tmux", "split-window", "-h", "-P", "-F", "#{pane_id}",
                 "-t", seats[0]["pane"], "-c", self.repo, full],
                capture_output=True, text=True, timeout=10,
            )
        except Exception as exc:                       # noqa: BLE001
            self.say(f"could not start {name}: {type(exc).__name__}")
            return
        if done.returncode:
            self.say(f"tmux refused: {qd.clip(done.stderr, 60)}")
            return
        pane = done.stdout.strip()
        subprocess.run(["tmux", "set-option", "-p", "-t", pane, "@qb_label", name],
                       capture_output=True, timeout=5)
        subprocess.run(["tmux", "select-layout", "-t", pane, "-E"],
                       capture_output=True, timeout=5)
        self.say(f"'{name}' is in the seat row — Ctrl-b x closes it")

    def click_agent(self, agent: dict) -> None:
        if self.jump_to_agent(agent.get("session")):
            self.say(f"jumped to {agent.get('holder')}")
            return
        self.say(
            f"{agent.get('holder')} · {agent.get('model') or '?'} · "
            f"{agent.get('repo') or '?'}@{agent.get('branch') or '?'} · "
            f"{agent.get('cwd') or '?'}"
        )

    def jump_to_agent(self, session: str | None) -> bool:
        """Move the tmux cursor to the pane this agent's conversation is in.

        ON THE SESSION ID, which the pane carries as `@qb_session` — so a click on
        a FLEET row lands on the right pane or on none, with nothing to narrow and
        nothing to guess. It used to jump by seat NUMBER parsed out of the holder's
        name, which meant a screen had to be picked between: two screens can each
        have a seat 1 (#208), so the click went to the wrong project about half the
        time until a scope was threaded through to break the tie (#540).

        A FLEET row for an agent on another machine simply matches no pane here,
        which is the honest answer and the same one it gave before.
        """
        if not session or not os.environ.get("TMUX"):
            return False
        try:
            out = subprocess.run(
                ["tmux", "list-panes", "-a", "-F", "#{pane_id}\t#{@qb_session}"],
                capture_output=True, text=True, timeout=5,
            ).stdout
        except Exception:                          # noqa: BLE001
            return False
        found = [p for p in (line.split("\t") for line in out.splitlines())
                 if len(p) == 2 and p[1] == session]
        pane = found[0][0] if len(found) == 1 else None
        if pane is None:
            return False
        try:
            subprocess.run(["tmux", "select-pane", "-t", pane], timeout=5)
        except Exception:                          # noqa: BLE001
            return False
        return True

    def open_pr(self, pr: dict) -> None:
        self.open_url(f"https://github.com/{pr.get('repo') or qd.REPO}/pull/{pr.get('number')}")

    def open_issue(self, issue: dict) -> None:
        self.open_url(f"https://github.com/{issue.get('repo') or qd.REPO}"
                      f"/issues/{issue.get('number')}")

    def edit_dial(self, row: dict | None) -> None:
        """Open the editor on one dial — or on a new one, when `row` is None.

        **Falls back to the browser rather than to a dead modal.** With no session
        on this host the write cannot succeed, and a form that took four fields
        and then said so would have spent the person's typing to tell them
        something it knew before they started. The page is still there and still
        works, which is the whole reason the read-only version shipped first.
        """
        cannot = self.human.why_not() if self.human else "no board configured"
        if cannot:
            self.say(f"{cannot} — opening the board's dials page instead")
            self.open_url(qd.dials_url(self.cfg))
            return
        # WHICH SCOPE a new dial lands in, decided here rather than in the modal:
        # this screen already knows which project it is about, and a scope picker
        # in a 78-column modal is a control that would be got wrong in a hurry.
        # Editing an existing row keeps that row's own scope, which is the only
        # answer that can mean "change what I am looking at".
        repo = (row or {}).get("repo") if row else self.new_dial_scope()
        label = repo or "fleet (every repo)"
        # The harness's own dial table, the board's answer, and the board's clock —
        # the three things the modal cannot work out for itself. `dial_vocabulary`
        # resolves once per process and answers `{}` on a box with no harness/loops
        # beside this script, which is the form as it shipped: free text and no
        # refusal (#539).
        self.push_screen(DialEdit(row, repo, label,
                                  vocabulary=qd.dial_vocabulary(),
                                  trouble=qd.dial_trouble(),
                                  in_force=self.dials,
                                  now=(self.dials or {}).get("now")),
                         self.dial_written)

    def new_dial_scope(self) -> str | None:
        """Which repo a NEW dial belongs to — off the SCOPE, never off the cwd.

        The rows on this pane are `Scope`'s (`QB_DASH_REPOS`, or `--repo`, or the
        launch directory's origin, in that order). `self.repo_slug` is only the
        last of those, and it is where work is LAUNCHED rather than what is being
        shown: a pane started in one checkout with `QB_DASH_REPOS=owner/other`
        displays `other`'s dials and would have written the dial to the checkout's
        repo instead. Same name, different setting, and nothing on screen
        afterwards says which one took it — the one mistake this panel must not
        let a person make.

        None means the fleet, and it is the honest answer twice over: a wide pane
        is about several projects and cannot choose between them, and a sole repo
        this process knows only by a bare name is not one the board would accept
        (`owner/name` is its shape). The modal states whichever answer this gives
        in bold before anything is written.
        """
        if self.scope.column:
            return None                      # several projects, or the wide view
        named = [r for r in self.scope.repos if "/" in r]
        return named[0] if len(named) == 1 else None

    def dial_written(self, asked: dict | None) -> None:
        """What the modal came back with, turned into one board write.

        Runs on the UI thread and hands the HTTP off to a worker: `op read` can
        prompt and the board can be slow, and a dashboard that froze mid-write
        would look like the thing it is trying to avoid being.
        """
        if not asked or not asked.get("dial"):
            return
        if self.dial_writing:
            # REFUSED, not superseded, and that is the whole of #577's last
            # symptom. `run_dial_write` is `exclusive=True`, so a second press
            # cancelled the first — and the first was the one holding the answer,
            # thirty seconds into a key command that had not returned yet. A
            # person who sees nothing presses it again, which is the one input
            # that guaranteed they would go on seeing nothing.
            self.alarm(f"still writing {self.dial_writing} — that one has not "
                       f"come back yet. It waits up to 30s on the key command; "
                       f"this press was ignored rather than cancelling it")
            return
        if asked.get("clear"):
            self.start_dial_write(asked, None, None)
            return
        try:
            value = qd.parse_dial_value(asked.get("value", ""))
            expires = qd.parse_dial_expiry(asked.get("expiry", ""),
                                           (self.dials or {}).get("now"))
        except (ValueError, OverflowError) as exc:
            # Refused HERE, where the sentence can name the box that was wrong,
            # rather than at a 422 that names a field nobody typed.
            #
            # OverflowError as well as ValueError, and it is not defensive
            # padding: `timedelta` raises it rather than ValueError for a duration
            # past its range, so the bounded regex and this clause are two halves
            # of one fix. Escaping here is a crash inside a Textual callback,
            # which takes the dashboard down and every other panel with it.
            self.say(str(exc))
            return
        if not (asked.get("reason") or "").strip():
            self.say("a dial needs a reason — why is this value in force? "
                     "The board refuses one without, and so does this")
            return
        self.start_dial_write(asked, value, expires)

    def start_dial_write(self, asked: dict, value, expires: str | None) -> None:
        """Announce the write, THEN start it. On the UI thread, in that order.

        The announcement is the fix, and it is one line because the hole it fills
        is one gap: between the modal dismissing and the worker returning, this
        screen said nothing at all. On a host whose key command blocks — `op read`
        against a vault that wants unlocking, which is every host on this fleet
        the day somebody's session expires — that gap is the full thirty seconds
        of the subprocess timeout, and a dismissed modal over an unchanged pane is
        indistinguishable from a write that landed.

        Naming the credential rather than saying "working…" is deliberate: the
        wait is almost always `op`, and a person who reads the word has the answer
        before the timeout does.
        """
        dial = asked["dial"]
        self.dial_writing = dial
        self.say(f"{'clearing' if asked.get('clear') else 'setting'} {dial} — "
                 f"fetching your key first (this can wait on `op`, up to 30s)")
        try:
            self.run_dial_write(asked, value, expires)
        except Exception as exc:                  # noqa: BLE001 — show it, don't die
            # THE WORKER MAY NEVER START, and a `finally` inside a function that
            # was never entered cannot clean up after it. Textual refuses new work
            # while the app is shutting down, and without this the flag stays set
            # and the refusal above rejects every write for the rest of the
            # session — a guard against one lost message that costs all of them.
            self.dial_writing = None
            self.alarm(f"could not start the write for {dial} — {exc}")

    @work(thread=True, exclusive=True, group="dialwrite")
    def run_dial_write(self, asked: dict, value, expires: str | None) -> None:
        """The write itself, off the UI thread. Never raises into Textual."""
        dial, repo = asked["dial"], asked.get("repo")
        failed = False
        try:
            if asked.get("clear"):
                got = self.human.clear_dial(dial, repo)
                cleared = got.get("cleared") or []
                said = (f"cleared {dial} — the repo's own default takes over"
                        if cleared else f"{dial} was already gone")
            else:
                got = self.human.set_dial(dial, value, asked["reason"], repo, expires)
                # WHAT IT REPLACED, said out loud: moving a dial without being told
                # what it was is how one gets nudged twice by two people who each
                # believed they were starting from the default. The endpoint
                # returns the old row for exactly this sentence.
                was = [f"{qd.dial_value(d, 40)} ({d.get('reason')})"
                       for d in (got.get("replaced") or [])]
                said = f"set {dial}" + (f" — it was {', '.join(was)}" if was else "")
        except Exception as exc:                  # noqa: BLE001 — show it, don't die
            failed = True
            # THE VERB FIRST, because the sentence is read left to right and the
            # dial name is the half a person already knows. `{dial}: {exc}` put 34
            # characters of `review_panel.budget.tokens_per_day` in front of the
            # only words that mattered. The verb is the one `start_dial_write`
            # announced, so the two lines are about visibly the same act.
            verb = "clear" if asked.get("clear") else "set"
            said = f"could not {verb} {dial} — {exc}"
        finally:
            # Cleared HERE and not at the end, so an exception that escapes the
            # reporting below cannot leave this screen believing a write is in
            # flight — which would wedge every later press against the refusal in
            # `dial_written`.
            #
            # NOT A GUARANTEE, and the honest bound is worth writing down: this
            # runs only once the worker body has been entered (the case where it
            # is not is handled at the call site), and `call_from_thread` itself
            # can be refused by an app that is already tearing down. What it
            # covers is every path that raises out of the write, which is the one
            # that happens.
            try:
                self.call_from_thread(self.clear_dial_writing)
            except Exception:                     # noqa: BLE001 — the app is going away
                pass
        self.call_from_thread(self.alarm if failed else self.say,
                              qd.clip(said, 400))
        # Straight back to the board rather than waiting out the plan clock: the
        # person is looking at the row they just changed, and a panel that showed
        # the old value for fifteen seconds would be read as a write that failed.
        self.call_from_thread(self.refresh_plan)

    def clear_dial_writing(self) -> None:
        """No write in flight. On the UI thread, which owns this flag."""
        self.dial_writing = None

    def open_dials(self) -> None:
        """The board's dials page — the only surface that can actually turn one.

        Said out loud in the detail line rather than left to be inferred from a
        browser opening. #443 is the record of what the silent version costs: a
        person told the reorder was theirs to do, in a terminal, whose reply was
        "i don't know how to re-order". A door nobody can find is the same as no
        door.

        **Still worth a key now that the panel can write.** The page shows every
        repo's dials at once where this panel shows the screen's own, so a person
        comparing two projects wants it. The `✎` on a row is the control; this is
        the map. It is also the fallback when this host has no key.
        """
        self.open_url(qd.dials_url(self.cfg))
        self.say("the board's dials page — every repo's dials at once, where this "
                 "panel shows the screen's own. Setting one from here needs a "
                 "person's key (QUARTERBACK_HUMAN_KEY_CMD); the page needs a "
                 "browser the edge has vouched for.")

    def open_url(self, url: str) -> None:
        try:
            subprocess.Popen(["xdg-open", url], stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
            self.say(f"opened {url}")
        except Exception as exc:                   # noqa: BLE001
            self.say(f"could not open ({type(exc).__name__}): {url}")

    # ---- key actions -----------------------------------------------------

    def action_open_dials(self) -> None:
        """`d` — from any table, because a dial governs all of them."""
        self.open_dials()

    def action_refresh_now(self) -> None:
        self.refresh_board()
        self.refresh_plan()          # …which fetches the dials with it
        self.refresh_prs()
        self.refresh_issues()
        self.say("refreshing…")

    def render_blockers(self, blockers: dict) -> None:
        """The open questions a person owes an answer to (#328, #274).

        Kept whole rather than joined here: `work_rows` does the join, because a
        blocker's subject is one of four kinds and three of them can name
        something this table is not otherwise drawing — and deciding that twice,
        once per renderer, is how two surfaces come to disagree about how many
        questions are outstanding.
        """
        self.blockers = blockers
        self.render_work()
        # The header carries the count and is drawn on the limits clock, which is
        # three minutes long. Without this the cell up there would keep the last
        # refresh's number while the rows down here showed this one's.
        self.render_limits(self.limits, self.limits_err)

    def action_toggle_waiting(self) -> None:
        """`w` — only the rows a person owes an answer about, or everything.

        Redrawn from what the client already has, like `s` and `b`: the board
        answered four seconds ago and this is a decision about how to READ that
        answer. The signature goes with it — the rows really are different and
        nothing else about them moved, so WORK would otherwise return early and
        leave the table exactly as it was.
        """
        self.waiting = not self.waiting
        self.plan_sig = None
        self.render_work()
        self.say("waiting: only what a person owes an answer about — w for all of it"
                 if self.waiting else "showing everything — w for what is waiting on you")

    def action_toggle_backlog(self) -> None:
        """`b` — also the work nothing is waiting on, or just what is in flight.

        Redrawn from what the client already has, like `s` and for the same
        reason: `gh` answered within the last ninety seconds and this is a
        decision about how to READ that answer. The signature has to be dropped
        with it — the rows really are different and nothing else about them moved,
        so WORK would otherwise return early and leave the table exactly as it was.
        """
        self.backlog = not self.backlog
        self.plan_sig = None
        self.render_work()
        self.say("backlog: showing open PRs review has finished with, and issues "
                 "nobody has taken — b to hide them" if self.backlog
                 else "backlog hidden — b to show it")

    def action_toggle_scope(self) -> None:
        """`s` — this project's rows, or the whole fleet's.

        Redrawn from what the client already has rather than re-fetched: the board
        answered four seconds ago and the scope is a decision about how to READ that
        answer, so a request here would spend a round trip to show the same rows.
        The plan is the exception in one detail — it redraws only when its contents
        changed, so its signature has to be dropped or the toggle would leave the
        one panel that was already on screen exactly as it was.
        """
        self.scope = self.scope.toggled()
        self.build_columns()
        # Dropped because WORK redraws only when its contents changed, and a scope
        # toggle changes which rows there are without changing any of them — so the
        # one table already on screen would be left exactly as it was.
        self.plan_sig = None
        if self.board:
            self.render_board(self.board)
        self.render_agents()
        self.render_work()
        self.say(f"scope: {self.scope.label()}"
                 + ("" if self.scope.on else " — s to narrow to this screen's"))

    def action_panel_pr(self) -> None:
        """`p` reviews the selected row, if a round is what it is waiting for."""
        row = self.selected_work()
        if row and self.work_action(row)[1] == "panel":
            self.panel_pr(self.work_pr(row))
        elif row:
            self.say("nothing here for a panel round — "
                     + qd.clip(row.get("title") or "", 60))

    def action_fix_issue(self) -> None:
        """`f` takes the issue the selected row is about, its own or its item's.

        THROUGH `work_action`, like `p` and like a click. It did not, and that made
        the keyboard the one route on this dashboard where the icon and the act
        could disagree: a row whose ⚒ was grey — another repo, already held, or a
        question a person owes an answer about — still started a session on `f`.
        The whole argument for asking once is that an icon which looks live and
        then refuses is unreachable; a second caller deciding for itself puts it
        straight back, out of sight of the thing that draws it.
        """
        row = self.selected_work()
        if not row:
            return
        if self.work_action(row)[1] != "fix":
            self.say("nothing to take here — " + (
                qd.blocker_detail(row["blocked"]) if row.get("blocked")
                else qd.clip(row.get("title") or "", 60)))
            return
        if row.get("item"):
            self.fix_plan_item(row["item"])
            return
        issue = self.work_issue(row)
        if issue:
            self.fix_issue(issue)

    def action_help(self) -> None:
        self.say("o open the selected row on GitHub · p panel-review it · f take "
                 "its issue · w only what a person owes an answer about · b the "
                 "backlog nothing is waiting on · d the board's "
                 "dials page · z this pane full screen and back · s this project's "
                 "rows or the whole fleet's · r refresh · q quit · click ⚖ to "
                 "review, ⚒ to fix, ✎ to set or clear a dial (ctrl+s saves, ctrl+x "
                 "clears), a work row for why it is where it is, a seat to jump to "
                 "its pane, ✕ to close one")

    def selected_row(self, table_id: str) -> dict | None:
        table = self.query_one(table_id, DataTable)
        if not table.row_count:
            return None
        row = table.coordinate_to_cell_key(table.cursor_coordinate).row_key
        return self.rows.get(str(row.value))

    def selected_work(self) -> dict | None:
        """The WORK row under the cursor — the one row every key below acts on.

        `o`, `p` and `f` each used to ask a different table and guess which one you
        meant from what had focus. There is one table now, so they ask it.
        """
        return self.selected_row("#work")

    def selected_pr(self) -> dict | None:
        """`{repo, number}` for the selected row, if it is about a PR."""
        row = self.selected_work()
        return self.work_pr(row) if row else None

    def action_open_pr(self) -> None:
        """`o` opens the selected row on GitHub — the PR, or the issue."""
        row = self.selected_work()
        if not row:
            return
        pr = row.get("pr")
        if pr:
            self.open_pr(pr)
        elif row.get("issue"):
            self.open_issue(row["issue"])
        elif self.work_pr(row):
            self.open_pr(self.work_pr(row))
        else:
            issue = self.work_issue(row)
            if issue:
                self.open_issue(issue)


def main(argv: list[str] | None = None) -> int:
    """The flags, which are the same two the plain renderer takes.

    Both renderers are launched by the same `qb-dash` wrapper and put in a pane by
    the same `QB_SEATS_DASH`, so a screen that can be pointed at a project one way
    has to be pointable the other way too. Everything else about this app is keys.
    """
    ap = argparse.ArgumentParser(prog="qb-dash-tui",
                                 description="the fleet dashboard, clickable")
    ap.add_argument("--scope", choices=("repo", "all"), default=None,
                    help="repo (default): only this screen's repos, and no repo column; "
                         "all: every repo the board knows, in AGENTS and in the plan "
                         "half of WORK — PRs and issues stay the watched repos' "
                         "either way. `s` toggles it live; QB_DASH_SCOPE sets the "
                         "opening view")
    ap.add_argument("--repo", action="append", metavar="PATH|OWNER/NAME",
                    help="the project this screen is for — a checkout, which also "
                         "becomes where the ⚒ and ⚖ start work, or an owner/name "
                         "slug, which only filters. Repeatable; overrides "
                         "QB_DASH_REPOS, QB_DASH_REPO and the cwd")
    args = ap.parse_args(argv)
    # Two answers out of one flag, and BOTH are needed. `set_repos` redirects what
    # the panels draw (resolve_repos is cached, and the plan's ordering and the `gh`
    # calls ask it directly). `repo` redirects where the ⚒ and ⚖ RUN — a checkout
    # can move that and a slug cannot, and getting only the first half meant the
    # rows of the named repo were drawn as takeable and then refused one by one.
    repo = None
    if args.repo:
        try:
            targets = [qd.repo_target(r) for r in args.repo]
        except ValueError as exc:
            print(f"qb-dash-tui: --repo {exc}", file=sys.stderr)
            return 2
        qd.set_repos([slug for slug, _ in targets])
        # The first checkout named wins, because the launchers take one directory
        # and `--repo` is repeatable: a screen watching three repos still runs its
        # work somewhere, and the first one asked for is the one to mean it about.
        repo = next((path for _, path in targets if path), None)
    scope = qd.resolve_scope(on=None if args.scope is None else args.scope == "repo")
    Dash(scope=scope, repo=repo).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
