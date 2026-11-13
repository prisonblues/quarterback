"""#836's Codex round — one read-only Codex seat that carries the instruments.

`/fix-issue`, `/fix-issue-here` and the epic coordinators used to hand-roll
`git diff | codex exec "Review this diff..."` loops. Those loops had no brakes at all:
no ledger, no premise register, no round cap. Lex-Ray v22 spent most of its 107 Codex
rounds in them, every round finding one more path to a class an earlier round had
already found. This command is the same cheap second opinion with the class ledger
wired in:

* **round 1** is open-ended, but every finding is classified: a class, its invariant,
  the enforcement that would make it impossible, the guard test, and EVERY instance and
  path the reviewer can find (not just the first);
* **later rounds** are handed the ledger and are closed-ended: verify each recorded
  invariant holds everywhere, report violations and genuinely new classes only;
* **two strikes**: a class seen on a second path is owed a structural fix, and the next
  round will not start until `panel_classes.py structural` records one;
* **the budget**: a round with no new class has converged. After
  `review_panel.class_ledger.rounds` (3) rounds without that, the next is refused and
  the owner decides.

One seat, no judge. **The seat may RUN code**: it works in the builder's own worktree
with a shell under codex's `workspace-write` sandbox, is told to settle a suspicion by
running the touched tests or a snippet and to cite what it ran, and is told not to
change the tree — which is checked, not trusted (`git status` + `git diff` before and
after). A reading argued over for several rounds is often one failing test. The
panel's seats stay shell-less (#92) because they read PR heads that may not be the
reader's own; that reason does not apply to a builder reviewing their own tree.
`--no-exec` gives the panel's read-only seat instead, and `--network` opens the
sandbox's network for a DB-backed test. `--shard` splits the read by area across parallel calls,
which shortens the serial chain instead of finding one path per round. `--design`
reviews a design note instead of the diff, for the design round invariant-heavy work
starts with — and the classes it records carry over into the code rounds.

Exit codes: 0 clean (converged, nothing to fix) · 1 findings to fix, round again ·
2 codex unavailable or unusable (best-effort callers skip) · 3 ledger not written ·
4 budget spent without convergence (owner decides) · 5 structural fix owed.

Usage:
    codex_round.py [--base REF] [--ledger PATH] [--round N] [--design FILE]
                   [--shard data,api,concurrency|all] [--wave PATH ...]
                   [--class-rounds N] [--dry-run] [--json]
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import panel_classes as pc
from panel_core import load_repo_cfg
from panel_seats import codex_args

UNAVAILABLE_EXIT = 2
CODEX_TIMEOUT = 1800

#: The areas `--shard` splits a review across. Named after where v22's classes lived:
#: every one of its non-converging loops was in one of these three.
SHARDS = {
    "data": "DATA AND THE DATABASE — schema, migrations, queries, transactions, "
            "constraints, what a rollback leaves behind, connection-pool limits",
    "api": "API AND VISIBILITY — who may see what, authentication, and EVERY read path "
           "that reaches a guarded value (resolvers, MCP and tool reads, exports, caches)",
    "concurrency": "CONCURRENCY AND ORDERING — races, locks, idempotency, retries, a "
                   "value read separately from the token that guards it, background "
                   "workers",
}

PROMPT = """You are one independent reviewer of {what}. Report REAL defects only —
correctness, security, data integrity, error handling, broken edge cases — not style.
{focus}
CLASSIFY EVERY DEFECT. A finding names a location; a CLASS names what every location with this
defect has in common ("resolver call without the viewer", "token read separately from the value
it guards", "outcome inferred instead of recorded"). For each class give:
- the INVARIANT that, held everywhere, rules it out;
- the ENFORCEMENT that would make it impossible by construction (a required argument, one gate
  plus a static guard, a type, a database constraint) rather than a per-path patch;
- the GUARD TEST that would keep it impossible (a static walk, a property test);
- EVERY instance in the material and EVERY path that reaches it — not the first one you saw. A
  review that reports one path per round never converges.

{execution}{ledger}Return ONLY a JSON object, no prose:
{{"violations": [{{"class": "<key of a recorded class>", "file": "path", "line": <int|null>,
                  "path": "how execution reaches it", "detail": "...",
                  "evidence": "command you ran and what it showed, or \"\""}}],
  "new_classes": [{{"name": "...", "invariant": "...", "enforcement": "...", "guard_test": "...",
                   "instances": [{{"file": "path", "line": <int|null>,
                                  "path": "how execution reaches it", "detail": "...",
                                  "evidence": "command you ran and what it showed, or \"\""}}]}}]}}
Both arrays empty only if the material is genuinely free of defects.

{material}
"""

#: Said to a seat that has a shell. The checkout it runs in is the builder's own
#: worktree, so the one rule is not to change it.
EXECUTION = """YOU CAN RUN CODE. You are in the change's own working tree with a shell. When a
suspicion can be settled by running something — the touched test files, a one-off snippet, a
query against the test database — run it, and put the command and what it showed in `evidence`.
A defect shown by a failing run outranks one argued from reading; a suspicion a run refutes is not
a finding. Do NOT edit, create or delete files in the tree, commit, or stash; scratch work goes
under $TMPDIR.

"""

OPEN_ROUND = ("No classes are recorded yet: this round is open-ended. Find every class "
              "the material holds.\n\n")


def git(*args: str, cwd: str | None = None) -> str:
    out = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if out.returncode != 0:
        raise SystemExit(f"codex_round: git {' '.join(args)} failed: "
                         f"{out.stderr.strip() or out.stdout.strip()}")
    return out.stdout


def default_base() -> str:
    try:
        return git("rev-parse", "--abbrev-ref", "origin/HEAD").strip()
    except SystemExit:
        return "origin/main"


def default_ledger() -> str:
    """Per WORKTREE, not per repo: `--git-dir` is the worktree's own, so two worktrees
    on one branch name never share a ledger, and nothing lands in the tree."""
    git_dir = Path(git("rev-parse", "--absolute-git-dir").strip())
    branch = git("branch", "--show-current").strip() or "detached"
    return str(git_dir / "qb-classes" / (re.sub(r"[^A-Za-z0-9._-]+", "-", branch)
                                         + ".json"))


def material(base: str, design: str) -> tuple[str, str]:
    """(what is reviewed, the text). The diff is against the merge base and INCLUDES
    uncommitted changes, so it can run before the builder commits."""
    if design:
        return (f"the design note {design} (no code yet — review the DESIGN: invariants, "
                "races, ordering, what each path may see)", Path(design).read_text())
    merge_base = git("merge-base", base, "HEAD").strip()
    diff = git("diff", merge_base)
    if not diff.strip():
        raise SystemExit(f"codex_round: no changes against {base} — nothing to review")
    return f"a code change (diff against {base})", f"Diff:\n{diff}"


def build_prompt(what: str, text: str, ledger: dict, wave: list[dict],
                 shard: str = "", can_run: bool = False) -> str:
    brief = pc.ledger_brief(ledger, wave) or OPEN_ROUND
    focus = (f"\nYOUR AREA THIS ROUND: {SHARDS[shard]}. Other reviewers cover the other "
             "areas; go deep on this one.\n" if shard else "")
    return PROMPT.format(what=what, focus=focus, execution=EXECUTION if can_run else "",
                         ledger=brief, material=text)


def parse_reply(text: str) -> dict | None:
    """The reply's JSON object, or None when there is none to read."""
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        reply = json.loads(text[start:end + 1])
    except ValueError:
        return None
    if not isinstance(reply, dict):
        return None
    return {"violations": [v for v in reply.get("violations") or []
                           if isinstance(v, dict)],
            "new_classes": [c for c in reply.get("new_classes") or []
                            if isinstance(c, dict)]}


def exec_args(model: str, effort: str, reply_file: Path, network: bool) -> list[str]:
    """`codex exec` argv for a seat that may RUN code in the builder's worktree.

    The panel's seats are pinned shell-less (`panel_seats.codex_args`, #92) because
    they read a PR head that may not be the reader's own. This seat reviews the
    builder's OWN worktree, so that reason does not hold here, and a seat that can run
    the test it suspects would fail settles in one round what a reading argues about
    for several. `workspace-write` bounds writes to the worktree and the temp dir;
    the network stays off unless the caller asks (a DB-backed test needs it)."""
    args = ["codex", "exec", "-s", "workspace-write",
            "-c", 'web_search="disabled"',
            "-c", f"sandbox_workspace_write.network_access={str(network).lower()}"]
    if model:
        args += ["--model", model]
    if effort:
        args += ["-c", f"model_reasoning_effort={effort}"]
    return args + ["--json", "--output-last-message", str(reply_file)]


def tree_state(top: str) -> str:
    """What the worktree looks like, to catch a seat that edited it."""
    return git("status", "--porcelain=v1", "-uall", cwd=top) + git("diff", cwd=top)


def run_codex(prompt: str, model: str, effort: str, top: str = "",
              network: bool = False) -> dict | None:
    """One seat's reply. ``top`` set means the seat runs IN that worktree with a
    shell; empty means the read-only, shell-less panel seat in a scratch dir."""
    with tempfile.TemporaryDirectory() as tmp:
        reply_file = Path(tmp) / "reply.txt"
        argv = (exec_args(model, effort, reply_file, network) if top
                else codex_args(model, effort, reply_file))
        try:
            subprocess.run(argv, input=prompt,
                           capture_output=True, text=True, timeout=CODEX_TIMEOUT,
                           cwd=top or tmp, check=False)
        except (OSError, subprocess.TimeoutExpired) as e:
            print(f"codex_round: codex did not finish ({e.__class__.__name__})",
                  file=sys.stderr)
            return None
        if not reply_file.exists():
            return None
        return parse_reply(reply_file.read_text())


def _where(item: dict) -> str:
    loc = str(item.get("file") or "?")
    if isinstance(item.get("line"), int):
        loc += f":{item['line']}"
    route = " ".join(str(item.get("path") or "").split())
    return f"{loc} via {route}" if route else loc


def _evidence(item: dict) -> str:
    ran = " ".join(str(item.get("evidence") or "").split())
    return f" [ran: {ran}]" if ran else ""


def apply_replies(ledger: dict, replies: list[dict], round_no: int,
                  strikes: int | None) -> list[str]:
    """Record every sighting the replies carry; returns the lines that say what was
    found. A violation naming a key the ledger does not hold is reported, not
    recorded — a class with no invariant is not one the next round can check."""
    lines: list[str] = []
    for reply in replies:
        for v in reply["violations"]:
            entry = pc.find_class(ledger, str(v.get("class") or ""))
            if entry is None:
                lines.append(f"  unclassified: {_where(v)} — {v.get('detail', '')} "
                             f"(named class {v.get('class')!r}, which is not recorded)")
                continue
            pc.record(ledger, entry["name"], round_no, _where(v), limit=strikes)
            lines.append(f"  violation [{entry['key']}] {entry['name']}: {_where(v)} — "
                         f"{v.get('detail', '')}{_evidence(v)}")
        for c in reply["new_classes"]:
            lines.extend(_record_new(ledger, c, round_no, strikes))
    return lines


def _record_new(ledger: dict, c: dict, round_no: int, strikes: int | None) -> list[str]:
    name = " ".join(str(c.get("name") or "").split())
    instances = [i for i in c.get("instances") or [] if isinstance(i, dict)]
    if not name or not instances or not str(c.get("invariant") or "").strip():
        return [f"  dropped: a class reply without a name, an invariant or an instance "
                f"({name or 'unnamed'})"]
    lines = []
    for i in instances:
        verdict = pc.record(ledger, name, round_no, _where(i),
                            invariant=str(c.get("invariant") or ""),
                            enforcement=str(c.get("enforcement") or ""),
                            guard_test=str(c.get("guard_test") or ""), limit=strikes)
        tag = "new class" if verdict["new"] else f"[{verdict['key']}]"
        lines.append(f"  {tag} {name}: {_where(i)} — {i.get('detail', '')}{_evidence(i)}")
    return lines


def exit_code(state: dict, found: list[str], partial: bool = False) -> int:
    """A round where a shard went unanswered is never clean: its area was not read."""
    if state["over"]:
        return pc.BUDGET_EXIT
    if state["owed"]:
        return pc.STRUCTURAL_OWED_EXIT
    return 0 if state["converged"] and not found and not partial else 1


def _seat() -> tuple[str, str, int | None, int | None]:
    cfg = load_repo_cfg(None)
    codex = (cfg.get("reviewers") or {}).get("codex") or {}
    notes: list[str] = []
    strikes, rounds = pc.ledger_limits(cfg.get("review_panel") or {}, notes)
    for note in notes:
        print(f"codex_round: {note}", file=sys.stderr)
    return str(codex.get("model") or ""), str(codex.get("effort") or ""), strikes, rounds


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="codex_round.py", description=__doc__.split(
        "\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", default="", help="ref to diff against (default origin/HEAD)")
    ap.add_argument("--ledger", default="", help="class ledger (default: per worktree, "
                    "under the worktree's git dir)")
    ap.add_argument("--round", type=int, default=None, dest="round_no",
                    help="default: one past the ledger's last round")
    ap.add_argument("--design", default="", metavar="FILE",
                    help="review this design note instead of the diff")
    ap.add_argument("--shard", default="", help="comma list of "
                    f"{','.join(SHARDS)}, or 'all': one parallel call per area")
    ap.add_argument("--wave", action="append", default=[], metavar="PATH",
                    help="another PR's ledger from the same wave (repeatable)")
    ap.add_argument("--class-rounds", type=int, default=None, dest="class_rounds",
                    help="override the round budget — the owner's call")
    ap.add_argument("--no-exec", action="store_true", dest="no_exec",
                    help="the panel's read-only, shell-less seat instead of one that may "
                         "run tests and code in the worktree")
    ap.add_argument("--network", action="store_true",
                    help="let the seat's sandbox reach the network (a local test DB)")
    ap.add_argument("--dry-run", action="store_true", help="print the prompt and stop")
    ap.add_argument("--json", action="store_true")
    return ap


def shards_of(spec: str) -> list[str]:
    if not spec:
        return [""]
    names = list(SHARDS) if spec.strip() == "all" else [
        s.strip() for s in spec.split(",") if s.strip()]
    bad = [n for n in names if n not in SHARDS]
    if bad or not names:
        raise SystemExit(f"--shard: unknown area {', '.join(bad) or spec!r} — expected "
                         f"{', '.join(SHARDS)} or all")
    return names


def ask_seats(prompts: list[str], model: str, effort: str, top: str,
              network: bool) -> list[dict | None]:
    """Run every shard in parallel. With ``top`` set the seats run in the worktree,
    and a tree they changed is reported — the rule they were given is not trusted
    to have held."""
    before = tree_state(top) if top else ""
    with ThreadPoolExecutor(max_workers=len(prompts)) as pool:
        replies = list(pool.map(lambda p: run_codex(p, model, effort, top, network),
                                prompts))
    if top and tree_state(top) != before:
        print("codex_round: WARNING — the seat CHANGED the worktree (it was told not "
              "to). Inspect `git status` / `git diff` before you commit anything.",
              file=sys.stderr)
    return replies


def print_round(args, round_no: int, ledger_path: str, ledger: dict, state: dict,
                found: list[str], answered: int, asked: int) -> None:
    if args.json:
        print(json.dumps({"ledger": ledger_path, "state": state, "found": found,
                          "shards_answered": answered, "shards": asked}))
        return
    print(f"codex round {round_no} ({ledger_path}):")
    if answered < asked:
        print(f"  PARTIAL: {asked - answered} of {asked} shard(s) gave no usable reply "
              "— their areas were not read this round")
    print("\n".join(found) or "  nothing found")
    print(pc.report(ledger, state))


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    shards = shards_of(args.shard)
    model, effort, strikes, rounds = _seat()
    if args.class_rounds is not None:
        rounds = args.class_rounds
    ledger_path = args.ledger or default_ledger()
    ledger, problems = pc.load_ledger(ledger_path)
    wave, more = pc.load_wave(args.wave)
    for p in problems + more:
        print(f"codex_round: {p}", file=sys.stderr)
    round_no = args.round_no or pc.last_round(ledger) + 1
    refusal = pc.budget_refusal(ledger, round_no, strikes, rounds)
    if refusal:
        print(refusal, file=sys.stderr)
        state = pc.round_state(ledger, round_no - 1, strikes, rounds)
        return pc.BUDGET_EXIT if state["over"] else pc.STRUCTURAL_OWED_EXIT
    what, text = material(args.base or default_base(), args.design)
    top = "" if args.no_exec else git("rev-parse", "--show-toplevel").strip()
    prompts = [build_prompt(what, text, ledger, wave, s, bool(top)) for s in shards]
    if args.dry_run:
        print("\n\n=====\n\n".join(prompts))
        return 0
    if not shutil.which("codex"):
        print("codex_round: codex is not installed — skipped", file=sys.stderr)
        return UNAVAILABLE_EXIT
    replies = ask_seats(prompts, model, effort, top, args.network)
    answered = sum(1 for r in replies if r)
    if not answered:
        print("codex_round: no usable reply from codex — skipped", file=sys.stderr)
        return UNAVAILABLE_EXIT
    found = apply_replies(ledger, [r for r in replies if r], round_no, strikes)
    write_failed = pc.save_ledger(ledger_path, ledger)
    state = pc.round_state(ledger, round_no, strikes, rounds)
    print_round(args, round_no, ledger_path, ledger, state, found, answered, len(prompts))
    return write_failed or exit_code(state, found, answered < len(prompts))


if __name__ == "__main__":
    raise SystemExit(main())
