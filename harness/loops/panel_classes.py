"""#836's class ledger — review rounds that converge.

A finding names a LOCATION; a class names the defect every one of those locations
shares ("resolver call without the viewer", "token read separately from the value it
guards"). Lex-Ray v22 ran 107 Codex rounds, and every loop that did not converge ended
the same way: somebody noticed, at round 3 to 5 and by hand, that the findings were all
one class, and the fix moved from patching each path to making the class impossible by
construction. This module makes that step mechanical.

Each class records three things beside its sightings:

* the **invariant** that rules it out;
* the **enforcement** that makes the invariant hold (a required argument, one gate, a
  type, a DB constraint);
* the **guard test** that keeps it holding (a static walk, a property test).

Three rules are built on it:

1. **Closed-ended rounds.** Rounds after the first are handed the ledger
   (:func:`ledger_brief`). A reviewer verifies each recorded invariant holds everywhere
   and may report only a violation of one or a genuinely NEW class — a new instance of a
   recorded class is a violation, not a new finding.
2. **Two strikes.** The sighting of a class on its second distinct path means the next
   fix must make the class impossible by construction (:func:`record` returns
   ``owed``, and the CLI exits :data:`STRUCTURAL_OWED_EXIT`, 5). "Fix this path too" is
   refused; the fix is accepted once it is recorded with ``--structural``. A sighting
   on a path the structural fix did not close re-opens the debt — the "impossible"
   claim was false.
3. **A budget measured in convergence.** A round converges when it records no new
   class. After ``class_ledger.rounds`` rounds without that, the next round is refused
   (:func:`budget_refusal`) and the owner decides between the structural options
   :func:`report` lists. No round 4 by default.

The ledger is one JSON file per PR, beside the premise register, written only by this
module. Ledgers from the other PRs of a wave can be read alongside it (``--wave``),
because v22's classes recurred across PRs.

Usage:
    panel_classes.py record --ledger L --round N --class NAME --path WHERE
        [--invariant ..] [--enforcement ..] [--guard-test ..] [--finding KEY]
    panel_classes.py structural --ledger L --round N --class NAME|KEY --how ..
        [--enforcement ..] [--guard-test ..]
    panel_classes.py status --ledger L --round N
    panel_classes.py brief --ledger L [--wave OTHER ...]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

import harness_rules
from panel_core import load_repo_cfg, write_payload
from panel_rounds import _norm_title, same_premise

LEDGER_VERSION = 1

#: Exit code of `record` when the class is owed a structural fix, and of a round that
#: will not start while one is owed. Distinct from :data:`BUDGET_EXIT` because the
#: remedies differ: this one the builder answers (record the structural fix), that one
#: only the owner does. Not 3, which is a ledger that could not be written.
STRUCTURAL_OWED_EXIT = 5

#: Exit code when the round budget is spent without convergence: stop, owner decides.
#: The same number as `panel_rounds.PREMISE_REPEATED_EXIT`, for the same meaning.
BUDGET_EXIT = 4

CLASS_LEDGER_DEFAULTS = harness_rules.DEFAULTS["review_panel"]["class_ledger"]

#: What :func:`report` offers when a class has no enforcement recorded yet. These are
#: the four shapes every v22 loop converged on, in the order they were reached for.
STRUCTURAL_SHAPES = ("a required argument the path cannot be called without",
                     "one gate every path goes through, with a static guard that no "
                     "path bypasses it",
                     "a type that cannot hold the bad state",
                     "a database constraint")

LEDGER_HEADING = "RECORDED DEFECT CLASSES — this round is a check against this list"


def class_key(name: str) -> str:
    """A stable identity for a class, from its name: :func:`panel_rounds.premise_key`'s
    recipe in its own namespace, so a class and a premise never collide."""
    norm = _norm_title(name)
    if not norm:
        return ""
    return hashlib.md5(f"class|{norm}".encode(),
                       usedforsecurity=False).hexdigest()[:16]


def _path_norm(path: str) -> str:
    return " ".join(str(path).split()).lower()


def new_ledger(repo: str = "", pr: int | None = None) -> dict:
    return {"version": LEDGER_VERSION, "repo": repo, "pr": pr, "classes": []}


def _round(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 1:
        return value
    return None


def _clean_entry(entry: object) -> dict | None:
    if not isinstance(entry, dict) or not str(entry.get("name") or "").strip():
        return None
    sightings = []
    for s in entry.get("sightings") or []:
        if not isinstance(s, dict) or _round(s.get("round")) is None:
            continue
        if not str(s.get("path") or "").strip():
            continue
        sightings.append({"round": s["round"], "path": str(s["path"]).strip(),
                          "finding": str(s.get("finding") or "")})
    if not sightings:
        return None
    structural = entry.get("structural")
    if not (isinstance(structural, dict) and str(structural.get("how") or "").strip()
            and _round(structural.get("round")) is not None):
        structural = None
    name = " ".join(str(entry["name"]).split())
    return {"key": class_key(name), "name": name,
            "invariant": str(entry.get("invariant") or ""),
            "enforcement": str(entry.get("enforcement") or ""),
            "guard_test": str(entry.get("guard_test") or ""),
            "sightings": sightings, "structural": structural}


def load_ledger(path: str, repo: str = "", pr: int | None = None
                ) -> tuple[dict, list[str]]:
    """The PR's ledger, and everything wrong with it.

    `load_premises`'s rules: a MISSING file is an empty ledger and no problem (the
    first record creates it); anything unreadable, or belonging to another repo or PR,
    is REPORTED and read as empty rather than guessed at."""
    problems: list[str] = []
    ledger = new_ledger(repo, pr)
    if not path:
        return ledger, problems
    try:
        raw = json.loads(Path(path).read_text())
    except FileNotFoundError:
        return ledger, problems
    except (OSError, ValueError) as e:
        problems.append(f"class ledger {path} could not be read ({e.__class__.__name__})"
                        " — read as EMPTY, so no recorded invariant is checked and every"
                        " class counts as new")
        return ledger, problems
    if not isinstance(raw, dict) or not isinstance(raw.get("classes"), list):
        problems.append(f"class ledger {path} is not a class ledger (no `classes` list)"
                        " — read as EMPTY")
        return ledger, problems
    was_repo, was_pr = str(raw.get("repo") or ""), raw.get("pr")
    if repo and was_repo and was_repo != repo:
        problems.append(f"class ledger {path} belongs to {was_repo}, not {repo} — read "
                        "as EMPTY; pass it with --wave to read another PR's classes")
        return ledger, problems
    if pr is not None and isinstance(was_pr, int) and was_pr != pr:
        problems.append(f"class ledger {path} belongs to PR #{was_pr}, not #{pr} — read "
                        "as EMPTY; pass it with --wave to read another PR's classes")
        return ledger, problems
    for entry in raw["classes"]:
        clean = _clean_entry(entry)
        if clean is None:
            problems.append(f"class ledger {path} carries an entry with no name or no "
                            "sighting — it was NOT counted")
            continue
        ledger["classes"].append(clean)
    ledger["repo"], ledger["pr"] = repo or was_repo, pr if pr is not None else was_pr
    return ledger, problems


def load_wave(paths: list[str]) -> tuple[list[dict], list[str]]:
    """The classes other PRs of the same wave recorded, read-only, each tagged with
    the PR it came from. A class recurring across PRs (v22's race in #4462 and again
    in #4438/#4460) is the same class, and the reviewer should be told so."""
    out: list[dict] = []
    problems: list[str] = []
    for path in paths:
        ledger, why = load_ledger(path)
        problems.extend(why)
        for entry in ledger["classes"]:
            out.append({**entry, "pr": ledger.get("pr")})
    return out, problems


def find_class(ledger: dict, name: str) -> dict | None:
    """The ledger's entry for ``name`` — by key, exact name, or a restatement of it."""
    key = class_key(name)
    wanted = str(name).strip().lower()
    norm = _norm_title(name)
    for entry in ledger.get("classes") or []:
        if wanted == entry["key"] or entry["key"] == key:
            return entry
    for entry in ledger.get("classes") or []:
        if norm and same_premise(norm, _norm_title(entry["name"])):
            return entry
    return None


def strikes(entry: dict) -> int:
    """How many distinct PATHS this class has been seen on. The same path seen again
    in a later round is a fix that did not hold, not a second strike."""
    return len({_path_norm(s["path"]) for s in entry["sightings"]})


def is_owed(entry: dict, limit: int | None) -> bool:
    """Is the next fix for this class required to be structural?

    Owed from the ``limit``-th distinct path until a structural fix is recorded, and
    owed AGAIN when a sighting is recorded after that fix on a path not seen before
    it: a class that was "impossible by construction" and then turned up was not."""
    if limit is None or strikes(entry) < limit:
        return False
    fix = entry.get("structural")
    if not fix:
        return True
    before = {_path_norm(s["path"]) for s in entry["sightings"]
              if s["round"] <= fix["round"]}
    return any(s["round"] > fix["round"] and _path_norm(s["path"]) not in before
               for s in entry["sightings"])


def record(ledger: dict, name: str, round_no: int, path: str, *,
           finding: str = "", invariant: str = "", enforcement: str = "",
           guard_test: str = "", limit: int | None = 2) -> dict:
    """Record one sighting of a class, creating the class on its first, and say
    whether the fix about to be written for it may be a per-path patch.

    The sighting is recorded whether or not the class is owed a structural fix:
    `declare_premise`'s rule, because the count is what the next decision reads."""
    name = " ".join(str(name).split())
    path = str(path).strip()
    if not name or not class_key(name):
        raise ValueError("a class needs a name — what every instance of it has in "
                         "common, not where this one is")
    if not path:
        raise ValueError("a sighting needs a path — the file:line, or the route that "
                         "reaches it, this instance was found on")
    if _round(round_no) is None:
        raise ValueError("rounds are numbered from 1")
    entry = find_class(ledger, name)
    new = entry is None
    if new:
        if not invariant.strip():
            raise ValueError(f"a new class needs its invariant — the property that, "
                             f"held everywhere, rules out {name!r}")
        entry = {"key": class_key(name), "name": name, "invariant": "",
                 "enforcement": "", "guard_test": "", "sightings": [],
                 "structural": None}
        ledger.setdefault("classes", []).append(entry)
    for field, value in (("invariant", invariant), ("enforcement", enforcement),
                         ("guard_test", guard_test)):
        if value.strip():
            entry[field] = " ".join(value.split())
    sighting = {"round": round_no, "path": path, "finding": str(finding or "")}
    if not any(s["round"] == round_no and _path_norm(s["path"]) == _path_norm(path)
               for s in entry["sightings"]):
        entry["sightings"].append(sighting)
    return {"key": entry["key"], "name": entry["name"], "new": new,
            "strikes": strikes(entry), "limit": limit,
            "owed": is_owed(entry, limit), "structural": bool(entry["structural"])}


def record_structural(ledger: dict, name: str, round_no: int, how: str, *,
                      enforcement: str = "", guard_test: str = "") -> dict:
    """Record that the fix for a class makes it impossible by construction.

    Refused without an enforcement and a guard test, because those two are what make
    "impossible" a claim the next round can check rather than a sentence."""
    entry = find_class(ledger, name)
    if entry is None:
        raise ValueError(f"no class {name!r} in the ledger — record a sighting first")
    if not how.strip():
        raise ValueError("--how: say how the fix makes the class impossible, in one "
                         "sentence")
    if _round(round_no) is None:
        raise ValueError("rounds are numbered from 1")
    if enforcement.strip():
        entry["enforcement"] = " ".join(enforcement.split())
    if guard_test.strip():
        entry["guard_test"] = " ".join(guard_test.split())
    missing = [f for f in ("enforcement", "guard_test") if not entry[f]]
    if missing:
        raise ValueError(f"a structural fix records its {' and its '.join(missing)}"
                         " — without them the next round has nothing to verify")
    entry["structural"] = {"how": " ".join(how.split()), "round": round_no}
    return {"key": entry["key"], "name": entry["name"], "structural": True}


def first_round(entry: dict) -> int:
    return min(s["round"] for s in entry["sightings"])


def last_round(ledger: dict) -> int:
    """The latest round any sighting was recorded against, 0 for an empty ledger."""
    return max((s["round"] for e in ledger.get("classes") or [] for s in e["sightings"]),
               default=0)


def round_state(ledger: dict, round_no: int, strikes_limit: int | None,
                rounds_limit: int | None) -> dict:
    """Where the cycle stands after ``round_no``'s classes were recorded.

    **Converged means the round recorded no NEW class.** Violations of recorded
    invariants are still work, but they are instances of a list the next round can
    check against — the round has stopped finding new kinds of thing, which is what
    makes the review bounded."""
    classes = ledger.get("classes") or []
    new = [e for e in classes if first_round(e) == round_no]
    seen = [e for e in classes if any(s["round"] == round_no for s in e["sightings"])]
    owed = [e for e in classes if is_owed(e, strikes_limit)]
    converged = not new
    return {"round": round_no, "classes": len(classes),
            "new": [e["key"] for e in new],
            "seen": [e["key"] for e in seen],
            "owed": [e["key"] for e in owed],
            "converged": converged,
            "strikes_limit": strikes_limit, "rounds_limit": rounds_limit,
            "over": bool(rounds_limit is not None and not converged
                         and round_no >= rounds_limit)}


def options(entry: dict) -> str:
    """The structural options for one open class, for the owner's decision."""
    if entry.get("enforcement"):
        return f"recorded enforcement: {entry['enforcement']}"
    return "no enforcement recorded yet — options: " + "; ".join(STRUCTURAL_SHAPES)


def _open_classes(ledger: dict, strikes_limit: int | None) -> list[dict]:
    return [e for e in ledger.get("classes") or []
            if not e.get("structural") or is_owed(e, strikes_limit)]


def budget_refusal(ledger: dict, round_no: int, strikes_limit: int | None,
                   rounds_limit: int | None) -> str:
    """Why round ``round_no`` must not run, or "" when it may.

    Refused when the round before it was at or past the budget without converging,
    and while any class is owed a structural fix (the previous fix pass patched a path
    where it should have closed the class, so another round can only find the next
    path)."""
    prev = round_no - 1
    if prev < 1 or not ledger.get("classes"):
        return ""
    state = round_state(ledger, prev, strikes_limit, rounds_limit)
    by_key = {e["key"]: e for e in ledger["classes"]}
    if state["over"]:
        lines = [f"class ledger: {rounds_limit} round(s) without convergence — round "
                 f"{prev} still recorded a new class. Stop: the owner decides. Open "
                 "classes and their structural options:"]
        for e in _open_classes(ledger, strikes_limit):
            lines.append(f"  - [{e['key']}] {e['name']} ({strikes(e)} path(s)): "
                         f"{options(e)}")
        lines.append("To go on anyway, raise `review_panel.class_ledger.rounds` (or pass"
                     " --class-rounds) — that is the owner's call, not the loop's.")
        return "\n".join(lines)
    if state["owed"]:
        lines = ["class ledger: a structural fix is owed and none is recorded — another "
                 "round would only find the next path. Record it first with "
                 "`panel_classes.py structural`:"]
        for key in state["owed"]:
            e = by_key[key]
            lines.append(f"  - [{e['key']}] {e['name']} ({strikes(e)} paths): "
                         f"{options(e)}")
        return "\n".join(lines)
    return ""


def _class_lines(entry: dict, origin: str = "") -> list[str]:
    paths = sorted({s["path"] for s in entry["sightings"]})
    shown = ", ".join(paths[:6]) + (f" (+{len(paths) - 6} more)" if len(paths) > 6 else "")
    lines = [f"- [class {entry['key']}] {entry['name']}{origin}",
             f"  invariant: {entry['invariant'] or '(not recorded)'}"]
    if entry.get("enforcement"):
        lines.append(f"  enforcement: {entry['enforcement']}")
    if entry.get("guard_test"):
        lines.append(f"  guard test: {entry['guard_test']}")
    lines.append(f"  seen at: {shown}")
    return lines


def ledger_brief(ledger: dict, wave: list[dict] | None = None) -> str:
    """The reviewer's closed list, or "" when there is nothing recorded — so a cycle
    with no ledger sends a prompt byte-identical to the one it always sent."""
    own = ledger.get("classes") or []
    keys = {e["key"] for e in own}
    others = [e for e in wave or [] if e["key"] not in keys]
    if not own and not others:
        return ""
    lines = [LEDGER_HEADING, "",
             "Earlier rounds classified their findings into the defect classes below. "
             "This round is a CHECK against that list, not an open hunt:",
             "(a) For each class, verify its invariant holds EVERYWHERE in the change — "
             "every call site, every path that reaches the guarded value. A place it "
             "does not hold is a finding whose title starts `[class <key>]`, and it is "
             "a violation even if the location is new.",
             "(b) Beyond that, report only a genuinely NEW class — a defect none of the "
             "invariants below would rule out. A new instance of a recorded class is a "
             "violation under (a), not a new finding.",
             "Anything else you notice is not this round's work.", ""]
    for e in own:
        lines.extend(_class_lines(e))
    if others:
        lines += ["", "Recorded on OTHER pull requests of the same wave — the same "
                  "invariants apply here:"]
        for e in others:
            lines.extend(_class_lines(e, f" (PR #{e.get('pr')})" if e.get("pr") else ""))
    return "\n".join(lines) + "\n\n"


def _limit(block: dict, key: str, minimum: int, notes: list[str]) -> int | None:
    raw = block.get("class_ledger")
    rules = raw if isinstance(raw, dict) else {}
    if raw is not None and not isinstance(raw, dict):
        notes.append(f"`review_panel.class_ledger` is {raw!r}, not an object — the "
                     "defaults apply")
    want = rules.get(key, CLASS_LEDGER_DEFAULTS[key])
    if want is None or want is False:
        return None
    if isinstance(want, bool) or not isinstance(want, int) or want < minimum:
        raise SystemExit(f"`review_panel.class_ledger.{key}` is {want!r} — expected a "
                         f"whole number >= {minimum}, or null to switch it off")
    return want


def ledger_limits(block: dict, notes: list[str]) -> tuple[int | None, int | None]:
    """(strikes, rounds) from a resolved `review_panel` block, per key. A strike limit
    of 1 is refused: it would demand a structural fix for every first sighting, which
    is not a repeat."""
    return _limit(block, "strikes", 2, notes), _limit(block, "rounds", 1, notes)


def report(ledger: dict, state: dict) -> str:
    by_key = {e["key"]: e for e in ledger.get("classes") or []}
    lines = [f"class ledger: round {state['round']} — {state['classes']} class(es), "
             f"{len(state['new'])} new this round"]
    if state["converged"]:
        lines.append("  CONVERGED: no new class this round")
    elif state["over"]:
        lines.append(f"  BUDGET SPENT: {state['rounds_limit']} round(s) without "
                     "convergence — stop, the owner decides")
    for key in state["new"]:
        lines.append(f"  new: [{key}] {by_key[key]['name']}")
    for key in state["owed"]:
        e = by_key[key]
        lines.append(f"  STRUCTURAL FIX OWED: [{key}] {e['name']} — seen on "
                     f"{strikes(e)} paths; {options(e)}")
    if state["over"]:
        for e in _open_classes(ledger, state["strikes_limit"]):
            lines.append(f"  open: [{e['key']}] {e['name']}: {options(e)}")
    return "\n".join(lines)


#: Exit code when the ledger could not be written — `panel.py --premise`'s number.
WRITE_FAILED_EXIT = 3


def save_ledger(path: str, ledger: dict) -> int:
    """Write the ledger (creating its directory), 0 or :data:`WRITE_FAILED_EXIT`."""
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        print(f"panel_classes: could not create {Path(path).parent} "
              f"({e.__class__.__name__})", file=sys.stderr)
        return WRITE_FAILED_EXIT
    return WRITE_FAILED_EXIT if write_payload(path, ledger) else 0


def _limits_for(repo: str | None) -> tuple[int | None, int | None, str]:
    cfg = load_repo_cfg(repo)
    notes: list[str] = []
    strikes_limit, rounds_limit = ledger_limits(cfg.get("review_panel") or {}, notes)
    for note in notes:
        print(f"panel_classes: {note}", file=sys.stderr)
    return strikes_limit, rounds_limit, cfg.get("github") or ""


def _cmd_record(args, strikes_limit, gh_repo) -> int:
    ledger, problems = load_ledger(args.ledger, gh_repo, args.pr)
    for p in problems:
        print(f"panel_classes: {p}", file=sys.stderr)
    try:
        verdict = record(ledger, args.cls, args.round, args.path, finding=args.finding,
                         invariant=args.invariant, enforcement=args.enforcement,
                         guard_test=args.guard_test, limit=strikes_limit)
    except ValueError as e:
        raise SystemExit(f"panel_classes record: {e}")
    if save_ledger(args.ledger, ledger):
        return WRITE_FAILED_EXIT
    print(json.dumps(verdict) if args.json else
          f"{'new' if verdict['new'] else 'known'} class [{verdict['key']}] "
          f"{verdict['name']} — {verdict['strikes']} path(s)")
    if verdict["owed"]:
        print(f"TWO STRIKES: [{verdict['key']}] has now been seen on {verdict['strikes']}"
              " paths. Do NOT patch this path too: the next fix must make the class "
              "impossible by construction (a required argument, one gate plus a guard, "
              "a type, a constraint). Record it with `panel_classes.py structural "
              f"--class {verdict['key']} --how ... --enforcement ... --guard-test ...`.",
              file=sys.stderr)
        return STRUCTURAL_OWED_EXIT
    return 0


def _cmd_structural(args, gh_repo) -> int:
    ledger, problems = load_ledger(args.ledger, gh_repo, args.pr)
    for p in problems:
        print(f"panel_classes: {p}", file=sys.stderr)
    try:
        verdict = record_structural(ledger, args.cls, args.round, args.how,
                                    enforcement=args.enforcement,
                                    guard_test=args.guard_test)
    except ValueError as e:
        raise SystemExit(f"panel_classes structural: {e}")
    if save_ledger(args.ledger, ledger):
        return WRITE_FAILED_EXIT
    print(json.dumps(verdict) if args.json else
          f"structural fix recorded for [{verdict['key']}] {verdict['name']}")
    return 0


def _cmd_status(args, strikes_limit, rounds_limit, gh_repo) -> int:
    ledger, problems = load_ledger(args.ledger, gh_repo, args.pr)
    for p in problems:
        print(f"panel_classes: {p}", file=sys.stderr)
    round_no = args.round or last_round(ledger)
    if round_no < 1:
        print("class ledger: empty — nothing recorded yet")
        return 0
    state = round_state(ledger, round_no, strikes_limit, rounds_limit)
    print(json.dumps(state) if args.json else report(ledger, state))
    if state["over"]:
        return BUDGET_EXIT
    if state["owed"]:
        return STRUCTURAL_OWED_EXIT
    return 0 if state["converged"] else 1


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="panel_classes.py", description=__doc__.split(
        "\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p, rounds: bool = True):
        p.add_argument("--ledger", required=True, metavar="PATH",
                       help="the PR's ledger, beside its premise register")
        p.add_argument("--repo", default=None, help="repo spec for the rules (default: "
                       "the cwd's repo)")
        p.add_argument("--pr", type=int, default=None)
        p.add_argument("--json", action="store_true")
        if rounds:
            p.add_argument("--round", type=int, default=None,
                           help="the round whose findings this answers")

    rec = sub.add_parser("record", help="record one sighting of a class")
    common(rec)
    rec.add_argument("--class", dest="cls", required=True, metavar="NAME")
    rec.add_argument("--path", required=True,
                     help="file:line, or the route that reaches it")
    rec.add_argument("--finding", default="", metavar="KEY")
    rec.add_argument("--invariant", default="")
    rec.add_argument("--enforcement", default="")
    rec.add_argument("--guard-test", default="", dest="guard_test")

    st = sub.add_parser("structural", help="record a by-construction fix for a class")
    common(st)
    st.add_argument("--class", dest="cls", required=True, metavar="NAME|KEY")
    st.add_argument("--how", required=True)
    st.add_argument("--enforcement", default="")
    st.add_argument("--guard-test", default="", dest="guard_test")

    status = sub.add_parser("status", help="convergence and budget after a round")
    common(status)

    br = sub.add_parser("brief", help="print the reviewer brief for the next round")
    common(br, rounds=False)
    br.add_argument("--wave", action="append", default=[], metavar="PATH",
                    help="another PR's ledger from the same wave (repeatable)")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if getattr(args, "round", None) is not None and args.round < 1:
        raise SystemExit("--round: rounds are numbered from 1")
    if args.cmd == "brief":
        ledger, problems = load_ledger(args.ledger)
        wave, more = load_wave(args.wave)
        for p in problems + more:
            print(f"panel_classes: {p}", file=sys.stderr)
        print(ledger_brief(ledger, wave), end="")
        return 0
    strikes_limit, rounds_limit, gh_repo = _limits_for(args.repo)
    if args.cmd in ("record", "structural") and args.round is None:
        raise SystemExit(f"{args.cmd} needs --round — the round whose findings it answers")
    if args.cmd == "record":
        return _cmd_record(args, strikes_limit, gh_repo)
    if args.cmd == "structural":
        return _cmd_structural(args, gh_repo)
    return _cmd_status(args, strikes_limit, rounds_limit, gh_repo)


if __name__ == "__main__":
    raise SystemExit(main())
