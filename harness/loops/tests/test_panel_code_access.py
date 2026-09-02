"""Reviewer code access: the per-repo setting, who can take it, and what is stripped.

#113's second half. Its first half stopped the coverage veto reporting a CONSTANT —
"I could not read a file outside the diff" is true of every round a blind seat sits —
and this is the other side of that trade: make the seat able to read, and the
declaration becomes a fact about the round again.

Three things these tests hold down, in descending order of what they cost if wrong:

**The strip must not escape the tree.** `Path.is_dir()` is true of a symlink to a
directory, so a `.claude -> ~/.claude` in a PR's tarball would send `rmtree` at the
real one. That is a tarball an outside contributor controls on exactly the repos this
setting exists to be switched OFF for, and it is still PR-controlled on the repos it
is on for.

**Only a seat that can express "read but not execute" gets the tree.** #92 answered
"may reviewers execute?" with no. Of the four vendors only claude can name a tool set
(`--allowedTools`), so `SEAT_READS_CODE` is an allowlist of one and the other three
keep their empty sandbox — handing them the checkout would take #75's instruction-file
channel for zero reading in return.

**A failure anywhere degrades to the OFF posture, loudly.** A fetch that 502s, a
tarball that will not unpack, a copy that runs out of disk: each leaves the seat blind,
recorded as blind, with a note saying why. A round that reviews from the diff is the
behaviour this repo shipped for months; a round that believes it read code it did not
is a confident review of nothing.
"""
import json
import os
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import panel  # noqa: E402
import panel_core  # noqa: E402
import panel_seats  # noqa: E402
from conftest import gh_stub, pr_tarball  # noqa: E402

# --------------------------------------------------------------- the strip

def _tree(root: Path, files: dict) -> Path:
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    return root


def test_the_strip_reaches_every_depth_not_just_the_root(tmp_path):
    """`claude` reads a `CLAUDE.md` beside the file it is looking at, so stripping
    only the top level leaves every nested one live — and a PR touching a
    subdirectory is exactly where one would be added."""
    root = _tree(tmp_path / "t", {
        "CLAUDE.md": "root", "app/AGENTS.md": "nested",
        "app/deep/er/GEMINI.md": "deeper", "app/main.py": "x = 1"})
    (root / ".claude").mkdir()
    (root / ".claude/settings.json").write_text("{}")

    removed = panel.strip_convention_files(root)

    assert removed == [".claude", "CLAUDE.md", "app/AGENTS.md",
                       "app/deep/er/GEMINI.md"]
    assert not (root / "CLAUDE.md").exists()
    assert not (root / "app/AGENTS.md").exists()
    assert not (root / "app/deep/er/GEMINI.md").exists()
    assert not (root / ".claude").exists()
    # ...and it took nothing else.
    assert (root / "app/main.py").read_text() == "x = 1"


def test_a_convention_dir_that_is_a_symlink_is_unlinked_not_followed(tmp_path):
    """The one test here that is about damage rather than coverage.

    `Path.is_dir()` follows symlinks and answers True for a link to a directory, so
    checking `is_dir()` before `is_symlink()` sends `shutil.rmtree` through the link
    and deletes the TARGET. A PR's tarball can contain `\\.claude -> /home/you/.claude`,
    and the population this setting is off for is precisely the one that would.

    Asserted on the target surviving, not on the link being gone: the link is gone
    either way, and only one of the two orderings leaves the real directory intact."""
    root = _tree(tmp_path / "t", {"app/main.py": "x = 1"})
    precious = tmp_path / "precious"
    precious.mkdir()
    (precious / "settings.json").write_text("do not delete me")
    os.symlink(precious, root / ".claude")
    os.symlink(precious / "settings.json", root / "CLAUDE.md")

    removed = panel.strip_convention_files(root)

    assert sorted(removed) == [".claude", "CLAUDE.md"]
    assert not (root / ".claude").exists() and not (root / "CLAUDE.md").is_symlink()
    assert precious.is_dir(), "rmtree followed the symlink out of the tree"
    assert (precious / "settings.json").read_text() == "do not delete me"


def test_a_tree_carrying_nothing_reports_an_empty_list_not_a_null(tmp_path):
    """`[]` is "the PR carried none", which is a different fact from the null a round
    that never fetched a tree records — and the payload keeps them apart, so this
    must not start returning None for a clean tree."""
    root = _tree(tmp_path / "t", {"app/main.py": "x = 1"})
    assert panel.strip_convention_files(root) == []


def test_the_strip_covers_every_vendor_the_panel_can_seat(tmp_path):
    """A denylist is only as good as its entries, and the seats are the entries that
    matter most: a file the panel's OWN vendors load is not a rot risk, it is a hole
    open today. This is the list, spelled out, so adding a seat without adding its
    convention file fails here rather than in a review nobody can see."""
    for name in ("CLAUDE.md", "AGENTS.md", "GEMINI.md"):
        assert name in panel.CONVENTION_FILES, name
    # grok reads six spellings of four names and the title-case pair is its alone,
    # so the shouted spellings above do not cover it (#292).
    for name in ("Agents.md", "Claude.md", "CLAUDE.local.md", "AGENT.md"):
        assert name in panel.CONVENTION_FILES, name
    for name in (".claude", ".codex", ".gemini", ".antigravity", ".grok"):
        assert name in panel.CONVENTION_DIRS, name


# --------------------------------------------------------------- the fetch

def test_the_github_wrapper_directory_is_unwrapped(monkeypatch, tmp_path):
    """GitHub wraps a tarball in one `owner-repo-sha/` directory. The seat's cwd has
    to be the repo ROOT or every path a reviewer quotes gains a prefix that appears
    in no diff — a whole review of real code, misfiled."""
    monkeypatch.setattr(panel_core, "sh_bytes", lambda *a, **k: pr_tarball(
        files={"app/main.py": "x = 1"}, prefix="acme-board-deadbee"))
    tree, problem = panel.fetch_pr_tree("acme/board", "deadbee", tmp_path)
    assert problem == ""
    assert tree is not None and tree.name == "acme-board-deadbee"
    assert (tree / "app/main.py").read_text() == "x = 1"
    # The download itself is not left lying about inside the seat's evidence.
    assert not list(tmp_path.glob("*.tar.gz"))


@pytest.mark.parametrize("boom,why", [
    (subprocess.CalledProcessError(1, "gh", stderr=b"gh: Not Found (HTTP 404)"),
     "Not Found"),
    (subprocess.TimeoutExpired("gh", 120), "TimeoutExpired"),
    # `OSError(2, ...)` INSTANTIATES as FileNotFoundError — Python maps errno to the
    # subclass — so the recorded class name is that, not "OSError". Spelled out
    # because the obvious expectation is the wrong one.
    (OSError(2, "No such file or directory"), "FileNotFoundError"),
])
def test_a_fetch_that_fails_returns_a_problem_and_never_raises(monkeypatch, tmp_path,
                                                               boom, why):
    """The contract `fetch_increment` set and the reason for it: code access is an
    ENHANCEMENT to a review, so it must not be able to kill a review that would
    otherwise have happened. Every family, because naming the obvious two is what
    left `UnicodeDecodeError` uncaught in the function this one copies."""
    def raiser(*a, **k):
        raise boom
    monkeypatch.setattr(panel_core, "sh_bytes", raiser)
    tree, problem = panel.fetch_pr_tree("acme/board", "deadbee", tmp_path)
    assert tree is None
    assert why in problem and "deadbee" in problem


