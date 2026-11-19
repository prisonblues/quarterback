"""The antigravity seat with a shell: `reviewers.antigravity.tools` and SEAT_RUNS_CODE.

Driven through the real `review_llm` → `run_seat` → `run_cli` path with a fake `agy`
on PATH that records, from inside the run, what it was handed and where it stood.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import harness_rules  # noqa: E402
import panel  # noqa: E402
import panel_core  # noqa: E402
import panel_seats  # noqa: E402
from conftest import gh_stub  # noqa: E402

REPLY = '[{"severity": "P3", "file": "a.py", "line": 1, "title": "t", "detail": "d"}]'

#: OUT is substituted once. The record path is baked in rather than passed in the
#: environment, because a tool-running seat's environment is an allowlist.
FAKE_AGY = f"""#!{sys.executable}
import json, os, sys
stdin = sys.stdin.read()
gh = os.environ.get("GH_CONFIG_DIR")
with open(OUT, "w") as f:
    json.dump({{"argv": sys.argv[1:], "stdin": stdin, "cwd": os.getcwd(),
               "env": dict(os.environ), "had_code": os.path.exists("app/main.py"),
               "gh_empty": bool(gh) and os.listdir(gh) == []}}, f)
print(json.dumps({{"event": "result", "result": {{"status": "SUCCESS",
                                                  "response": {REPLY!r}}}}}))
"""


@pytest.fixture
def agy(tmp_path, monkeypatch):
    """A fake `agy` first on PATH, plus inherited variables the seat must not keep.
    Returns a reader for what the fake saw, or None if it never ran."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    out = tmp_path / "agy-saw.json"
    exe = bin_dir / "agy"
    exe.write_text(FAKE_AGY.replace("OUT", repr(str(out)), 1))
    exe.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    for k, v in {"SSH_AUTH_SOCK": "/run/agent.sock", "OPENAI_API_KEY": "sk-test",
                 "PGHOSTADDR": "10.0.0.1", "XDG_RUNTIME_DIR": "/run/user/1000"}.items():
        monkeypatch.setenv(k, v)
    return lambda: json.loads(out.read_text()) if out.exists() else None


def _tree(root: Path) -> Path:
    (root / "app").mkdir(parents=True)
    (root / "app/main.py").write_text("x = 1\n")
    return root


def _prompt(saw: dict) -> str:
    return json.loads(saw["stdin"])["message"]["content"]


def test_with_tools_the_seat_runs_in_its_copy_with_an_allowlisted_environment(agy, tmp_path):
    tree = _tree(tmp_path / "src")

    got = panel.review_llm("antigravity", "m", "PROMPT", code_tree=tree, runs_code=True)

    saw = agy()
    assert saw["argv"].count("--dangerously-skip-permissions") == 1
    assert saw["had_code"] is True
    assert Path(saw["cwd"]).resolve() != tree.resolve(), "ran in the shared tree"
    assert _prompt(saw).endswith(f"YOUR COPY OF THE PR IS AT: {saw['cwd']}\n")
    for gone in ("SSH_AUTH_SOCK", "OPENAI_API_KEY", "PGHOSTADDR", "XDG_RUNTIME_DIR"):
        assert gone not in saw["env"], gone
    assert ".invalid" in saw["env"]["DATABASE_URL"] and saw["gh_empty"] is True
    assert saw["env"]["QB_SANDBOX"] == "1"
    assert got.code_blind is False and len(got.findings) == 1


def test_without_tools_the_seat_is_the_diff_only_reviewer(agy, tmp_path):
    got = panel.review_llm("antigravity", "m", "PROMPT", code_tree=_tree(tmp_path / "src"))

    saw = agy()
    assert "--dangerously-skip-permissions" not in saw["argv"]
    assert saw["had_code"] is False and _prompt(saw) == "PROMPT"
    assert saw["env"]["SSH_AUTH_SOCK"] == "/run/agent.sock"
    assert got.code_blind is True


