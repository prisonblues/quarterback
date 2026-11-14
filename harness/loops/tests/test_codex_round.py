"""#836: the Codex round — one Codex seat that carries the class ledger, the two-strike
rule and the round budget, in place of the hand-rolled `codex exec` loops that had none.
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import codex_round as cr  # noqa: E402
import panel_classes as pc  # noqa: E402

RESOLVER = {"name": "resolver call without the viewer",
            "invariant": "every resolver call is handed the viewer",
            "enforcement": "keyword-only viewer=", "guard_test": "AST walk",
            "instances": [{"file": "a.py", "line": 10, "path": "GET /x", "detail": "d"}]}


# ---------------------------------------------------------------------- the reply

def test_a_reply_wrapped_in_prose_still_parses():
    reply = cr.parse_reply('Here:\n{"violations": [], "new_classes": [{"name": "n"}]}\n')
    assert reply == {"violations": [], "new_classes": [{"name": "n"}]}


@pytest.mark.parametrize("text", ["", "no json here", "{broken", "[1, 2]"])
def test_a_reply_with_no_object_is_none(text):
    assert cr.parse_reply(text) is None


@pytest.mark.parametrize("text", ["{}", '{"status": "error"}',
                                  '{"violations": [], "new_classes": "x"}',
                                  '{"violations": [1], "new_classes": []}',
                                  '{"violations": [], "new_classes": ["x"]}'])
def test_a_reply_without_both_arrays_is_not_an_empty_review(text):
    assert cr.parse_reply(text) is None


# ---------------------------------------------------------------- recording a round

def test_a_new_class_records_every_instance_it_names():
    ledger = pc.new_ledger()
    two = {**RESOLVER, "instances": RESOLVER["instances"]
           + [{"file": "b.py", "line": 3, "path": "MCP read"}]}
    lines = cr.apply_replies(ledger, [{"violations": [], "new_classes": [two]}], 1, 2)
    entry = ledger["classes"][0]
    assert len(entry["sightings"]) == 2 and entry["enforcement"] == "keyword-only viewer="
    # Two instances in one reply are one class with two instances (#840).
    assert pc.strikes(entry) == 1 and not pc.is_owed(entry, 2)
    assert lines[0].startswith("  new class")


def test_a_violation_is_recorded_against_the_class_it_names():
    ledger = pc.new_ledger()
    cr.apply_replies(ledger, [{"violations": [], "new_classes": [RESOLVER]}], 1, 2)
    key = ledger["classes"][0]["key"]
    lines = cr.apply_replies(ledger, [{"violations": [
        {"class": key, "file": "c.py", "line": 4, "path": "export",
         "detail": "no viewer", "evidence": "pytest c_test.py: 1 failed"}],
        "new_classes": []}], 2, 2)
    assert pc.strikes(ledger["classes"][0]) == 2
    assert "violation" in lines[0] and "[ran: pytest c_test.py: 1 failed]" in lines[0]


def test_a_violation_of_an_unrecorded_class_is_reported_not_recorded():
    ledger = pc.new_ledger()
    lines = cr.apply_replies(ledger, [{"violations": [{"class": "feedbeef00000000",
                                                       "file": "a.py"}],
                                       "new_classes": []}], 2, 2)
    assert ledger["classes"] == [] and "unclassified" in lines[0]


def test_a_class_with_no_invariant_is_dropped():
    ledger = pc.new_ledger()
    lines = cr.apply_replies(ledger, [{"violations": [], "new_classes": [
        {**RESOLVER, "invariant": ""}]}], 1, 2)
    assert ledger["classes"] == [] and "dropped" in lines[0]


# ---------------------------------------------------------------------- exit codes

@pytest.mark.parametrize("state, found, partial, code", [
    ({"over": True, "owed": ["k"], "converged": False}, ["x"], False, pc.BUDGET_EXIT),
    ({"over": False, "owed": ["k"], "converged": True}, ["x"], False,
     pc.STRUCTURAL_OWED_EXIT),
    ({"over": False, "owed": [], "converged": False}, ["x"], False, 1),
    ({"over": False, "owed": [], "converged": True}, ["x"], False, 1),
    ({"over": False, "owed": [], "converged": True}, [], True, 1),
    ({"over": False, "owed": [], "converged": True}, [], False, 0),
])
def test_the_exit_code_says_what_the_caller_does_next(state, found, partial, code):
    assert cr.exit_code(state, found, partial) == code


# ---------------------------------------------------------------- shards + prompts

def test_shards_name_the_areas_or_all():
    assert cr.shards_of("") == [""]
    assert cr.shards_of("all") == list(cr.SHARDS)
    assert cr.shards_of("data, api") == ["data", "api"]
    with pytest.raises(SystemExit, match="unknown area"):
        cr.shards_of("ui")


def test_a_later_round_is_briefed_from_the_ledger_and_the_first_is_open():
    ledger = pc.new_ledger()
    assert cr.OPEN_ROUND in cr.build_prompt("x", "diff", ledger, [])
    cr.apply_replies(ledger, [{"violations": [], "new_classes": [RESOLVER]}], 1, 2)
    prompt = cr.build_prompt("x", "diff", ledger, [], "api")
    assert pc.LEDGER_HEADING in prompt and cr.SHARDS["api"] in prompt


def test_only_a_seat_with_a_shell_is_told_it_can_run_code():
    assert cr.EXECUTION in cr.build_prompt("x", "d", pc.new_ledger(), [], can_run=True)
    assert cr.EXECUTION not in cr.build_prompt("x", "d", pc.new_ledger(), [])


def test_the_executing_seat_writes_only_where_the_sandbox_lets_it(tmp_path):
    argv = cr.exec_args("", "", tmp_path / "r.txt", network=False)
    assert argv[argv.index("-s") + 1] == "workspace-write"
    assert "sandbox_workspace_write.network_access=false" in argv
    assert "sandbox_workspace_write.network_access=true" in cr.exec_args(
        "m", "high", tmp_path / "r.txt", network=True)


def test_a_seat_that_changed_the_tree_is_reported(monkeypatch, capsys):
    states = iter(["clean", "dirty"])
    monkeypatch.setattr(cr, "tree_state", lambda top: next(states))
    monkeypatch.setattr(cr, "run_codex", lambda *a: {"violations": [], "new_classes": []})
    cr.ask_seats(["p"], "", "", "/repo", False)
    assert "CHANGED the worktree" in capsys.readouterr().err


# ---------------------------------------------------------------------- main()

@pytest.fixture
def round_env(monkeypatch, tmp_path):
    """Everything main() reaches outside the process, replaced."""
    monkeypatch.setattr(cr, "_seat", lambda: ("", "", 2, 3))
    monkeypatch.setattr(cr, "material", lambda base, design: ("a change", "diff"))
    monkeypatch.setattr(cr, "git", lambda *a, **k: str(tmp_path))
    monkeypatch.setattr(cr.shutil, "which", lambda name: "/bin/codex")
    monkeypatch.setattr(cr, "tree_state", lambda top: "same")
    replies: list = []
    asked: list = []

    def fake(prompt, model, effort, top, network):
        asked.append((prompt, top))
        return replies.pop(0) if replies else None
    monkeypatch.setattr(cr, "run_codex", fake)
    return str(tmp_path / "classes.json"), replies, asked


def test_a_first_round_records_its_classes_and_asks_for_another(round_env, capsys):
    ledger, replies, asked = round_env
    replies.append({"violations": [], "new_classes": [RESOLVER]})
    assert cr.main(["--ledger", ledger]) == 1
    saved, _ = pc.load_ledger(ledger)
    assert saved["classes"][0]["name"] == RESOLVER["name"]
    assert asked[0][1]                      # ran in the worktree, with a shell


def test_a_clean_round_converges(round_env):
    ledger, replies, _ = round_env
    replies.append({"violations": [], "new_classes": [RESOLVER]})
    cr.main(["--ledger", ledger])
    replies.append({"violations": [], "new_classes": []})
    assert cr.main(["--ledger", ledger]) == 0


def test_no_reply_from_codex_is_a_skip_not_a_clean_round(round_env):
    ledger, _, _ = round_env
    assert cr.main(["--ledger", ledger]) == cr.UNAVAILABLE_EXIT


def test_no_exec_runs_the_shell_less_seat(round_env):
    ledger, replies, asked = round_env
    replies.append({"violations": [], "new_classes": []})
    cr.main(["--ledger", ledger, "--no-exec"])
    assert asked[0][1] == "" and cr.EXECUTION not in asked[0][0]


def test_a_spent_budget_stops_before_codex_is_called(round_env, capsys):
    ledger_path, _, asked = round_env
    ledger = pc.new_ledger()
    for r in (1, 2, 3):
        pc.record(ledger, f"class number {r} of a kind", r, f"f{r}.py",
                  invariant="holds")
    pc.save_ledger(ledger_path, ledger)
    assert cr.main(["--ledger", ledger_path]) == pc.BUDGET_EXIT
    assert asked == [] and "owner decides" in capsys.readouterr().err
    assert cr.main(["--ledger", ledger_path, "--class-rounds", "5"]) == \
        cr.UNAVAILABLE_EXIT


def test_a_partial_shard_round_says_so(round_env, capsys):
    ledger, replies, _ = round_env
    replies.append({"violations": [], "new_classes": []})
    assert cr.main(["--ledger", ledger, "--shard", "data,api"]) == 1
    assert "PARTIAL: 1 of 2" in capsys.readouterr().out


def test_the_json_report_carries_the_state(round_env, capsys):
    ledger, replies, _ = round_env
    replies.append({"violations": [], "new_classes": [RESOLVER]})
    cr.main(["--ledger", ledger, "--json"])
    out = json.loads(capsys.readouterr().out)
    assert out["state"]["round"] == 1 and out["shards"] == 1


# ------------------------------------------------- what the first Codex round found

@pytest.fixture
def repo(tmp_path, monkeypatch):
    def run(*argv):
        subprocess.run(["git", *argv], cwd=tmp_path, check=True, capture_output=True)
    run("init", "-q", "-b", "main")
    run("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty",
        "-m", "base")
    monkeypatch.chdir(tmp_path)
    return tmp_path, run


def test_a_new_file_not_yet_added_is_in_the_material(repo):
    top, _ = repo
    (top / "new_module.py").write_text("def f():\n    return 1\n")
    _, text = cr.material("HEAD", "")
    assert "new_module.py" in text and "return 1" in text


def test_a_seat_that_committed_is_caught(repo):
    top, run = repo
    before = cr.tree_state(str(top))
    run("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty",
        "-m", "seat")
    assert cr.tree_state(str(top)) != before


def test_executing_seats_take_the_worktree_one_at_a_time(monkeypatch):
    import threading
    import time
    live, peak, lock = [0], [0], threading.Lock()

    def seat(*a):
        with lock:
            live[0] += 1
            peak[0] = max(peak[0], live[0])
        time.sleep(0.05)
        with lock:
            live[0] -= 1
        return {"violations": [], "new_classes": []}
    monkeypatch.setattr(cr, "run_codex", seat)
    monkeypatch.setattr(cr, "tree_state", lambda top: "same")
    cr.ask_seats(["a", "b", "c"], "", "", "/repo", False)
    assert peak[0] == 1
    cr.ask_seats(["a", "b", "c"], "", "", "", False)
    assert peak[0] == 3


def test_the_executing_seat_has_no_apps_or_plugins(tmp_path):
    argv = cr.exec_args("", "", tmp_path / "r.txt", network=True)
    assert "features.apps=false" in argv and "features.plugins=false" in argv


@pytest.mark.parametrize("flag", ["--round", "--class-rounds"])
@pytest.mark.parametrize("value", ["0", "-1", "x"])
def test_a_round_or_budget_below_one_is_refused(flag, value):
    with pytest.raises(SystemExit):
        cr._parser().parse_args([flag, value])


def test_a_clean_round_is_counted_so_the_next_gets_its_own_number(round_env):
    ledger, replies, _ = round_env
    replies.append({"violations": [], "new_classes": [RESOLVER]})
    cr.main(["--ledger", ledger])
    replies.append({"violations": [], "new_classes": []})
    cr.main(["--ledger", ledger])
    saved, _ = pc.load_ledger(ledger)
    assert pc.last_round(saved) == 2


def test_an_unreadable_ledger_stops_the_round_and_is_left_alone(round_env, capsys):
    ledger, replies, asked = round_env
    Path(ledger).write_text("{not json")
    replies.append({"violations": [], "new_classes": []})
    assert cr.main(["--ledger", ledger]) == pc.LEDGER_FAILED_EXIT
    assert asked == [] and Path(ledger).read_text() == "{not json"


def test_a_seat_that_switched_branch_is_caught(repo):
    top, run = repo
    before = cr.tree_state(str(top))
    run("switch", "-q", "-c", "other")
    assert cr.tree_state(str(top)) != before


def test_a_round_whose_seat_changed_the_tree_is_not_clean(round_env, monkeypatch):
    ledger, replies, _ = round_env
    states = iter(["clean", "dirty"])
    monkeypatch.setattr(cr, "tree_state", lambda top: next(states))
    replies.append({"violations": [], "new_classes": []})
    assert cr.main(["--ledger", ledger]) == 1


def test_a_round_that_could_not_be_saved_does_not_report_itself(round_env, monkeypatch,
                                                                 capsys):
    ledger, replies, _ = round_env
    monkeypatch.setattr(pc, "save_ledger", lambda path, led: pc.LEDGER_FAILED_EXIT)
    replies.append({"violations": [], "new_classes": []})
    assert cr.main(["--ledger", ledger]) == pc.LEDGER_FAILED_EXIT
    out = capsys.readouterr()
    assert "CONVERGED" not in out.out and "NOT recorded" in out.err