def test_bytes_that_are_not_a_tarball_are_reported_rather_than_raised(monkeypatch,
                                                                      tmp_path):
    """A 200 that is not a gzip — a proxy's error page, a truncated body — used to be
    the shape that got through the fetch guard and died in the unpack."""
    monkeypatch.setattr(panel_core, "sh_bytes", lambda *a, **k: b"<html>502</html>")
    tree, problem = panel.fetch_pr_tree("acme/board", "deadbee", tmp_path)
    assert tree is None and "could not unpack" in problem


def test_a_tarball_escaping_its_destination_is_refused(monkeypatch, tmp_path):
    """`extractall(filter="data")` is the guard, and this is what it guards against.
    Without it a member named `../../../etc/whatever` writes wherever this process
    can — and the tarball is the PR's, on the repos this setting exists to protect."""
    import io
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        data = b"pwned"
        info = tarfile.TarInfo("acme-board-dead/../../escaped.txt")
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))
    monkeypatch.setattr(panel_core, "sh_bytes", lambda *a, **k: buf.getvalue())

    tree, problem = panel.fetch_pr_tree("acme/board", "deadbee", tmp_path)

    assert tree is None and "could not unpack" in problem
    assert not (tmp_path.parent / "escaped.txt").exists()


@pytest.mark.parametrize("prefixes", [[], ["one", "two"]])
def test_an_unexpected_tarball_shape_is_reported_rather_than_guessed(monkeypatch,
                                                                     tmp_path,
                                                                     prefixes):
    """Zero top-level entries, or several. Guessing at either is how a seat ends up
    rooted one directory off with every path subtly wrong — so it is reported and the
    seat stays blind, which is a posture that works.

    The empty case is not symmetry for its own sake: `extractall` creates the
    destination only when it has something to put there, so an empty tarball leaves no
    directory and `iterdir` on it raised FileNotFoundError straight out of a function
    whose contract is that it never raises."""
    import io
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for i, pref in enumerate(prefixes):
            data = b"x"
            info = tarfile.TarInfo(f"{pref}/f{i}.py")
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    monkeypatch.setattr(panel_core, "sh_bytes", lambda *a, **k: buf.getvalue())
    tree, problem = panel.fetch_pr_tree("acme/board", "deadbee", tmp_path)
    assert tree is None and "unpacked to" in problem


# --------------------------------------------------------------- the seat's copy

def test_each_seat_gets_its_own_copy_that_is_a_git_repo(tmp_path):
    """Per seat for the reason `member_sandbox` is: two seats must not be able to
    interact through their working directory. And a git repo because codex refuses to
    start outside one — a tarball carries no `.git`."""
    tree = _tree(tmp_path / "src", {"app/main.py": "x = 1"})
    one, ok_one = panel.seat_checkout(tree, tmp_path / "seat1")
    two, ok_two = panel.seat_checkout(tree, tmp_path / "seat2")

    assert ok_one and ok_two
    assert one != two
    for cwd in (one, two):
        assert (Path(cwd) / "app/main.py").read_text() == "x = 1"
        assert (Path(cwd) / ".git").exists()
    # Writing in one is invisible to the other, which is the property being bought.
    (Path(one) / "app/main.py").write_text("tampered")
    assert (Path(two) / "app/main.py").read_text() == "x = 1"


def test_a_copy_that_fails_says_so_rather_than_handing_over_half_a_tree(monkeypatch,
                                                                        tmp_path,
                                                                        capsys):
    """The second return value is the whole point. A seat left believing it has the
    tree records `code_blind: False`, which puts its declarations back into the
    coverage veto on the strength of an access it never got, and tells the board the
    round had coverage it did not."""
    def boom(*a, **k):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(panel_seats.shutil, "copytree", boom)
    tree = _tree(tmp_path / "src", {"app/main.py": "x = 1"})

    cwd, ok = panel.seat_checkout(tree, tmp_path / "seat")

    assert ok is False, "a failed copy must not report the seat as sighted"
    assert not (Path(cwd) / "app/main.py").exists(), "half a tree was handed over"
    assert (Path(cwd) / ".git").exists(), "the seat still needs a repo to start in"
    assert "could not stage the PR tree" in capsys.readouterr().err


# --------------------------------------------------------------- who gets it