def test_runs_code_cannot_hand_a_shell_to_a_seat_outside_the_set(tmp_path, monkeypatch):
    seen = {}

    def fake_run_cli(args, label, **kw):
        seen.update(kw, args=args() if callable(args) else args)
        return "[]", None
    monkeypatch.setattr(panel.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(panel_seats, "run_cli", fake_run_cli)

    got = panel.review_llm("codex", "m", "p", code_tree=_tree(tmp_path / "src"),
                           runs_code=True)

    assert "env_extra" not in seen and got.code_blind is True


def test_a_copy_that_fails_to_stage_sits_the_seat_out(agy, tmp_path, monkeypatch):
    def boom(*a, **k):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(panel_seats.shutil, "copytree", boom)

    got = panel.review_llm("antigravity", "m", "PROMPT", code_tree=_tree(tmp_path / "src"),
                           runs_code=True)

    assert agy() is None, "agy was started"
    assert "could not stage its copy" in got.skip


def test_runs_code_env_lets_through_only_the_allowlist(tmp_path):
    base = {"PATH": "/bin", "HOME": "/h", "LANG": "C", "LC_ALL": "C", "TERM": "xterm",
            "XDG_CONFIG_HOME": "/h/.config", "NIX_SSL_CERT_FILE": "/c", "QB_SANDBOX": "1",
            "XDG_RUNTIME_DIR": "/r", "SSH_AUTH_SOCK": "/a", "AWS_ACCESS_KEY_ID": "a",
            "TEST_DATABASE_URL": "pg://x", "GEMINI_API_KEY": "g", "WHATEVER": "w"}
    env = panel_seats.runs_code_env(tmp_path, base)
    kept = {"PATH", "HOME", "LANG", "LC_ALL", "TERM", "XDG_CONFIG_HOME",
            "NIX_SSL_CERT_FILE", "QB_SANDBOX"}
    for k in base:
        assert (k in env) is (k not in kept), k
        if k not in kept:
            assert env[k] is None, k


def test_the_overlay_applies_even_without_a_cwd(monkeypatch):
    monkeypatch.setenv("SSH_AUTH_SOCK", "/inherited-agent")
    out, err = panel_seats.run_cli(
        ["sh", "-c", 'printf "%s" "${SSH_AUTH_SOCK-unset}"'], "probe", attempts=1,
        env_extra={"SSH_AUTH_SOCK": None})
    assert err is None and out == "unset"
    assert os.environ["SSH_AUTH_SOCK"] == "/inherited-agent", "mutated the panel's env"


@pytest.mark.parametrize("cfg, want", [
    ({"antigravity": {"tools": True}}, True),
    ({"antigravity": {"tools": False}}, False),
    ({"antigravity": {"tools": "true"}}, False),
    ({}, False),
    ({"codex": {"tools": True}}, False),
])
def test_only_an_exact_true_on_a_runs_code_seat_grants_the_tree(cfg, want):
    got = panel_seats.tool_seats(cfg)
    assert ("antigravity" in got) is want and "codex" not in got and "claude" in got


def test_a_tools_value_that_is_not_a_boolean_is_refused():
    with pytest.raises(SystemExit, match=r"antigravity\.tools` must be true or false"):
        harness_rules._check_seat_shape(
            {"reviewers": {"antigravity": {"tools": "false"}}}, ".harness-rules.sample")


def test_a_repo_that_never_mentions_tools_reaches_dispatch_with_it_on(tmp_path, monkeypatch):
    """Through `panel.load_repo_cfg` on a throwaway repo whose sample enables the seat
    without saying `tools`. Interactive, so its rules come from the working tree."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".harness-rules.sample").write_text(
        json.dumps({"reviewers": {"antigravity": {"enabled": True}}}))
    for cmd in (["init", "-q", "-b", "main"],
                ["remote", "add", "origin", "https://github.com/acme/board.git"],
                ["add", "."],
                ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "rules"]):
        subprocess.run(["git", "-C", str(repo), *cmd], check=True, capture_output=True)
    box = tmp_path / "box-rules.json"
    box.write_text("{}")
    monkeypatch.setenv("QUARTERBACK_HARNESS_RULES", str(box))
    monkeypatch.delenv("HARNESS_UNATTENDED", raising=False)

    rules = panel.load_repo_cfg(str(repo))["reviewers"]

    assert "antigravity" in panel_seats.tool_seats(rules)


# --------------------------------------------------------------- through run()

CFG = {"github": "acme/board", "path": "/tmp/acme-board",
       "_rules_baseline": ".harness-rules.sample",
       "reviewers": {"claude": {"enabled": True, "model": "sonnet"},
                     "antigravity": {"enabled": True, "model": "m", "effort": "",
                                     "tools": True}},
       "review_panel": {}}


def _round(monkeypatch, tmp_path, capsys, no_code_access=False):
    seen = {"prompts": {}, "runs": {}}
    monkeypatch.setattr(panel, "load_repo_cfg", lambda _n: json.loads(json.dumps(CFG)))
    monkeypatch.setattr(panel_core, "sh", gh_stub(
        meta={"title": "feat: x", "additions": 3, "deletions": 1,
              "headRefOid": "abcdef1234"},
        diff="diff --git a/a.py b/a.py\n+x\n"))
    monkeypatch.setattr(panel, "fetch_pr_tree",
                        lambda _r, _s, into: (_tree(into / "t"), ""))
    monkeypatch.setattr(panel.shutil, "which", lambda name: "/usr/bin/" + name)

    def reviewer(name, model, prompt, effort="", code_tree=None, budget_usd=None,
                 runs_code=False):
        seen["prompts"][name], seen["runs"][name] = prompt, runs_code
        sighted = code_tree is not None and name in panel_seats.tool_seats(CFG["reviewers"])
        return panel.ReviewerRun([], None, 10, [], code_blind=not sighted)

    monkeypatch.setattr(panel, "review_llm", reviewer)
    monkeypatch.setattr(panel, "review_ci", lambda *a: ("PASS", [], None))
    monkeypatch.setattr(panel, "adjudicate", lambda *a, **k: ([], "", panel.CoverageRuling()))
    out = tmp_path / "p.json"
    assert panel.run("board", 34, post=False, json_file=str(out), record=False,
                     no_code_access=no_code_access) == 0
    return capsys.readouterr().out, json.loads(out.read_text()), seen


def test_a_round_gives_the_shell_brief_to_antigravity_and_records_it(
        monkeypatch, tmp_path, capsys):
    report, payload, seen = _round(monkeypatch, tmp_path, capsys)

    assert "YOU HAVE THE CODE, AND A SHELL" in seen["prompts"]["antigravity"]
    assert seen["runs"]["antigravity"] is True
    assert "AND A SHELL" not in seen["prompts"]["claude"] and not seen["runs"]["claude"]
    assert payload["code_access"]["shell_seats"] == ["antigravity"]
    assert "(antigravity with a shell)" in report


def test_no_code_access_takes_the_shell_away(monkeypatch, tmp_path, capsys):
    report, payload, seen = _round(monkeypatch, tmp_path, capsys, no_code_access=True)

    assert seen["runs"]["antigravity"] is False
    assert "YOU HAVE NO TOOLS" in seen["prompts"]["antigravity"]
    assert payload["code_access"]["shell_seats"] == []
    assert "with a shell" not in report
