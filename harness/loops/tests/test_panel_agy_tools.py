"""The antigravity seat: its prompt on stdin, and its shell when `tools` is on.

Driven through the real `review_llm` → `run_seat` → `run_cli` path with a fake `agy`
on PATH that records what it was handed, so these hold the invocation that would
actually run rather than a double of it.
"""
from __future__ import annotations

import ast
import json
import os
import re
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

#: The output path is baked in (OUT_PATH), not passed in the environment: the seat's
#: environment is an allowlist, and a variable would not reach a tool-running run.
FAKE_AGY = f"""#!{sys.executable}
import json, os, sys
out = OUT_PATH
stdin = sys.stdin.read()
with open(out, "w") as f:
    json.dump({{"argv": sys.argv[1:], "stdin": stdin, "cwd": os.getcwd(),
               "env": dict(os.environ),
               "had_code": os.path.exists("app/main.py"),
               "gh_empty": os.listdir(os.environ.get("GH_CONFIG_DIR") or "/") == []}}, f)
status = STATUS
print(json.dumps({{"event": "init", "init": {{}}}}))
print(json.dumps({{"event": "result",
                  "result": {{"status": status, "response": {REPLY!r},
                             "error": "" if status == "SUCCESS" else "model quota spent"}}}}))
"""


def _install(bin_dir: Path, out: Path, status: str) -> None:
    exe = bin_dir / "agy"
    exe.write_text(FAKE_AGY.replace("OUT_PATH", repr(str(out)))
                   .replace("STATUS", repr(status), 1))
    exe.chmod(0o755)