def _seat_run(monkeypatch, reply="[]", probe=("app/main.py", ".git")):
    """One seat run to completion, recording what its cwd CONTAINED at the moment the
    CLI would have started.

    Probed inside the stub rather than asserted afterwards, because `run_seat` holds
    its sandbox in a `with tempfile.TemporaryDirectory()` — by the time `review_llm`
    returns, the directory the seat ran in no longer exists, and every assertion
    about it passes or fails for the wrong reason."""
    seen = {}
    monkeypatch.setattr(panel.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(panel_seats, "claude_usage", lambda _s: None)

    def fake_run_cli(args, label, timeout=panel.CLI_TIMEOUT, attempts=3,
                     stdin_text=None, on_output=None, replied=None, cwd=None):
        seen["cwd"] = cwd
        seen["args"] = args() if callable(args) else args
        seen["had"] = {rel: (Path(cwd) / rel).exists() for rel in probe}
        seen["stdin"] = stdin_text or ""
        return reply, None

    monkeypatch.setattr(panel_seats, "run_cli", fake_run_cli)
    return seen


@pytest.mark.parametrize("seat", sorted(panel.LLM_REVIEWERS))
def test_only_a_seat_that_can_read_without_executing_is_given_the_tree(monkeypatch,
                                                                       tmp_path, seat):
    """`SEAT_READS_CODE` is an allowlist of one, and this is the gate that enforces
    it. #92 answered "may reviewers execute?" with no, and of the four vendors only
    claude can name a tool set: codex's only read path is its shell, pi's
    `--no-tools` is all-or-nothing, and antigravity has no tool mechanism at all.

    A seat NOT on the list keeps its empty sandbox even though a tree was prepared —
    standing in a checkout it cannot read would buy #75's instruction-file channel
    for nothing in return."""
    tree = _tree(tmp_path / "src", {"app/main.py": "x = 1"})
    seen = _seat_run(monkeypatch)
    got = panel.review_llm(seat, "m", "p", code_tree=tree)

    reads = seat in panel.SEAT_READS_CODE
    assert reads is (seat == "claude"), "the allowlist changed without this test"
    assert got.code_blind is not reads
    assert seen["had"]["app/main.py"] is reads
    # Either way the seat got a git repo to start in.
    assert seen["had"][".git"] is True


def test_the_read_only_tool_pin_arrives_only_with_the_tree(monkeypatch, tmp_path):
    """Without `--allowedTools` this seat has its full default set INCLUDING Bash —
    measured on claude 2.1.232, a bare `claude -p` in an empty repo ran
    `echo TOOLS-OK-$((6*7))` and reported `TOOLS-OK-42`. Harmless while its cwd is
    empty, which is why the pin is not applied then; decisive once the cwd is a
    contributor's checkout, where a shell would run the contributor's code."""
    tree = _tree(tmp_path / "src", {"app/main.py": "x = 1"})

    seen = _seat_run(monkeypatch)
    panel.review_llm("claude", "sonnet", "p", code_tree=tree)
    assert "--allowedTools" in seen["args"]
    for tool in panel.READ_ONLY_TOOLS:
        assert tool in seen["args"]
    assert "Bash" not in seen["args"]
    assert seen["args"][seen["args"].index("--permission-mode") + 1] == "manual"

    seen = _seat_run(monkeypatch)
    panel.review_llm("claude", "sonnet", "p")
    assert "--allowedTools" not in seen["args"]


def test_a_staging_failure_downgrades_the_seat_rather_than_lying_about_it(monkeypatch,
                                                                          tmp_path,
                                                                          capsys):
    """`reads_code` is reassigned from what HAPPENED, not left as the intent. A seat
    whose copy failed has an empty sandbox, so recording it as sighted would put its
    declarations back into the coverage veto over an access it never got — and would
    hand it the read-only tool pin, which is the harmless half of being wrong."""
    tree = _tree(tmp_path / "src", {"app/main.py": "x = 1"})

    def boom(*a, **k):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(panel_seats.shutil, "copytree", boom)
    seen = _seat_run(monkeypatch)

    got = panel.review_llm("claude", "sonnet", "p", code_tree=tree)

    assert got.code_blind is True, "a seat with no tree must record itself blind"
    assert seen["had"]["app/main.py"] is False
    assert "--allowedTools" not in seen["args"], "pinned for a tree it does not have"
    assert "could not stage the PR tree" in capsys.readouterr().err


# --------------------------------------------------------------- through run()

CFG = {"github": "acme/board", "path": "/tmp/acme-board",
       "_rules_baseline": ".harness-rules.sample",
       "reviewers": {"claude": {"enabled": True, "model": "sonnet"},
                     "codex": {"enabled": True, "model": "", "effort": ""}},
       "review_panel": {}}


def _round(monkeypatch, tmp_path, capsys, *, cfg_extra=None, tree=None,
           no_code_access=False, findings=()):
    """A whole `run`, returning (report, payload). Records the prompt each seat got
    and whether it was handed a tree."""
    seen = {"prompts": {}, "trees": {}}
    cfg = json.loads(json.dumps(CFG))
    cfg["review_panel"].update(cfg_extra or {})
    monkeypatch.setattr(panel, "load_repo_cfg", lambda _n: cfg)
    monkeypatch.setattr(panel_core, "sh", gh_stub(
        meta={"title": "feat: x", "additions": 3, "deletions": 1,
              "headRefOid": "abcdef1234"},
        diff="diff --git a/a.py b/a.py\n+x\n"))
    if tree is not None:
        monkeypatch.setattr(panel_core, "sh_bytes", lambda *a, **k: tree)

    def reviewer(name, model, prompt, effort="", code_tree=None, budget_usd=None):
        seen["prompts"][name] = prompt
        seen["trees"][name] = code_tree
        seen.setdefault("budgets", {})[name] = budget_usd
        return panel.ReviewerRun(list(findings), None, 10, [],
                                 code_blind=not (code_tree is not None
                                                 and name in panel.SEAT_READS_CODE))

    monkeypatch.setattr(panel, "review_llm", reviewer)
    monkeypatch.setattr(panel, "review_ci", lambda *a: ("PASS", [], None))
    monkeypatch.setattr(panel, "adjudicate", lambda *a, **k: ([], "", panel.CoverageRuling()))
    out = tmp_path / "p.json"
    assert panel.run("board", 34, post=False, json_file=str(out), record=False,
                     no_code_access=no_code_access) == 0
    return capsys.readouterr().out, json.loads(out.read_text()), seen


def test_the_setting_is_on_by_default_and_only_claude_takes_it(monkeypatch, tmp_path,
                                                                capsys):
    """The default, end to end. ON because the blindness was measured and expensive —
    nine of nineteen veto lines on PR #160's round 1 were reviewers declaring they
    could not read a file this repo answers."""
    report, payload, seen = _round(monkeypatch, tmp_path, capsys)

    assert payload["code_access"]["setting"] is True
    assert payload["code_access"]["seats"] == ["claude"]
    assert seen["trees"]["claude"] is not None
    assert seen["trees"]["codex"] is not None, "the tree is offered to every seat"
    # ...and the GATE is in run_seat, so codex records itself blind regardless.
    assert payload["reviewers"]["claude"]["code_blind"] is False
    assert payload["reviewers"]["codex"]["code_blind"] is True
    assert "**Code access:** claude read the PR's tree at abcdef12" in report
    assert "codex reviewed the diff alone" in report


def test_only_the_seat_with_the_tree_is_told_it_has_one(monkeypatch, tmp_path, capsys):
    """A seat has to be TOLD, or the access buys nothing: the prompt's frame is "here
    is a diff" and its `could_not_assess` instruction explicitly offers "a file the
    diff does not include" as a valid answer. Telling a seat that CANNOT read would be
    worse than saying nothing — it would declare gaps it was invited to close by
    opening files it has no tool for."""
    _report, _payload, seen = _round(monkeypatch, tmp_path, capsys)
    assert "YOU HAVE THE CODE" in seen["prompts"]["claude"]
    assert "YOU HAVE THE CODE" not in seen["prompts"]["codex"]


def test_the_setting_off_fetches_nothing_at_all(monkeypatch, tmp_path, capsys):
    """OFF is today's posture and has to cost nothing: no download, no strip, every
    seat blind. `sh_bytes` raising is the assertion — if the fetch were attempted the
    round would carry a note about it."""
    def no_fetch(*a, **k):
        raise AssertionError("a tarball was fetched with code access off")
    monkeypatch.setattr(panel_core, "sh_bytes", no_fetch)
    report, payload, seen = _round(monkeypatch, tmp_path, capsys,
                                   cfg_extra={"reviewer_code_access": False})

    assert payload["code_access"]["setting"] is False
    assert payload["code_access"]["seats"] == []
    assert payload["code_access"]["convention_files_removed"] is None
    assert all(v is None for v in seen["trees"].values())
    assert all(m["code_blind"] is True for m in payload["reviewers"].values())
    assert "**Code access:**" not in report


def test_the_flag_overrides_a_repo_that_turned_it_on(monkeypatch, tmp_path, capsys):
    """`--no-code-access` is one run's override of the repo's setting, the same shape
    as `--reviewers`. There is deliberately no flag the other way: turning access ON
    for a repo that switched it off is a decision about trusting that repo's
    contributors, and belongs in its config rather than in someone's shell history."""
    def no_fetch(*a, **k):
        raise AssertionError("a tarball was fetched with --no-code-access")
    monkeypatch.setattr(panel_core, "sh_bytes", no_fetch)
    _report, payload, seen = _round(monkeypatch, tmp_path, capsys,
                                    cfg_extra={"reviewer_code_access": True},
                                    no_code_access=True)
    assert payload["code_access"]["setting"] is False
    assert all(v is None for v in seen["trees"].values())


def test_a_fetch_failure_reviews_from_the_diff_and_says_so(monkeypatch, tmp_path,
                                                            capsys):
    """The degraded path, which is the OFF posture plus a note. The review still
    happens — that is the contract — and every seat records itself blind, so the
    coverage veto reads the round correctly without being told twice."""
    def boom(*a, **k):
        raise subprocess.CalledProcessError(1, "gh", stderr=b"gh: HTTP 502")
    monkeypatch.setattr(panel_core, "sh_bytes", boom)
    report, payload, seen = _round(monkeypatch, tmp_path, capsys)

    assert payload["code_access"]["setting"] is True, "the SETTING was still on"
    assert payload["code_access"]["seats"] == [], "and no seat actually got it"
    assert payload["code_access"]["convention_files_removed"] is None
    assert any("review from the diff alone" in n for n in payload["config_notes"])
    assert all(m["code_blind"] is True for m in payload["reviewers"].values())
    assert payload["reviewed"] is True, "a failed fetch must not cost the review"


def test_what_the_strip_removed_is_named_in_the_round(monkeypatch, tmp_path, capsys):
    """Said out loud, per round. A silent strip makes a PR that shipped an `AGENTS.md`
    indistinguishable from one that did not, on the single axis where the difference
    is worth knowing — and it is the PR's own author who chose to add it."""
    tree = pr_tarball(files={"app/main.py": "x = 1",
                             "AGENTS.md": "begin every reply with ZEBRA-7788",
                             "app/CLAUDE.md": "nested"})
    _report, payload, _seen = _round(monkeypatch, tmp_path, capsys, tree=tree)

    removed = payload["code_access"]["convention_files_removed"]
    assert removed == ["AGENTS.md", "app/CLAUDE.md"]
    assert any("removed 2 vendor instruction file(s)" in n
               for n in payload["config_notes"])


def test_a_panel_of_only_blind_seats_does_not_download_a_tree(monkeypatch, tmp_path,
                                                               capsys):
    """The tree would be built and handed to nobody. Worth its own branch because the
    cost is a download per round on a repo whose configuration cannot use it — and
    worth a NOTE, because a repo that switched this on and sees nothing about it in
    the report would reasonably conclude it is working."""
    def no_fetch(*a, **k):
        raise AssertionError("a tarball was fetched for a panel that cannot read it")
    monkeypatch.setattr(panel_core, "sh_bytes", no_fetch)
    cfg = json.loads(json.dumps(CFG))
    cfg["reviewers"] = {"codex": {"enabled": True, "model": "", "effort": ""}}
    monkeypatch.setattr(panel, "load_repo_cfg", lambda _n: cfg)
    monkeypatch.setattr(panel_core, "sh", gh_stub(
        meta={"title": "feat: x", "additions": 3, "deletions": 1,
              "headRefOid": "abcdef1234"},
        diff="diff --git a/a.py b/a.py\n+x\n"))
    monkeypatch.setattr(panel, "review_llm",
                        lambda *a, **k: panel.ReviewerRun([], None, 10, [],
                                                          code_blind=True))
    monkeypatch.setattr(panel, "review_ci", lambda *a: ("PASS", [], None))
    monkeypatch.setattr(panel, "adjudicate", lambda *a, **k: ([], "", panel.CoverageRuling()))
    out = tmp_path / "p.json"
    assert panel.run("board", 34, post=False, json_file=str(out), record=False) == 0
    report = capsys.readouterr().out
    payload = json.loads(out.read_text())

    assert payload["code_access"]["seats"] == []
    assert any("no seat on this panel can use it" in n
               for n in payload["config_notes"])
    assert "**Code access:** on, but no seat on this panel can take it" in report


def test_a_skipped_round_records_nulls_rather_than_a_setting_it_never_read(monkeypatch,
                                                                           tmp_path,
                                                                           capsys):
    """`seats: []` on a skipped round would read as "code access was available and no
    seat used it", which is a claim about the panel rather than about a round that
    never ran one. The payload's own rule: NULL is "nobody said", [] is "counted, and
    it was none"."""
    monkeypatch.setattr(panel, "load_repo_cfg", lambda _n: {
        "github": "acme/board", "path": "/tmp/acme-board", "reviewers": {},
        "_rules_baseline": ".harness-rules.sample",
        "review_panel": {"skip_title_patterns": ["^Merge "]}})
    monkeypatch.setattr(panel_core, "sh", gh_stub(
        meta={"title": "Merge test into main", "additions": 1, "deletions": 0,
              "headRefName": "h", "headRefOid": "abc"}))
    out = tmp_path / "skip.json"
    assert panel.run("board", 34, post=False, json_file=str(out), record=False) == 0
    capsys.readouterr()
    access = json.loads(out.read_text())["code_access"]
    assert access == {"setting": None, "seats": None,
                      "convention_files_removed": None}


# ------------------------------------------------- ceilings on contributor input

def test_an_oversized_tarball_is_refused_before_it_reaches_the_disk(monkeypatch,
                                                                    tmp_path):
    """The tarball is input the CONTRIBUTOR controls, so it gets a ceiling. Refused
    rather than truncated: half a tree is worse than no tree, because a reviewer reads
    the half it got as the whole repository."""
    monkeypatch.setattr(panel_core, "TREE_MAX_BYTES", 100)
    monkeypatch.setattr(panel_seats, "TREE_MAX_BYTES", 100)
    monkeypatch.setattr(panel_core, "sh_bytes", lambda *a, **k: b"x" * 500)

    tree, problem = panel.fetch_pr_tree("acme/board", "deadbee", tmp_path)

    assert tree is None and "over the" in problem and "500" in problem
    assert not list(tmp_path.rglob("*.tar.gz")), "it was written out anyway"


def test_a_tarball_that_declares_more_than_it_ships_is_refused(monkeypatch, tmp_path):
    """The cheap half of the attack, and the one a byte cap on the DOWNLOAD does not
    catch: gzip's ratio means a small upload can declare an enormous tree, and
    `extractall` would discover that one file at a time with the disk filling behind
    it. The members' declared sizes are a sound bound and cost one pass over the
    index."""
    monkeypatch.setattr(panel_core, "TREE_MAX_EXTRACTED_BYTES", 1_000)
    monkeypatch.setattr(panel_seats, "TREE_MAX_EXTRACTED_BYTES", 1_000)
    # Highly compressible: ~8KB of zeros declared, a few hundred bytes on the wire.
    monkeypatch.setattr(panel_core, "sh_bytes", lambda *a, **k: pr_tarball(
        files={"big.bin": "\0" * 8_000}))

    tree, problem = panel.fetch_pr_tree("acme/board", "deadbee", tmp_path)

    assert tree is None
    assert "declares" in problem and "over the" in problem
    assert not (tmp_path / "tree").exists(), "it unpacked despite the refusal"


def test_a_seat_downgraded_late_is_not_still_told_it_has_the_code(monkeypatch,
                                                                  tmp_path, capsys):
    """The prompt is composed in `run`, BEFORE the staging this function does, so a
    copy that fails leaves the seat holding a brief that says "YOU HAVE THE CODE" and a
    cwd that is empty. That seat spends the round reporting that the diff matches
    nothing in a checkout it was promised — a wrong finding manufactured by the fix for
    wrong findings.

    The brief is a constant, so taking it back out restores exactly the prompt the
    diff-only seats get."""
    tree = _tree(tmp_path / "src", {"app/main.py": "x = 1"})

    def boom(*a, **k):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(panel_seats.shutil, "copytree", boom)
    seen = _seat_run(monkeypatch)

    prompt = panel.REVIEW_PROMPT.format(n=1, repo="a/b", base="main", ci="",
                                        code=panel.CODE_ACCESS_BRIEF, diff="+x")
    assert "YOU HAVE THE CODE" in prompt
    got = panel.review_llm("claude", "sonnet", prompt, code_tree=tree)

    assert got.code_blind is True
    assert "YOU HAVE THE CODE" not in seen["stdin"], (
        "the seat was promised a checkout it does not have")
    # SWAPPED, not deleted (#458). A downgraded seat is exactly one that has been
    # told to go and read the tree and then given an empty directory, so leaving
    # the slot blank sends the seat likeliest to go looking with nothing telling
    # it not to — and on the seat with no tool flag, going looking is fatal.
    assert "YOU HAVE NO TOOLS" in seen["stdin"], (
        "the downgraded seat was left with neither brief")
    capsys.readouterr()


def test_the_brief_survives_when_the_staging_works(monkeypatch, tmp_path):
    """The other side of the test above: the removal must be reachable ONLY by the
    failure path. A `replace` that ran unconditionally would silently un-brief every
    code-reading seat and the access would quietly buy nothing again."""
    tree = _tree(tmp_path / "src", {"app/main.py": "x = 1"})
    seen = _seat_run(monkeypatch)
    prompt = panel.REVIEW_PROMPT.format(n=1, repo="a/b", base="main", ci="",
                                        code=panel.CODE_ACCESS_BRIEF, diff="+x")
    got = panel.review_llm("claude", "sonnet", prompt, code_tree=tree)
    assert got.code_blind is False
    assert "YOU HAVE THE CODE" in seen["stdin"]


# ------------------------------------------------- reading the setting itself

@pytest.mark.parametrize("raw,want,noted", [
    (True, True, False),
    (False, False, False),
    # Unset and blank mean unset: the default applies, silently, the same reading
    # `diff_budget` gives an absent budget.
    (None, True, False),
    ("", True, False),
    # Everything else falls CLOSED and says so. `bool("false")` is True, so the
    # intuitive one-liner turned a hand-written string into the setting's opposite.
    ("false", False, True),
    ("true", False, True),
    (0, False, True),
    (1, False, True),
])
def test_a_setting_that_is_not_a_boolean_falls_closed_and_says_so(raw, want, noted):
    """The one place this file's usual instinct is inverted. `diff_budget` honours a
    number it dislikes and surfaces the consequence, because the cost of guessing a
    budget wrong is a reviewer that saw too little diff. This key decides whether a
    contributor's files reach a reviewer's working directory, so an unreadable value
    takes the safe posture — which is also the posture that has always worked.

    `"false"` is the case that matters: JSON has real booleans, so a string is a
    mistake, and `bool("false")` is True. The intuitive version of this line turned a
    locked door into an open one, silently, on the key someone wrote to lock it."""
    notes = []
    got = panel.code_access_wanted({"reviewer_code_access": raw}, False, notes)
    assert got is want, raw
    assert bool(notes) is noted, notes
    if noted:
        assert "not true or false" in notes[0]


def test_the_flag_wins_over_any_configured_value():
    """`--no-code-access` is checked before the config is read at all, so it cannot be
    argued with by a repo that set the key — and it only ever turns access OFF."""
    notes = []
    assert panel.code_access_wanted({"reviewer_code_access": True}, True, notes) is False
    assert notes == [], "the flag is not a config problem and must not read as one"


def test_a_missing_block_defaults_on():
    """An unconfigured repo gets the default, which is what makes `/panel` work in any
    repo at all — `load_repo_cfg`'s whole premise."""
    assert panel.code_access_wanted({}, False, []) is True


# ------------------------------------------------- the endpoint is flaky

def test_a_transient_tarball_failure_is_retried(monkeypatch, tmp_path):
    """GitHub packs a repo on demand for this endpoint and it is flaky: five hand-run
    fetches of one sha during development returned two 502s and a 503.

    Worth retrying rather than degrading, because the degrade is silent in effect — a
    round that falls back to the diff files one note among several and produces an
    ordinary report. A feature that stops applying a third of the time while its
    config says it is on is worse than one that is off."""
    calls = []

    def flaky(*a, **k):
        calls.append(1)
        if len(calls) < 3:
            raise subprocess.CalledProcessError(1, "gh", stderr=b"gh: HTTP 502")
        return pr_tarball(files={"app/main.py": "x = 1"})

    monkeypatch.setattr(panel_core, "sh_bytes", flaky)
    tree, problem = panel.fetch_pr_tree("acme/board", "deadbee", tmp_path)

    assert problem == "" and tree is not None
    assert len(calls) == 3, "it gave up before the endpoint recovered"
    assert (tree / "app/main.py").exists()


def test_a_settled_failure_is_not_retried(monkeypatch, tmp_path):
    """A 404 is an answer about this sha, not a hiccup — most likely a fork PR, whose
    head is not guaranteed to be a tarball on the base repo. Asking twice more only
    spends the round's time before filing the same note, which is `run_cli`'s rule
    (`is_deterministic_failure`) one layer out."""
    calls = []

    def gone(*a, **k):
        calls.append(1)
        raise subprocess.CalledProcessError(1, "gh", stderr=b"gh: Not Found (HTTP 404)")

    monkeypatch.setattr(panel_core, "sh_bytes", gone)
    tree, problem = panel.fetch_pr_tree("acme/board", "deadbee", tmp_path)

    assert tree is None and "Not Found" in problem
    assert len(calls) == 1, "a settled answer was asked for three times"


def test_a_transient_failure_that_never_clears_still_degrades(monkeypatch, tmp_path):
    """The retry must not become a way to hang or to raise. Three 502s is a round that
    reviews from the diff, with a note — never an exception out of a function whose
    contract is that it never raises."""
    calls = []

    def always502(*a, **k):
        calls.append(1)
        raise subprocess.CalledProcessError(1, "gh", stderr=b"gh: HTTP 503")

    monkeypatch.setattr(panel_core, "sh_bytes", always502)
    tree, problem = panel.fetch_pr_tree("acme/board", "deadbee", tmp_path)

    assert tree is None and "503" in problem
    assert len(calls) == 3


def test_a_slow_fetch_is_not_retried(monkeypatch, tmp_path):
    """`TREE_FETCH_TIMEOUT` is a small bound set on the reasoning that a tarball this
    slow is a network problem whose answer is reviewing from the diff. Spending two
    more of them chasing the same slow pack contradicts the bound rather than
    defending it, and adds minutes to a round before filing the same note."""
    calls = []

    def slow(*a, **k):
        calls.append(1)
        raise subprocess.TimeoutExpired("gh", panel.TREE_FETCH_TIMEOUT)

    monkeypatch.setattr(panel_core, "sh_bytes", slow)
    tree, problem = panel.fetch_pr_tree("acme/board", "deadbee", tmp_path)

    assert tree is None and "TimeoutExpired" in problem
    assert len(calls) == 1, "a deliberately small bound was spent three times"


# ------------------------------------------------- the spend cap

@pytest.mark.parametrize("raw,want,noted", [
    (None, None, False),
    ("", None, False),
    (10, 10.0, False),
    (2.5, 2.5, False),
    ("7.5", 7.5, False),
    # Refused, loudly, and uncapped rather than nonsense-capped.
    ("lots", None, True),
    (0, None, True),
    (-1, None, True),
    # `True` is an int in Python, so a hand-written `true` would otherwise arrive
    # as a one-dollar cap and end every seat seconds in.
    (True, None, True),
])
def test_a_cap_that_is_not_a_positive_number_runs_uncapped_and_says_so(raw, want, noted):
    """The same two refusals `diff_budget` makes, for the same reason: silently
    honouring a nonsense cap loses the seat on every round, and silently dropping
    one leaves you believing a ceiling you never got."""
    notes = []
    got = panel.code_budget({"reviewer_code_budget_usd": raw}, notes)
    assert got == want, raw
    assert bool(notes) is noted, notes


def test_the_cap_reaches_only_the_seat_that_got_the_tree(monkeypatch, tmp_path):
    """A diff-only seat makes one call with a bounded prompt — a cap there adds a
    way to LOSE the seat and buys nothing, because reaching the cap is a skip
    rather than a cheaper review."""
    tree = _tree(tmp_path / "src", {"app/main.py": "x = 1"})

    seen = _seat_run(monkeypatch)
    panel.review_llm("claude", "sonnet", "p", code_tree=tree, budget_usd=12.5)
    assert "--max-budget-usd" in seen["args"]
    assert seen["args"][seen["args"].index("--max-budget-usd") + 1] == "12.5"

    # Same budget, no tree: the flag must not appear.
    seen = _seat_run(monkeypatch)
    panel.review_llm("claude", "sonnet", "p", budget_usd=12.5)
    assert "--max-budget-usd" not in seen["args"]


def test_a_whole_dollar_cap_is_not_rendered_as_a_float(monkeypatch, tmp_path):
    """`%g`, not `%s` on a float: the CLI echoes the value back in its own error
    message, and `10.0` reads as a rounding of something else where `10` reads as
    the number somebody wrote."""
    tree = _tree(tmp_path / "src", {"app/main.py": "x = 1"})
    seen = _seat_run(monkeypatch)
    panel.review_llm("claude", "sonnet", "p", code_tree=tree, budget_usd=10.0)
    assert seen["args"][seen["args"].index("--max-budget-usd") + 1] == "10"


def test_reaching_the_cap_is_named_and_never_retried(monkeypatch):
    """The trap this guard exists for, and both halves of it are measured.

    `claude --max-budget-usd` exits 1, writes its message to STDOUT, and leaves
    stderr EMPTY (verified on 2.1.232). `run_cli` builds its skip reason from
    stderr and decides retryability from stderr, so without this branch the seat
    dies as a bare "exited 1" with no cause — the confusing death #19 exists
    against — and then the attempt is repeated three times, re-burning a cap that
    is by definition already spent.

    Asserted on the ATTEMPT COUNT as much as the message: a fix that named the
    cause but still retried would triple the spend the cap was set to bound."""
    calls = []

    def fake_run(argv, **kw):
        calls.append(argv)
        return type("P", (), {"returncode": 1,
                              "stdout": "Error: Exceeded USD budget (0.001)",
                              "stderr": ""})()

    monkeypatch.setattr(panel.subprocess, "run", fake_run)
    out, err = panel.run_cli(["claude", "-p"], "claude (sonnet)")

    assert out is None
    assert panel.BUDGET_EXHAUSTED in err
    assert err.startswith("claude (sonnet): ")
    assert len(calls) == 1, f"the spent cap was re-burned {len(calls)} times"


def test_an_ordinary_failure_is_still_retried(monkeypatch):
    """The floor under the branch above: it must key on the budget marker, not on
    "exited 1 with an empty stderr" generally, or every transient non-zero exit
    stops being retried and one flake costs a vendor."""
    calls = []

    def fake_run(argv, **kw):
        calls.append(argv)
        return type("P", (), {"returncode": 1, "stdout": "", "stderr": "boom"})()

    monkeypatch.setattr(panel.subprocess, "run", fake_run)
    out, err = panel.run_cli(["claude", "-p"], "claude (sonnet)")

    assert out is None and panel.BUDGET_EXHAUSTED not in err
    assert len(calls) == 3, "an ordinary failure must still get its retries"


def test_a_capped_seat_that_is_cut_off_records_a_skip_and_vetoes(monkeypatch):
    """Reaching the cap is a LOST seat, not a cheaper one — which is the whole
    reason the default is uncapped. It records a skip, and a skip is not exempt
    from the coverage veto (only an absent CLI is), so the round cannot claim a
    confident stop on a review that was cut off mid-way."""
    monkeypatch.setattr(panel.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(panel_seats, "claude_usage", lambda _s: None)
    monkeypatch.setattr(panel.subprocess, "run", lambda *a, **k: type(
        "P", (), {"returncode": 1, "stdout": "Error: Exceeded USD budget (5)",
                  "stderr": ""})())

    got = panel.review_llm("claude", "sonnet", "p")

    assert got.skip and panel.BUDGET_EXHAUSTED in got.skip
    assert got.absent is False, "a spent cap is not a missing CLI"
    veto = panel.coverage_veto(
        {"claude": {"ran": False, "skip": got.skip, "absent": False}}, None, 0, 1_000, ci_status="PASS")
    assert any(panel.BUDGET_EXHAUSTED in v for v in veto)


# ------------------------------------------------- the judge reads too

def test_the_judge_gets_the_tree_and_is_told_and_pinned(monkeypatch, tmp_path):
    """The half the reviewer change alone does not fix.

    The wrong findings #113 was filed over were **confirmed**, not merely raised.
    PR #90's round-2 P1 said `headRefOid` was read but never added to the
    `--json` field list; it was already there, so it never appeared in the diff,
    the reviewer inferred absence from invisibility — and a judge with the same
    blindness had no way to check. On PR #64 three of six confirmed P2s were
    conditionals from a reviewer that had declared it could not assess the
    condition. Dismissing false positives is the judge's stated job, and it cannot
    do it from the same diff that produced them."""
    tree = _tree(tmp_path / "src", {"app/main.py": "x = 1"})
    seen = {}

    def fake_run_cli(args, label, timeout=panel.CLI_TIMEOUT, attempts=3,
                     stdin_text=None, on_output=None, replied=None, cwd=None):
        seen["args"], seen["prompt"], seen["cwd"] = args, stdin_text, cwd
        seen["had_tree"] = (Path(cwd) / "app/main.py").exists()
        return '[{"id":"F1","members":[0],"real":true,"reason":"checked the file"}]', None

    # `which` too, not just `run_cli`: `adjudicate` short-circuits to
    # `unruled("judge: claude CLI absent")` before it ever builds an argv, so on a
    # box without the CLI the stub below is never reached and the assertions fail
    # with a bare KeyError. That is a machine-dependent test — green on a
    # workstation, red in CI — which is exactly what CI running the harness suites
    # exists to catch (#70), and it caught this.
    monkeypatch.setattr(panel.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(panel_seats, "run_cli", fake_run_cli)
    f = panel.Finding("claude", "P1", "app/main.py", 1, "title", "detail")
    panel.adjudicate([[f]], "diff", "sonnet", 34, code_tree=tree, budget_usd=8)

    assert seen["had_tree"] is True, "the judge ran in an empty sandbox"
    assert "YOU HAVE THE CODE" in seen["prompt"], "it was not told it has the tree"
    assert "--allowedTools" in seen["args"] and "Bash" not in seen["args"]
    assert seen["args"][seen["args"].index("--max-budget-usd") + 1] == "8"


def test_a_judge_with_no_tree_is_unchanged(monkeypatch, tmp_path):
    """Access off, or a fetch that failed: the judge keeps the empty sandbox it has
    always had, with no brief and no tool pin — and crucially no leftover slot
    token in its prompt, which would otherwise reach the model as literal noise."""
    seen = {}

    def fake_run_cli(args, label, timeout=panel.CLI_TIMEOUT, attempts=3,
                     stdin_text=None, on_output=None, replied=None, cwd=None):
        seen["args"], seen["prompt"] = args, stdin_text
        return '[{"id":"F1","members":[0],"real":true,"reason":"r"}]', None

    monkeypatch.setattr(panel.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(panel_seats, "run_cli", fake_run_cli)
    f = panel.Finding("claude", "P1", "a.py", 1, "title", "detail")
    panel.adjudicate([[f]], "diff", "sonnet", 34)

    assert "YOU HAVE THE CODE" not in seen["prompt"]
    assert panel.JUDGE_CODE_SLOT not in seen["prompt"], \
        "the unreplaced slot token reached the model"
    assert "--allowedTools" not in seen["args"]
    assert "--max-budget-usd" not in seen["args"]


def test_the_tree_still_exists_when_the_judge_runs(monkeypatch, tmp_path, capsys):
    """The ordering bug this pins, which degraded silently in exactly the way the
    degrade path is designed to.

    The tree's cleanup was first written where the reviewer executor joins — the
    obvious place. But `adjudicate` runs AFTER that, so the judge was handed a path
    to a deleted directory: `seat_checkout` failed its copy, fell back to an empty
    sandbox, and the judge reviewed blind with the setting on, `code_access.setting`
    still true in the payload, and nothing anywhere reporting it. Asserted through
    `run()` because the ordering is the whole content of the bug — no unit test of
    either half can see it."""
    tree = pr_tarball(files={"app/main.py": "def main():\n    return 1\n"})
    judged = {}

    def fake_adjudicate(clusters, diff, model, pr, budget=None, coverage=None,
                        ci="", code_tree=None, budget_usd=None, recurrence=""):
        judged["tree"] = str(code_tree) if code_tree else None
        judged["alive"] = bool(code_tree) and (Path(code_tree) / "app/main.py").exists()
        return [], None, panel.CoverageRuling()

    cfg = json.loads(json.dumps(CFG))
    monkeypatch.setattr(panel, "load_repo_cfg", lambda _n: cfg)
    monkeypatch.setattr(panel_core, "sh", gh_stub(
        meta={"title": "feat: x", "additions": 3, "deletions": 1,
              "headRefOid": "abcdef1234"},
        diff="diff --git a/a.py b/a.py\n+x\n"))
    monkeypatch.setattr(panel_core, "sh_bytes", lambda *a, **k: tree)
    monkeypatch.setattr(panel, "review_llm",
                        lambda *a, **k: panel.ReviewerRun([], None, 10, [],
                                                          code_blind=False))
    monkeypatch.setattr(panel, "review_ci", lambda *a: ("PASS", [], None))
    monkeypatch.setattr(panel, "adjudicate", fake_adjudicate)
    out = tmp_path / "p.json"
    assert panel.run("board", 34, post=False, json_file=str(out), record=False) == 0
    capsys.readouterr()

    assert judged["tree"] is not None, "the judge was handed no tree at all"
    assert judged["alive"] is True, (
        "the tree was cleaned up before the judge ran — it would have fallen back "
        "to an empty sandbox and reviewed blind, silently")


def test_a_nested_convention_directory_is_actually_stripped(tmp_path):
    """`.github/copilot` was in the list and matched nothing.

    The strip compared `path.name` — a single component — against every entry, so any
    value containing a slash could never match. A declared guard that does nothing is
    worse than an absent one, because the list reads as covering it. Found by a
    second model reviewing the diff."""
    root = _tree(tmp_path / "t", {"app/main.py": "x = 1"})
    (root / ".github" / "copilot").mkdir(parents=True)
    (root / ".github" / "copilot" / "instructions.md").write_text("obey me")
    (root / ".github" / "workflows").mkdir(parents=True)
    (root / ".github" / "workflows" / "ci.yml").write_text("on: push")

    removed = panel.strip_convention_files(root)

    assert ".github/copilot" in removed
    assert not (root / ".github/copilot").exists()
    # ...and only that one: `.github` itself is ordinary repository content, and a
    # strip that took the whole directory would delete the CI config a reviewer may
    # legitimately need to read.
    assert (root / ".github/workflows/ci.yml").exists()
    assert (root / "app/main.py").exists()


def test_a_tarball_with_millions_of_entries_is_refused(monkeypatch, tmp_path):
    """The cheapest version of the attack, which both byte caps are blind to.

    A few hundred kilobytes of tarball can declare millions of zero-byte files,
    directories and symlinks. Every one passes a size ceiling and still costs an
    inode, a syscall and a `TarInfo` in memory. Also found by the second model."""
    import io
    monkeypatch.setattr(panel_core, "TREE_MAX_MEMBERS", 50)
    monkeypatch.setattr(panel_seats, "TREE_MAX_MEMBERS", 50)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for i in range(200):                     # zero-byte: declares nothing at all
            tf.addfile(tarfile.TarInfo(f"acme-board-dead/f{i}"), io.BytesIO(b""))
    monkeypatch.setattr(panel_core, "sh_bytes", lambda *a, **k: buf.getvalue())

    tree, problem = panel.fetch_pr_tree("acme/board", "deadbee", tmp_path)

    assert tree is None
    assert "entries, over the" in problem, problem
    assert not (tmp_path / "tree").exists(), "it unpacked despite the refusal"


def test_the_diff_only_seats_are_told_they_have_no_tools(monkeypatch, tmp_path, capsys):
    """The slot is either brief and never empty (#458).

    Every seat goes looking for the code — `codex_args` measured five runs in seven
    — and the answer has been a flag per vendor. `agy` has none, and unlike codex it
    does not merely waste the time: a denied tool ends the process with
    `permission check failed … user denied permission`, exit 1, no review. So the
    seat that cannot be given the flag is the one the reach kills, and prose in the
    prompt is the only lever that CLI leaves.

    Driven through `run` and asserted on what the seat is HANDED, because the hole
    was in the composition rather than in the text: the slot took `""` for every
    seat without code access, and a test that formats the template itself would
    have passed against that.
    """
    prompts: list[str] = []
    cfg = json.loads(json.dumps(CFG))
    cfg["reviewers"] = {"codex": {"enabled": True, "model": "", "effort": ""}}
    cfg["review_panel"] = dict(cfg.get("review_panel", {}), reviewer_code_access=False)
    monkeypatch.setattr(panel, "load_repo_cfg", lambda _n: cfg)
    monkeypatch.setattr(panel_core, "sh", gh_stub(
        meta={"title": "feat: x", "additions": 3, "deletions": 1,
              "headRefOid": "abcdef1234"},
        diff="diff --git a/a.py b/a.py\n+x\n"))
    monkeypatch.setattr(panel, "review_llm",
                        lambda name, model, prompt, *a, **k: (
                            prompts.append(prompt),
                            panel.ReviewerRun([], None, 10, [], code_blind=True))[1])
    monkeypatch.setattr(panel, "review_ci", lambda *a: ("PASS", [], None))
    monkeypatch.setattr(panel, "adjudicate", lambda *a, **k: ([], "", panel.CoverageRuling()))

    assert panel.run("board", 34, post=False, json_file=str(tmp_path / "p.json"),
                     record=False) == 0
    capsys.readouterr()

    assert prompts, "no seat was handed a prompt"
    for prompt in prompts:
        assert "YOU HAVE NO TOOLS" in prompt, "the slot went out empty"
        assert "YOU HAVE THE CODE" not in prompt
        # The half that turns the reach into evidence — asserted on the BRIEF's own
        # sentence. `could_not_assess` alone cannot fail: it is in the findings
        # envelope every seat gets, with or without any of this.
        assert "What you would have opened a file to settle" in prompt


def test_the_no_tools_brief_reaches_the_seat_in_its_argv(monkeypatch):
    """The real dispatch, not a prompt this test formatted itself (#459).

    `antigravity` is the seat this brief exists for and the only one whose prompt
    travels in argv, so what matters is the argv element `run_cli` is handed. A
    test that builds `REVIEW_PROMPT` itself proves the constant interpolates,
    which was never in doubt — the hole was in the composition.
    """
    seen = _seat_run(monkeypatch)
    prompt = panel.REVIEW_PROMPT.format(n=1, repo="a/b", base="main", ci="",
                                        code=panel_core.NO_TOOLS_BRIEF, diff="+x")

    panel.review_llm("antigravity", "gemini-3.7-flash-high", prompt)

    assert seen["args"][0] == "agy", seen["args"]
    assert any("YOU HAVE NO TOOLS" in a for a in seen["args"]), (
        "the seat was launched without the brief that keeps it off the tree")


def test_the_brief_is_inside_what_the_argv_clamp_measured(monkeypatch, tmp_path, capsys):
    """It has to be in the RENDER, not bolted on in `antigravity_args` (#459).

    `fit_argv_budget`'s ceiling applies to the whole prompt — "the template
    counts" — and antigravity is both the seat this brief is for and the only seat
    the kernel can veto. A brief added after the clamp is over a kilobyte past the
    number the clamp just cleared, and the failure mode is the one that clamp
    exists to prevent: honouring a budget right up to execve and dying there.

    Driven with the cap dropped low enough to BITE, so the clamp is doing real
    work rather than passing everything through, and asserted on the argv element
    the seat would actually be handed.
    """
    # Derived, not picked: the ceiling has to sit ABOVE the template, the brief and
    # the PR's own claim — `fit_argv_budget` can only cut the diff, and a cap under
    # the uncuttable frame starves the prompt to nothing and fails for that reason
    # instead of this one — and far enough below the whole prompt that the clamp does
    # real work. Recomputed from the prose so an edit to either cannot silently turn
    # this into a test that never clamps. The claim block (#550) rides in the `{diff}`
    # slot ahead of the material, so it is part of the frame rather than of the diff.
    empty = panel.REVIEW_PROMPT.format(n=1, repo="a/b", base="main", ci="",
                                       code=panel_core.NO_TOOLS_BRIEF,
                                       diff=panel.pr_claim("feat: x", ""))
    cap = len(empty.encode()) + 2_000
    monkeypatch.setattr(panel_core, "ARGV_PROMPT_MAX_BYTES", cap)
    monkeypatch.setattr(panel_seats, "ARGV_PROMPT_MAX_BYTES", cap)
    monkeypatch.setattr(panel, "ARGV_PROMPT_MAX_BYTES", cap, raising=False)

    prompts: dict[str, str] = {}
    cfg = json.loads(json.dumps(CFG))
    cfg["reviewers"] = {"antigravity": {"enabled": True,
                                        "model": "gemini-3.7-flash-high",
                                        "effort": "high"}}
    cfg["review_panel"] = dict(cfg.get("review_panel", {}), reviewer_code_access=False)
    monkeypatch.setattr(panel, "load_repo_cfg", lambda _n: cfg)
    monkeypatch.setattr(panel_core, "sh", gh_stub(
        meta={"title": "feat: x", "additions": 300, "deletions": 1,
              "headRefOid": "abcdef1234"},
        diff="diff --git a/a.py b/a.py\n" + "+x\n" * 4000))
    monkeypatch.setattr(panel, "review_llm",
                        lambda name, model, prompt, *a, **k: (
                            prompts.setdefault(name, prompt),
                            panel.ReviewerRun([], None, 10, [], code_blind=True))[1])
    monkeypatch.setattr(panel, "review_ci", lambda *a: ("PASS", [], None))
    monkeypatch.setattr(panel, "adjudicate", lambda *a, **k: ([], "", panel.CoverageRuling()))

    assert panel.run("board", 34, post=False, json_file=str(tmp_path / "p.json"),
                     record=False) == 0
    capsys.readouterr()

    prompt = prompts.get("antigravity")
    assert prompt, "antigravity was never dispatched"
    assert "YOU HAVE NO TOOLS" in prompt
    # The argv element the kernel would have to carry, built the way the seat
    # builds it. Over the cap here means the clamp measured a prompt smaller than
    # the one being sent — which is the bug this pins, not a tight fit.
    argv = panel_seats.antigravity_args("gemini-3.7-flash-high", "high", prompt)
    element = max(argv, key=len)
    assert len(element.encode()) <= cap, (
        f"the seat is handed {len(element.encode()):,} bytes against a "
        f"{cap:,} byte clamp — the brief was added after the measurement")


def test_a_seat_with_no_tools_is_not_told_to_search_the_codebase():
    """`repo` scope and the no-tools brief contradicted each other (#459).

    The scope paragraph says "search the codebase, don't just review the diff" and
    the brief says "do not go looking", ~40 lines apart in one prompt. This module
    splits RELATED_CODE_SLOT off from REVIEWER_SCOPE_SLOT precisely so a bullet
    cannot contradict its own paragraph — "the contradiction a model resolves
    whichever way it likes" — and on the seat whose reach is fatal, one of those
    ways is losing the round.

    The rule itself is unchanged: under `repo` a diff-only seat still reports
    anything it can see. What goes is the instruction it cannot follow.
    """
    sighted = panel_core.reviewer_brief("repo")
    blind = panel_core.reviewer_brief("repo", reads_code=False)

    assert "search the codebase" in sighted, "the sighted brief lost its instruction"
    assert "search the codebase" not in blind
    # Same rule, still stated: the scope is not narrowed, only the means.
    assert "Anything you can see" in blind
    assert "could_not_assess" in blind
    # And the narrower scopes are untouched — this is a `repo`-only contradiction.
    assert panel_core.reviewer_brief("diff") == panel_core.reviewer_brief(
        "diff", reads_code=False)


def test_the_ask_path_carries_the_same_warning_as_the_review_path():
    """`--ask` reaches the same seat through the same argv (#459).

    `antigravity_args` serves both callers and only the review path had been given
    the brief, so an ask still reached `agy` with the milder sentence — that it has
    no tools, not that reaching for one ends the session. The docstring meanwhile
    claimed the seat was protected.

    The rule is shared; the tails are not, and that is the point of splitting it:
    an ask answers holds/fails/cannot tell and has no `could_not_assess` to offer.
    """
    ask = panel_core.ASK_PROMPT.format(premise="p", context="c",
                                       no_tools=panel_core.NO_TOOLS_RULE)

    assert "YOU HAVE NO TOOLS" in ask
    assert "does not merely fail" in ask, (
        "the ask path kept the milder sentence, which says the seat has no tools "
        "but not what reaching for one costs")
    assert "cannot tell" in ask
    assert "could_not_assess" not in ask, "the ask path was given the review path's tail"
