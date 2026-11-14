"""The antigravity seat with a shell: `reviewers.antigravity.tools` and SEAT_RUNS_CODE.

In the no-tools mode a model that ignored NO_TOOLS_BRIEF and probed `echo …` had the
command auto-denied by agy's headless print mode, and that denial ended the session
with exit 0 and no output — the seat lost for #853's round. The owner's call
(2026-11-14) was to let review agents run tools, so the seat now reviews from its own
stripped copy of the PR's tree with `--dangerously-skip-permissions`.

What these hold down:

**The flag and the tree arrive together, or neither does.** A shell with an empty
sandbox is the old seat plus a risk; a tree with no flag is the old seat's fatal
soft-deny with more to tempt it. A copy that fails to stage takes both back.

**The cwd is a copy, never the tree the round fetched and never a real worktree.**

**The overlay is on every executing run.** The tarball copy has no `.env`, so a test
the seat runs falls back to defaults — a live database on this box — unless the
database variables point at nothing.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import harness_rules  # noqa: E402
import panel  # noqa: E402
import panel_core  # noqa: E402
import panel_seats  # noqa: E402
from conftest import gh_stub  # noqa: E402


def _tree(root: Path, files: dict) -> Path:
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
    return root


def _seat_run(monkeypatch):
    """One seat run, recording what run_cli was handed while the cwd still exists."""
    seen = {}
    monkeypatch.setattr(panel.shutil, "which", lambda name: "/usr/bin/" + name)

    def fake_run_cli(args, label, timeout=panel.CLI_TIMEOUT, attempts=3,
                     stdin_text=None, on_output=None, replied=None, cwd=None,
                     env_extra=None):
        seen["cwd"] = cwd
        seen["args"] = args() if callable(args) else args
        seen["had_code"] = (Path(cwd) / "app/main.py").exists()
        seen["env"] = env_extra
        if env_extra:
            gh = Path(env_extra["GH_CONFIG_DIR"])
            seen["gh_empty"] = gh.is_dir() and not any(gh.iterdir())
        return "[]", None

    monkeypatch.setattr(panel_seats, "run_cli", fake_run_cli)
    return seen


def test_the_tools_mode_adds_the_permission_switch_and_nothing_else():
    off = panel.antigravity_args("gemini-3.8-flash-high", "", "PROMPT")
    on = panel.antigravity_args("gemini-3.8-flash-high", "", "PROMPT", tools=True)
    assert "--dangerously-skip-permissions" not in off
    assert off == ["agy", "--mode", "plan", "--print-timeout", f"{panel.CLI_TIMEOUT}s",
                   "-p", "PROMPT", "--model", "gemini-3.8-flash-high"]
    assert [a for a in on if a != "--dangerously-skip-permissions"] == off


@pytest.mark.parametrize("cfg, want", [
    ({"antigravity": {"tools": True}}, True),
    ({"antigravity": {"tools": False}}, False),
    # A string that reads like a boolean must not grant a shell.
    ({"antigravity": {"tools": "true"}}, False),
    ({}, False),
    # Only a SEAT_RUNS_CODE seat can be widened in by the setting.
    ({"codex": {"tools": True}}, False),
])
def test_only_an_exact_true_on_a_runs_code_seat_grants_the_tree(cfg, want):
    got = panel_seats.tool_seats(cfg)
    assert "claude" in got
    assert ("antigravity" in got) is want
    assert "codex" not in got


def test_the_shipped_default_is_on():
    assert harness_rules.DEFAULTS["reviewers"]["antigravity"]["tools"] is True
    assert "antigravity" in panel_seats.tool_seats(harness_rules.DEFAULTS["reviewers"])


def test_a_tools_value_that_is_not_a_boolean_is_refused():
    with pytest.raises(SystemExit, match=r"antigravity\.tools` must be true or false"):
        harness_rules._check_seat_shape(
            {"reviewers": {"antigravity": {"tools": "false"}}}, ".harness-rules.sample")


def test_an_executing_seat_runs_in_its_own_copy_with_the_flag_and_overlay(monkeypatch,
                                                                         tmp_path):
    tree = _tree(tmp_path / "src", {"app/main.py": "x = 1"})
    seen = _seat_run(monkeypatch)

    got = panel.review_llm("antigravity", "m", "p", code_tree=tree, runs_code=True)

    assert seen["had_code"] is True
    assert Path(seen["cwd"]).resolve() != tree.resolve(), "ran in the shared tree"
    assert not Path(seen["cwd"]).resolve().is_relative_to(tree.resolve())
    assert "--dangerously-skip-permissions" in seen["args"]
    assert got.code_blind is False
    env = seen["env"]
    assert env and ".invalid" in env["DATABASE_URL"] and ".invalid" in env["PGHOST"]
    assert env["GH_TOKEN"] == "" and seen["gh_empty"] is True


def test_without_the_setting_the_seat_is_unchanged(monkeypatch, tmp_path):
    tree = _tree(tmp_path / "src", {"app/main.py": "x = 1"})
    seen = _seat_run(monkeypatch)

    got = panel.review_llm("antigravity", "m", "p", code_tree=tree)

    assert seen["had_code"] is False
    assert "--dangerously-skip-permissions" not in seen["args"]
    assert seen["env"] is None
    assert got.code_blind is True


def test_runs_code_cannot_hand_a_shell_to_a_seat_outside_the_set(monkeypatch, tmp_path):
    tree = _tree(tmp_path / "src", {"app/main.py": "x = 1"})
    seen = _seat_run(monkeypatch)

    got = panel.review_llm("codex", "m", "p", code_tree=tree, runs_code=True)

    assert seen["had_code"] is False and seen["env"] is None
    assert got.code_blind is True


def test_a_staging_failure_takes_back_the_flag_the_overlay_and_the_brief(monkeypatch,
                                                                         tmp_path, capsys):
    tree = _tree(tmp_path / "src", {"app/main.py": "x = 1"})

    def boom(*a, **k):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(panel_seats.shutil, "copytree", boom)
    seen = _seat_run(monkeypatch)
    prompt = "head\n" + panel_core.CODE_EXEC_BRIEF + "\ntail"

    got = panel.review_llm("antigravity", "m", prompt, code_tree=tree, runs_code=True)

    assert got.code_blind is True
    assert "--dangerously-skip-permissions" not in seen["args"]
    assert seen["env"] is None
    sent = seen["args"][seen["args"].index("-p") + 1]
    assert "AND A SHELL" not in sent and "YOU HAVE NO TOOLS" in sent
    assert "could not stage the PR tree" in capsys.readouterr().err


# --------------------------------------------------------------- through run()

CFG = {"github": "acme/board", "path": "/tmp/acme-board",
       "_rules_baseline": ".harness-rules.sample",
       "reviewers": {"claude": {"enabled": True, "model": "sonnet"},
                     "antigravity": {"enabled": True, "model": "gemini-3.8-flash-high",
                                     "effort": "", "tools": True}},
       "review_panel": {}}


def _round(monkeypatch, tmp_path, capsys, tools=True):
    seen = {"prompts": {}, "runs": {}, "trees": {}}
    cfg = json.loads(json.dumps(CFG))
    cfg["reviewers"]["antigravity"]["tools"] = tools
    monkeypatch.setattr(panel, "load_repo_cfg", lambda _n: cfg)
    monkeypatch.setattr(panel_core, "sh", gh_stub(
        meta={"title": "feat: x", "additions": 3, "deletions": 1,
              "headRefOid": "abcdef1234"},
        diff="diff --git a/a.py b/a.py\n+x\n"))
    monkeypatch.setattr(panel, "fetch_pr_tree",
                        lambda _r, _s, into: (_tree(into / "t", {"a.py": "x"}), ""))
    monkeypatch.setattr(panel.shutil, "which", lambda name: "/usr/bin/" + name)

    def reviewer(name, model, prompt, effort="", code_tree=None, budget_usd=None,
                 runs_code=False):
        seen["prompts"][name] = prompt
        seen["runs"][name] = runs_code
        seen["trees"][name] = code_tree
        return panel.ReviewerRun([], None, 10, [], code_blind=False)

    monkeypatch.setattr(panel, "review_llm", reviewer)
    monkeypatch.setattr(panel, "review_ci", lambda *a: ("PASS", [], None))
    monkeypatch.setattr(panel, "adjudicate", lambda *a, **k: ([], "", panel.CoverageRuling()))
    out = tmp_path / "p.json"
    assert panel.run("board", 34, post=False, json_file=str(out), record=False) == 0
    return capsys.readouterr().out, json.loads(out.read_text()), seen


def test_with_tools_on_the_seat_is_told_it_has_a_shell_and_the_round_says_so(
        monkeypatch, tmp_path, capsys):
    _report, payload, seen = _round(monkeypatch, tmp_path, capsys)

    assert "YOU HAVE THE CODE, AND A SHELL" in seen["prompts"]["antigravity"]
    assert seen["runs"]["antigravity"] is True
    # claude keeps its read-only brief; the shell is not offered to it.
    assert "YOU HAVE THE CODE." in seen["prompts"]["claude"]
    assert "AND A SHELL" not in seen["prompts"]["claude"]
    assert seen["runs"]["claude"] is False
    assert any("antigravity reviewed with a shell" in n
               for n in payload.get("config_notes", []))


def test_with_tools_off_the_seat_is_the_diff_only_reviewer_again(monkeypatch, tmp_path,
                                                                 capsys):
    _report, payload, seen = _round(monkeypatch, tmp_path, capsys, tools=False)

    assert "YOU HAVE NO TOOLS" in seen["prompts"]["antigravity"]
    assert "YOU HAVE THE CODE" not in seen["prompts"]["antigravity"]
    assert seen["runs"]["antigravity"] is False
    assert not any("reviewed with a shell" in n for n in payload.get("config_notes", []))