@pytest.fixture
def agy(tmp_path, monkeypatch):
    """A fake `agy` first on PATH; returns a reader for what its last run saw."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    out = tmp_path / "agy-saw.json"
    _install(bin_dir, out, "SUCCESS")
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/run/agent.sock")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("PGHOSTADDR", "10.0.0.1")
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1000")
    return lambda: json.loads(out.read_text()) if out.exists() else None


def _tree(root: Path) -> Path:
    (root / "app").mkdir(parents=True)
    (root / "app/main.py").write_text("x = 1\n")
    return root


def _stdin_prompt(saw: dict) -> str:
    message = json.loads(saw["stdin"])
    assert message["event"] == "user"
    return message["message"]["content"]


def test_the_prompt_travels_on_stdin_however_large(agy):
    """argv capped agy at 128 KiB; stdin has no such ceiling."""
    prompt = "REVIEW " + "x" * 300_000

    got = panel.review_llm("antigravity", "m", prompt)

    saw = agy()
    assert _stdin_prompt(saw) == prompt
    assert not any("x" * 1000 in a for a in saw["argv"])
    assert "--input-format" in saw["argv"] and "--output-format" in saw["argv"]
    assert len(got.findings) == 1


def test_with_tools_the_seat_runs_in_its_copy_with_a_stripped_environment(agy, tmp_path):
    tree = _tree(tmp_path / "src")

    got = panel.review_llm("antigravity", "m", "PROMPT", code_tree=tree, runs_code=True)

    saw = agy()
    assert saw["argv"].count("--dangerously-skip-permissions") == 1
    assert saw["had_code"] is True
    assert Path(saw["cwd"]).resolve() != tree.resolve(), "ran in the shared tree"
    assert _stdin_prompt(saw).endswith(f"YOUR COPY OF THE PR IS AT: {saw['cwd']}\n")
    env = saw["env"]
    for gone in ("SSH_AUTH_SOCK", "OPENAI_API_KEY", "PGHOSTADDR", "XDG_RUNTIME_DIR"):
        assert gone not in env, gone
    assert ".invalid" in env["DATABASE_URL"] and ".invalid" in env["PGHOST"]
    assert saw["gh_empty"] is True
    assert env["QB_SANDBOX"] == "1" and env["PATH"]
    assert got.code_blind is False and len(got.findings) == 1


def test_without_tools_the_seat_is_the_diff_only_reviewer(agy, tmp_path):
    tree = _tree(tmp_path / "src")

    got = panel.review_llm("antigravity", "m", "PROMPT", code_tree=tree)

    saw = agy()
    assert "--dangerously-skip-permissions" not in saw["argv"]
    assert saw["had_code"] is False
    assert _stdin_prompt(saw) == "PROMPT"
    assert saw["env"]["SSH_AUTH_SOCK"] == "/run/agent.sock"
    assert got.code_blind is True


def test_a_copy_that_fails_to_stage_sits_the_seat_out(agy, tmp_path, monkeypatch):
    """Its prompt promises a shell in a copy that does not exist, so it does not run."""
    tree = _tree(tmp_path / "src")

    def boom(*a, **k):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(panel_seats.shutil, "copytree", boom)

    got = panel.review_llm("antigravity", "m", "PROMPT", code_tree=tree, runs_code=True)

    assert agy() is None, "agy was started"
    assert "could not stage its copy" in got.skip


def test_a_failed_turn_is_no_reply_not_an_empty_review(agy, tmp_path):
    _install(tmp_path / "bin", tmp_path / "agy-saw.json", "ERROR")

    got = panel.review_llm("antigravity", "m", "PROMPT")

    assert got.findings == [] and "wrote no reply" in got.skip
    assert "status ERROR: model quota spent" in got.skip, "agy's own reason was dropped"


FIXTURES = Path(__file__).resolve().parent / "fixtures"


def test_a_recorded_agy_stream_parses():
    """Real agy 1.1.24 output, not an envelope these tests invented."""
    ok = (FIXTURES / "agy-1.1.24-stream-success.ndjson").read_text()
    assert panel_seats.antigravity_reply(ok).strip() == "The magic word is: banana"
    assert panel_seats.antigravity_error(ok) == ""
    bad = (FIXTURES / "agy-1.1.24-stream-error.ndjson").read_text()
    assert panel_seats.antigravity_reply(bad) == ""
    assert 'missing the "event" field' in panel_seats.antigravity_error(bad)


@pytest.mark.parametrize("stdout", [
    '{"event": "result", "result": "not an object"}',
    '{"event": "result", "result": {"status": "SUCCESS", "response": ["a", "list"]}}',
    '["not", "an", "event"]',
    "not json at all",
    "",
])
def test_a_malformed_result_is_no_reply_not_a_crash(stdout):
    assert panel_seats.antigravity_reply(stdout) == ""
    assert panel_seats.antigravity_error(stdout)


_SUCCESS = json.dumps({"event": "result", "result": {"status": "SUCCESS", "response": REPLY}})


@pytest.mark.parametrize("tail, why", [
    ('{"event": "result", "result": "not an object"}', "malformed final result"),
    ("garbage after the result", "malformed final result"),
    ('{"event": "result", "result": {"status": "ERROR", "error": "cut off"}}',
     "status ERROR: cut off"),
])
def test_the_last_result_decides_even_after_an_earlier_success(tail, why):
    stdout = f"{_SUCCESS}\n{tail}\n"
    assert panel_seats.antigravity_reply(stdout) == ""
    assert panel_seats.antigravity_error(stdout) == why


def test_the_overlay_applies_even_without_a_cwd(monkeypatch):
    monkeypatch.setenv("SSH_AUTH_SOCK", "/inherited-agent")
    out, err = panel_seats.run_cli(
        ["sh", "-c", 'printf "%s" "${SSH_AUTH_SOCK-unset}"'], "probe", attempts=1,
        env_extra={"SSH_AUTH_SOCK": None})
    assert err is None and out == "unset"
    assert os.environ["SSH_AUTH_SOCK"] == "/inherited-agent", "mutated the panel's env"


def _argv_capped_owner(path: Path, lineno: int, tree: ast.AST) -> ast.FunctionDef | None:
    owners = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
              and n.lineno <= lineno <= n.end_lineno]
    return min(owners, key=lambda n: n.end_lineno - n.lineno) if owners else None


def test_the_removed_argv_machinery_is_named_nowhere():
    """agy's prompt left argv. Nothing may still export, call, test or describe what
    went with it — in code, tests or the prose docs. `argv_capped` is the one
    exception, allowed exactly where it is read off OLD payloads: inside
    `panel_rounds.load_baseline`, and in tests that feed `load_baseline` one."""
    loops = Path(__file__).resolve().parent.parent
    gone = ("ARGV_PROMPT_MAX_BYTES", "fit_argv_budget", "argv_clamp", "_context_chars",
            "kernel_cut")
    prose = [loops / "README.md", loops.parent / "commands" / "panel-review-pr.md"]
    code = [*loops.glob("*.py"), *(p for p in loops.glob("tests/*.py")
                                   if p.name != Path(__file__).name)]
    for src in [*code, *prose]:
        text = src.read_text()
        for name in gone:
            assert not re.search(rf"\b{name}\b", text), f"{src.name} still names {name}"
    for src in code:
        text = src.read_text()
        if "argv_capped" not in text:
            continue
        tree = ast.parse(text)
        for lineno, line in enumerate(text.splitlines(), 1):
            if "argv_capped" not in line:
                continue
            owner = _argv_capped_owner(src, lineno, tree)
            body = ast.get_source_segment(text, owner) if owner else ""
            in_reader = (src.name == "panel_rounds.py" and owner is not None
                         and owner.name == "load_baseline")
            feeds_reader = (src.parent.name == "tests"
                            and ("load(" in body or "load_baseline(" in body))
            allowed = in_reader or feeds_reader
            assert allowed, f"{src.name}:{lineno} names argv_capped outside the old-payload read"


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


def test_the_shipped_default_is_on():
    assert harness_rules.DEFAULTS["reviewers"]["antigravity"]["tools"] is True


def test_a_repo_that_never_mentions_tools_reaches_dispatch_with_it_on(tmp_path, monkeypatch):
    """Through the real resolver, from a throwaway repo whose sample enables the seat
    without saying `tools` — not from whatever checkout the suite runs in."""
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

    rules = harness_rules.resolve_repo(str(repo), from_default_branch=False)["reviewers"]

    assert "antigravity" in panel_seats.tool_seats(rules)

def test_a_tools_value_that_is_not_a_boolean_is_refused():
    with pytest.raises(SystemExit, match=r"antigravity\.tools` must be true or false"):
        harness_rules._check_seat_shape(
            {"reviewers": {"antigravity": {"tools": "false"}}}, ".harness-rules.sample")


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
