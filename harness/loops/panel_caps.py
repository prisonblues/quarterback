"""Caps — the round ceiling and the spend ceiling, as policy the panel obeys (#55).

`--max-rounds` has always existed and has never bound anything. It is *"the
caller's cap"*, resolved from whichever of the CLI flag and the rules file spoke
last, and the loop it caps is driven by a markdown file a person reads. Unattended
there is no such reader, and every other piece of #52 makes the countdown shorter:
a watcher on `synchronize` closes the loop (push → review → fix → push), and a
busy repo with a dozen open PRs needs no loop at all to run up a bill — one
release-merge review on this repo came to about $750, which is why
`skip_title_patterns` exists.

So this module is the half that was missing. Not a second pacing mechanism —
`qb-pace` already reads the shared subscription's five-hour and weekly windows and
`qb-start` already gates a spawn on it — but the *policy*: a number a person sets,
board-side, that the thing doing the spending enforces on itself.

## What it enforces, and against what

Two ceilings, and they answer different questions.

**The round ceiling** bounds one cycle. `review_panel.max_rounds` already exists,
already has a board dial, and is already reported per round. What changes here is
that a value the BOARD stated becomes a ceiling rather than a default: below it,
`--max-rounds` and the repo's own file still say whatever they like; above it,
they do not. See :func:`round_ceiling`.

**The spend ceiling** bounds everything else, and it is checked against a
measurement rather than against a round number. Five dials, all `None` by default
(:data:`CEILINGS`), read against `GET /review/spend`. A caller that renumbers its
rounds escapes the round ceiling and does not escape this one: a run is a row on
the board whatever it called itself, which is why `runs_per_pr` is here beside
`tokens_per_pr`.

## What spend is measured in

**Tokens, input + output** — `/review/stats`' own `billable`, for its reasons.
#55 named per-reviewer token capture (#15) as the blocker and settled meanwhile
for a crude "reviews per day" proxy. #15 landed both halves: `review_reviewers`
carries `input_tokens`/`output_tokens`/`cached_input_tokens`/`reasoning_tokens`
and `panel_seats._usage` emits them only where the vendor stated them. So the
honest unit is available and is what the ceiling is denominated in.

**And runs, which are not the proxy's leftovers.** A seat nobody instrumented
(`antigravity`), a run recorded before v2.14, a vendor that states nothing — each
contributes real spend and no measured tokens, so a token-only ceiling reads an
unmeasured spend as no spend. `GET /review/spend` reports `rows` against
`measured_rows` for exactly this, and a verdict computed over partial coverage
SAYS SO rather than quietly under-counting. The run ceilings are the floor under
that: a row is a row.

**Never derived from a price table.** #15's rule, and it holds here — `cost_usd`
is reported where a vendor stated it and is not a ceiling unit, because a ceiling
in dollars that this code computed would be wrong the moment a price moved and
would still read like a measurement.

## Why it cannot be raised from inside the repo being reviewed

The ceiling is a board dial. Three things make that a real answer rather than a
place to keep a number:

1. **`POST /dials` takes `app.auth.human`** — `Remote-User` plus the edge secret.
   A machine token is refused 403, and every agent on a box holds the machine
   token. Anything running while a branch under review is checked out — a test, a
   build step, a git hook — cannot move it.
2. **The dial layer is applied last**, after `.harness-rules.sample` and after the
   per-box overlay, so what the board says is what is in force.
3. **The per-box overlay is not read at all on the unattended path**, and the
   sample is read from `origin/<default branch>` rather than from the branch under
   review — `harness_rules`' two-ref rule, which exists so a poisoned PR cannot
   rewrite the rules governing its own review.

And on this side of the wire, `--force` does not override a cap. It overrides the
pre-flight's size verdict, which is this host deciding what its own seats can
read; a fleet ceiling that a local flag could switch off would be advice again.

## What it does when it cannot check

A ceiling this run could not verify is not the same as no ceiling, and the two
answers differ by whether anybody is watching:

* **Attended** — proceed, and say in the report that the ceiling was unverified.
  Refusing would break the case #59 exists to protect: `/panel` on a laptop with
  no board, no network and no `qb` still reviews a PR, and it always has. The
  round ceiling still binds, because it needs no board read.
* **Unattended** — refuse. `qb-start` already reasons this way about `qb-pace`
  (*"a spawn proceeds only on a definite go; anything non-zero means the gate did
  not run, not that it passed"*), and a governor that cannot read its input must
  not report clear (#244). An unattended run that treats an unreachable board as
  headroom is a ceiling anybody can remove by unplugging a cable.

This is the decision #59 asks #55 to make explicitly rather than inherit, and it
is made here.

## What it does NOT do: reserve

The check is a read and the dispatch that follows it is a separate act, so two
panels starting in the same second both see the same headroom and both proceed.
The overshoot is bounded — at most one round per concurrent run — and it is a
stated property rather than an oversight, raised by the codex second opinion on
the change that introduced it.

Closing it would mean a **reservation**: the board holding a claim on part of a
budget for the duration of a run. That is a different mechanism with a failure
mode of its own — a reservation that leaks parks a repo until somebody notices,
which is the shape `qb-pace`'s docstring refuses when it says a `hold` is a wait
and not a stop, and which #45 refuses for worktree ownership in the same words. A
ceiling whose whole purpose is to stop an unattended fleet running away must not
introduce a way to stop an attended one that is behaving.

So the bound is the honest answer for now: a ceiling that can be exceeded by one
round per concurrent panel is still a ceiling, and it is several orders of
magnitude tighter than the ceiling that existed before this, which was none.

**None of this fires until a person sets a number.** Every ceiling defaults to
`None`; with all five unset the panel makes no board call at all
(:func:`Budget.dormant`) and behaves exactly as it did before this module existed.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

from harness_rules import (
    BOARD_TIMEOUT, RULES_FILENAME, board_config, ssl_context, unattended,
)

#: The five spend ceilings, mapped to the window each is measured over. The key is
#: the name under `review_panel.budget`; the value is `(which window in the
#: `GET /review/spend` body, which unit)`.
#:
#: NOT a second statement of what they default to — `harness_rules.DEFAULTS` is
#: still the only place that is written down, and every one of them is `None`
#: there. This says which measurement each one is checked against, which is the
#: part `DEFAULTS` cannot say.
CEILINGS: dict[str, tuple[str, str]] = {
    "tokens_per_day": ("repo_window", "tokens"),
    "runs_per_day": ("repo_window", "runs"),
    "tokens_per_pr": ("pr_total", "tokens"),
    "runs_per_pr": ("pr_total", "runs"),
    "fleet_tokens_per_day": ("fleet_window", "tokens"),
}

#: The RUN ceiling that measures the same window as a token ceiling, where there is
#: one. Named rather than derived from the key, because `fleet_tokens_per_day` has no
#: sibling and a string transform would confidently invent `fleet_runs_per_day` — a
#: dial nothing reads, offered as the remedy to somebody whose ceiling has stopped
#: binding.
RUN_SIBLING = {"tokens_per_day": "runs_per_day", "tokens_per_pr": "runs_per_pr"}

#: How the two units are spelled in a refusal. A ceiling that says "3 over 2" and
#: does not say what of is a ceiling nobody can act on.
UNIT_NOUN = {"tokens": "tokens", "runs": "recorded review runs"}

#: The board path this module reads. Its ceiling lives in `dials`; this is the
#: measurement the ceiling is checked against, and the two are deliberately
#: different endpoints — the board does not know what a dial means.
SPEND_PATH = "review/spend"

#: How long the spend read waits. `DIALS_TIMEOUT`'s reasoning rather than
#: `BOARD_TIMEOUT`'s: this runs before every round, and a slow board must cost a
#: review a few seconds and a sentence, never the review.
SPEND_TIMEOUT = 5

#: Set to anything — including empty — to answer the spend read from the
#: environment instead of the network, the way `QUARTERBACK_DIALS` answers the
#: dial read. Empty means "no board", which is how a test exercises the offline
#: path without a server and how a box is taken off the wire deliberately.
SPEND_ENV = "QUARTERBACK_REVIEW_SPEND"

#: The largest window `GET /review/spend` accepts, mirrored so a junk
#: `budget_window_hours` is refused here with a sentence rather than as a 422 from
#: a request the operator never sees. Kept equal to `app.api.reviews`'
#: `MAX_SPEND_WINDOW_HOURS` by `tests/test_review_spend.py`.
MAX_WINDOW_HOURS = 24 * 28

#: The window a `budget_window_hours` that cannot be read falls back to. The same
#: number as `DEFAULTS`, and it is spelled here only so the refusal path has
#: something to name; the value in force still comes from the resolved rules.
DEFAULT_WINDOW_HOURS = 24


def _refuse(key: str, value, accepted: str) -> None:
    """`panel_seats._refuse_value`'s mechanism and sentence, for this module's keys.

    Not imported from there: `panel_seats` is the seat runner and this module is
    read by `panel.py` before it, so the dependency would run the wrong way for
    the sake of five lines. The shape is what matters and it is pinned by test.
    """
    raise SystemExit(
        f"{RULES_FILENAME}: `review_panel.{key}`={value!r} is not {accepted} — "
        "fix the value, or remove the key to take the default. (An unknown KEY is "
        "warned about and dropped, because it may be a setting only a newer harness "
        "knows; a known key this harness cannot read is a typo, and applying the "
        "default anyway would run the review under a policy the file did not ask for.)")


def _positive_int(key: str, raw, accepted: str) -> int | None:
    """A whole number >= 0, or `None` for absent. Bools refused before the int read.

    `resolve_max_rounds`' reader, and its reasons: `2.0` out of a JSON generator is
    two, `2.5` is not a number of anything this counts, and `True` is `1` to Python
    — a ceiling of one run, set by a value that says nothing about runs, on the
    switch that decides whether a review happens at all.
    """
    if raw is None or raw == "":
        return None
    n = None
    if isinstance(raw, bool):
        n = None
    elif isinstance(raw, int):
        n = raw
    elif isinstance(raw, float) and raw.is_integer():
        n = int(raw)
    elif isinstance(raw, str):
        try:
            n = int(raw.strip())
        except ValueError:
            n = None
    if n is None or n < 0:
        _refuse(key, raw, accepted)
    return n


def round_ceiling(cfg: dict) -> tuple[int | None, dict | None]:
    """`(the board's round cap, the dial entry that stated it)`, or `(None, None)`.

    **The layer is the answer, not just the value.** `review_panel.max_rounds` has
    four layers and only one of them is a ceiling: `DEFAULTS` and the repo's own
    file are what this repo would LIKE, and the board is what a person decided.
    Under `apply_dials` the board's value is already the one in force — what this
    adds is that a caller cannot then step over it with `--max-rounds`.

    Read off `_dials`, which `resolve_repo` stamps with the layer that answered
    every dial, rather than by comparing values: a board dial set to exactly the
    sample's number still came from the board, and a ceiling that vanished
    whenever somebody wrote it down twice would be a ceiling nobody could trust.
    """
    said = (cfg.get("_dials") or {}).get("review_panel.max_rounds")
    if not isinstance(said, dict) or said.get("layer") != "board":
        return None, None
    value = said.get("value")
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        # A board dial this harness could not apply never reaches `_dials` as a
        # board layer — `_dial_problem` refuses it and `board_dials` names it — so
        # this is the belt on top of that brace, and it fails OPEN rather than
        # inventing a ceiling out of a value nothing validated.
        return None, None
    return value, said


@dataclass(frozen=True)
class Budget:
    """The spend ceilings in force for one repo, already validated.

    `window_hours` is what "per day" means here. Resolved beside the ceilings and
    not separately, because halving the window halves every rolling ceiling and a
    caller that could read one without the other would be reading a number whose
    meaning it could not see.
    """

    #: `{key: ceiling}` for every one of :data:`CEILINGS` a layer actually set.
    #: A key absent from this map has no ceiling — which is every key, until a
    #: person writes one.
    limits: dict[str, int] = field(default_factory=dict)
    window_hours: int = DEFAULT_WINDOW_HOURS

    @property
    def dormant(self) -> bool:
        """No ceiling anywhere, so nothing to measure and no board call to make.

        The load-bearing property of this whole change: on a fleet that installs
        the release and sets nothing, this is `True`, `check` returns at once, and
        the panel spends exactly what it spent before.
        """
        return not self.limits

    @property
    def windows(self) -> set[str]:
        """Which of the three `GET /review/spend` windows this budget actually needs."""
        return {CEILINGS[k][0] for k in self.limits}


def resolve_budget(panel: dict, notes: list[str]) -> Budget:
    """Read and validate `review_panel.budget` plus `review_panel.budget_window_hours`.

    A malformed value is a hard refusal, not a fallback to "no ceiling":
    `harness_rules`' standing asymmetry is that an unknown NAME is warned about and
    dropped because it may be a setting only a newer harness knows, while a known
    key this harness cannot read is a typo — and taking the default on a typo'd
    ceiling means running unbounded on a file that asked for a bound.

    A ceiling of `0` is accepted and means *nothing may be spent*. It is not the
    same as absent and must not be folded into it: "this repo is stopped" is a
    thing an operator may want to say, and `null` (clear the dial) is how they say
    the other one.
    """
    raw = panel.get("budget")
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        _refuse("budget", raw, "a JSON object of ceilings, e.g. "
                               '{"tokens_per_day": 4000000}')
    limits: dict[str, int] = {}
    for key in CEILINGS:
        value = _positive_int(f"budget.{key}", raw.get(key),
                              "a whole number >= 0, or null for no ceiling")
        if value is not None:
            limits[key] = value
    hours = _positive_int("budget_window_hours", panel.get("budget_window_hours"),
                          f"a whole number of hours between 1 and {MAX_WINDOW_HOURS}")
    if hours is None:
        hours = DEFAULT_WINDOW_HOURS
    if not 1 <= hours <= MAX_WINDOW_HOURS:
        _refuse("budget_window_hours", panel.get("budget_window_hours"),
                f"a whole number of hours between 1 and {MAX_WINDOW_HOURS}")
    budget = Budget(limits=limits, window_hours=hours)
    if not budget.dormant:
        # Said on every round that runs under one, in the list `--post` publishes to
        # the PR. #52's "never silent" cuts both ways: a run that was NOT stopped
        # still ran under a ceiling, and a reader six weeks later has the report.
        notes.append(
            "spend ceiling in force — " + ", ".join(
                f"{key.replace('_', ' ')} {limit:,}"
                for key, limit in sorted(budget.limits.items()))
            + f" (rolling window {budget.window_hours}h)")
    return budget


def fetch_spend(github: str, pr: int | None, hours: int) -> tuple[dict | None, str]:
    """`(what review has already cost, "")`, or `(None, why not)`.

    `$QUARTERBACK_REVIEW_SPEND` short-circuits the read exactly as
    `$QUARTERBACK_DIALS` short-circuits the dial read, and for the same two
    reasons: a test needs the body without a server, and a box needs a way to say
    "there is no board" that is not indistinguishable from a broken one.

    An unreadable board is reported as a SENTENCE and never as an empty answer. A
    caller that got `{}` here would compare every ceiling against zero spend and
    proceed — a governor reporting clear on an input it never read, which is the
    failure #244 names and the one this endpoint exists to avoid.
    """
    raw = os.environ.get(SPEND_ENV)
    if raw is not None:
        if not raw.strip():
            return None, "no board is configured for this box"
        try:
            body = json.loads(raw)
        except json.JSONDecodeError as e:
            return None, f"${SPEND_ENV} is not valid JSON: {e}"
        if not isinstance(body, dict):
            return None, f"${SPEND_ENV} must hold a JSON object"
        return body, ""

    url, token, why = board_config()
    if why:
        return None, why
    query = f"?hours={int(hours)}&repo={urllib.parse.quote(github, safe='')}"
    if pr is not None:
        query += f"&pr={int(pr)}"
    where = f"{url}/{SPEND_PATH}{query}"
    req = urllib.request.Request(where, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=SPEND_TIMEOUT,
                                    context=ssl_context()) as resp:
            return json.loads(resp.read().decode()), ""
    except urllib.error.HTTPError as e:
        if e.code == 404:
            # A board older than this endpoint. A capability answer, like the dial
            # layer's own 404 — but NOT a pass: the caller decides what an
            # unverifiable ceiling means, and it decides differently attended and
            # unattended.
            return None, (f"this board has no {SPEND_PATH} endpoint (404) — it "
                          f"predates the spend ceiling")
        if e.code == 401:
            return None, (f"the board refused this box's token (401) reading "
                          f"{SPEND_PATH}")
        return None, f"{SPEND_PATH} answered {e.code}"
    except (OSError, ValueError) as e:
        # OSError covers a timeout, a reset and a dropped route; ValueError is a
        # body that is not JSON. `BOARD_TIMEOUT` is named so the sentence can say
        # what was waited for without a second constant going stale.
        return None, (f"the board could not be reached for {SPEND_PATH} ({e}; "
                      f"waited {SPEND_TIMEOUT}s of a {BOARD_TIMEOUT}s budget)")


def _window(spend: dict, name: str) -> dict | None:
    got = spend.get(name)
    return got if isinstance(got, dict) else None


def _measured(window: dict) -> str:
    """How much of a window's spend was actually instrumented, as a clause or `""`.

    Read from `rows` against `measured_rows`, which `GET /review/spend` reports for
    this. A ceiling checked against a partially-measured window is checked against
    an UNDERCOUNT, and the honest thing is to say so in the same breath as the
    number rather than to leave a reader to assume the sum was complete.
    """
    rows, measured = window.get("rows") or 0, window.get("measured_rows") or 0
    if not rows or measured >= rows:
        return ""
    return (f" (measured over {measured} of {rows} reviewer runs — the real spend "
            f"is higher)")


@dataclass(frozen=True)
class Verdict:
    """What the caps say about starting this round.

    `refusal` is the sentence, and it is empty when the round may run. It is
    handed to `panel_preflight.preflight` as its `gate`, so a cap refusal travels
    the path a pre-flight refusal already travels — printed, recorded on the board
    with `reviewed: false` and a `skip_reason`, posted to the PR under `--post`,
    and with a `ran: false` row per seat so the board cannot read the refusal as a
    panel that found nothing. #55's second acceptance criterion is that a budget
    stop must not look like a clean review, and that machinery is what makes it
    not.
    """

    refusal: str = ""

    @property
    def stop(self) -> bool:
        return bool(self.refusal)


def check(cfg: dict, panel: dict, pr: int | None, notes: list[str],
          *, headless: bool | None = None, budget: Budget | None = None) -> Verdict:
    """The spend ceiling, checked BEFORE any seat is dispatched.

    `notes` is appended to in place, the way every other resolver in the panel
    reports itself, and the returned `Verdict` carries the refusal separately
    because the caller has to route that one somewhere else.

    `budget` is an already-:func:`resolve_budget`-ed answer, for the caller that
    wanted the VALIDATION earlier than the board read — `panel.run` does, because a
    typo'd ceiling should die beside the other bad-dial refusals and before an API
    call, while the read that checks it should wait until the PR is known to be one
    the panel would review at all. Left out, this resolves for itself, which is what
    a test and a one-shot caller want.

    `headless` overrides `harness_rules.unattended()`, for a test and for a caller
    that already knows. It decides one thing and it is the decision #59 asked #55
    to make: what an unverifiable ceiling means. See the module docstring.
    """
    if budget is None:
        budget = resolve_budget(panel, notes)
    if budget.dormant:
        # The whole of the "changes nothing until a number is set" claim. No board
        # call, no measurement, no verdict — this is the line a fleet installing
        # the release runs, and it is indistinguishable from the release before it.
        return Verdict()

    github = cfg.get("github") or ""
    headless = unattended() if headless is None else headless
    spend, why = fetch_spend(github, pr, budget.window_hours)
    if spend is None:
        return _unverified(
            [f"the board could not be read: {why}"], github, notes, headless)

    over: list[str] = []
    near: list[str] = []
    #: Ceilings that are SET and could not be evaluated. Kept as a list rather than
    #: turned into a note at the point of discovery, because "could not be checked"
    #: has to reach the same fork as "the board could not be read at all" — an
    #: unattended run must not spend against either. Codex found this: a board that
    #: answered with a window missing, or with a window nothing had instrumented,
    #: used to add a note and PROCEED, so a partial response silently removed the
    #: ceiling on exactly the runs nobody is watching.
    unverifiable: list[str] = []
    for key, limit in sorted(budget.limits.items()):
        window_name, unit = CEILINGS[key]
        where = window_name.replace("_", " ")
        window = _window(spend, window_name)
        if window is None:
            unverifiable.append(
                f"`budget.{key}` — the board returned no `{window_name}`")
            continue
        used = window.get(unit)
        if used is None:
            # Only ever the token units. Two very different cases hide behind one
            # null, and only the second is unverifiable:
            #
            #   `rows == 0` — nothing was reviewed in the window at all, so nothing
            #     was spent. A real zero, and treating it as unverifiable would
            #     refuse every unattended run on a quiet repo for ever.
            #   `rows > 0`  — runs happened and none of them was instrumented. The
            #     spend is non-zero and unknown, which is the case a token-only
            #     ceiling silently stops binding on.
            if window.get("rows"):
                sibling = RUN_SIBLING.get(key)
                unverifiable.append(
                    f"`budget.{key}` — none of the {window.get('rows')} reviewer runs "
                    f"on the {where} recorded any {UNIT_NOUN[unit]}, so what was "
                    f"spent is unknown rather than nothing"
                    + (f". `budget.{sibling}` is the ceiling that still binds a seat "
                       f"nobody instrumented" if sibling else ""))
                continue
            used = 0
        if used >= limit:
            over.append(f"{key.replace('_', ' ')}: {used:,} of {limit:,} "
                        f"{UNIT_NOUN[unit]} already spent on the {where}"
                        f"{_measured(window)}")
        elif limit and used >= limit * 0.8:
            near.append(f"{key.replace('_', ' ')}: {used:,} of {limit:,} "
                        f"{UNIT_NOUN[unit]} on the {where}{_measured(window)}")

    for line in near:
        notes.append(f"spend ceiling nearly reached — {line}")
    # A ceiling that WAS reached outranks one that could not be read: the answer is
    # known, it is "stop", and reporting the softer refusal would send an operator
    # to fix a board that was working.
    if not over and unverifiable:
        return _unverified(unverifiable, github, notes, headless)
    if not over:
        return Verdict()
    return Verdict(
        refusal=("the repo's spend ceiling is reached — " + "; ".join(over) +
                 ". No seat was dispatched and nothing was reviewed. The ceiling is "
                 "fleet policy on the board (`review_panel.budget`, "
                 "`POST /dials`), so it cannot be raised from inside this repo and "
                 "`--force` does not override it"))


def _unverified(why: list[str], github: str, notes: list[str],
                headless: bool) -> Verdict:
    """One fork for every "a ceiling is set and this run could not check it".

    #59 asked #55 to decide this explicitly rather than inherit it, and the
    decision turns on whether anybody is watching — see the module docstring. The
    two answers share a function so they cannot drift, and so that a THIRD way of
    failing to check (a window missing, a window nothing instrumented) reaches the
    same fork as the first two rather than quietly proceeding.
    """
    said = ("spend ceiling UNVERIFIED — " + "; ".join(why) +
            f". This run cannot say what {github or 'this repo'} has already spent")
    if headless:
        notes.append(said)
        return Verdict(
            refusal=said + ", and an unattended run does not spend against a ceiling "
                           "it could not read (#244). Restore the board, set a "
                           "ceiling this box can measure, or clear it deliberately.")
    notes.append(said + " — reviewing anyway, because the local path stays "
                        "first-class (#59). The round cap still binds.")
    return Verdict()


def enabled_refusal(cfg: dict) -> str:
    """Why this repo's reviews are switched off, or `""`.

    #55's fourth acceptance criterion — *"turning the watcher off for a repo takes
    one setting, and takes effect on the next claim rather than the next restart"*.
    A dial IS that, because `resolve_repo` reads them on every run: one
    `POST /dials {"dial": "enabled", "value": false, "repo": …}` and the next claim
    resolves to off, with no restart and no deploy.

    `lander.py` has honoured the top-level `enabled` since it existed and the panel
    never has, so a repo that switched itself off still got reviewed. The dial is
    `narrow` — the board may turn a repo off and may not turn one back on over the
    top of a file that said no.
    """
    if cfg.get("enabled") is not False:
        return ""
    who = cfg.get("name") or "this repo"
    said = (cfg.get("_dials") or {}).get("enabled") or {}
    if said.get("layer") == "board":
        by, reason = said.get("set_by") or "a person", said.get("reason") or ""
        lapses = said.get("expires_at")
        return (f"{who}'s reviews are switched off on the board — `enabled: false`, "
                f"set by {by}"
                + (f' because "{reason}"' if reason else "")
                + (f", until {lapses}" if lapses else "")
                + ". Clear the dial to turn them back on. No panel ran.")
    return (f"{who} has `enabled: false` in its rules — the loops skip this repo. "
            f"No panel ran.")


__all__ = [
    "Budget", "CEILINGS", "DEFAULT_WINDOW_HOURS", "MAX_WINDOW_HOURS", "RUN_SIBLING",
    "SPEND_ENV",
    "SPEND_PATH", "SPEND_TIMEOUT", "UNIT_NOUN", "Verdict", "check",
    "enabled_refusal", "fetch_spend", "resolve_budget", "round_ceiling",
]
