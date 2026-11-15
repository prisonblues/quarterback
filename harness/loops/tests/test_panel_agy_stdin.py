"""The antigravity seat's transport: its prompt on stdin, its reply from the stream.

Driven through the real `review_llm` → `run_seat` → `run_cli` path with a fake `agy`
on PATH. The fake refuses what real agy refuses (a missing stream-json flag, a stdin
that is not one user message), so a wrong invocation fails here rather than passing.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import panel  # noqa: E402
import panel_seats  # noqa: E402

REPLY = '[{"severity": "P3", "file": "a.py", "line": 1, "title": "t", "detail": "d"}]'
FIXTURES = Path(__file__).resolve().parent / "fixtures"

#: CONF is the one substitution: a JSON file holding where to record what the fake
#: saw and what it should print. Its two refusals are the ones recorded from real
#: agy 1.1.24: `-p` beside `--input-format stream-json` (exit 2) and a stdin message
#: with no `event` field (an ERROR result, exit 1).
FAKE_AGY = f"""#!{sys.executable}
import json, sys
conf = json.load(open(CONF))
args = sys.argv[1:]
if "--input-format" in args and ("-p" in args or "--print" in args):
    sys.stderr.write("Error: --input-format stream-json reads prompts from stdin, so a "
                     "prompt given on the command line would be ignored\\n")
    sys.exit(2)
stdin = sys.stdin.read()
try:
    has_event = "event" in json.loads(stdin.splitlines()[0])
except Exception:
    has_event = False
if not has_event:
    print(json.dumps({{"event": "result", "result": {{"status": "ERROR", "response": "",
                      "error": 'stream input message is missing the "event" field'}}}}))
    sys.exit(1)
with open(conf["out"], "w") as f:
    json.dump({{"argv": args, "stdin": stdin}}, f)
sys.stdout.write(conf["stdout"])
sys.stderr.write(conf["stderr"])
sys.exit(conf["code"])
"""


def _stream(status: str, response: str = REPLY, error: str = "") -> str:
    return "\n".join([
        json.dumps({"event": "init", "init": {}}),
        json.dumps({"event": "result",
                    "result": {"status": status, "response": response, "error": error}}),
    ]) + "\n"


@pytest.fixture
def agy(tmp_path, monkeypatch):
    """Install a fake `agy` first on PATH. Call it with what agy should print; it
    returns a reader for what the fake was handed."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    out = tmp_path / "agy-saw.json"
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")

    conf = tmp_path / "agy-conf.json"

    def install(stdout: str = _stream("SUCCESS"), stderr: str = "", code: int = 0):
        conf.write_text(json.dumps({"out": str(out), "stdout": stdout, "stderr": stderr,
                                    "code": code}))
        exe = bin_dir / "agy"
        exe.write_text(FAKE_AGY.replace("CONF", repr(str(conf)), 1))
        exe.chmod(0o755)
        return lambda: json.loads(out.read_text()) if out.exists() else None
    return install


def test_the_prompt_travels_on_stdin_however_large(agy):
    """argv capped agy at 128 KiB; stdin has no such ceiling."""
    saw = agy()
    prompt = "REVIEW " + "x" * 300_000

    got = panel.review_llm("antigravity", "m", prompt)

    assert json.loads(saw()["stdin"])["message"]["content"] == prompt
    assert not any("x" * 1000 in a for a in saw()["argv"])
    assert len(got.findings) == 1 and got.skip is None


def test_an_agy_error_result_is_in_the_failure_diagnosis(agy):
    """agy reports a failed turn on stdout, in the result event; the skip and every
    classifier read the diagnosis that carries it."""
    stream = _stream("ERROR", response="", error="model quota spent")
    agy(stdout=stream)

    got = panel.review_llm("antigravity", "m", "PROMPT")

    assert got.findings == [] and "model quota spent" in got.skip
    assert "model quota spent" in panel_seats.error_text(stream), "classifiers cannot see it"


def test_a_nonzero_exit_is_a_failure_with_agys_words(agy):
    agy(stdout="", stderr="AGY_ERROR: the model refused the request\n", code=3)

    got = panel.review_llm("antigravity", "m", "PROMPT")

    assert got.findings == [] and "the model refused the request" in got.skip


def test_a_recorded_denial_is_a_named_permission_skip(agy):
    """Real agy 1.1.24 under stream-json, asked to run a command it may not: it
    soft-denies, ends the turn with SUCCESS and an empty response, and says why on
    stderr. The seat must come back as a skip naming the denial, not as a review."""
    agy(stdout=(FIXTURES / "agy-1.1.24-stream-denied.ndjson").read_text(),
        stderr=(FIXTURES / "agy-1.1.24-stream-denied.stderr").read_text())

    got = panel.review_llm("antigravity", "m", "PROMPT")

    assert got.findings == [] and got.unstructured is False
    assert "permissions.allow" in got.skip


def test_a_recorded_agy_stream_parses():
    """Real agy 1.1.24 output, not an envelope these tests invented."""
    ok = (FIXTURES / "agy-1.1.24-stream-success.ndjson").read_text()
    assert panel_seats.antigravity_reply(ok).strip() == "The magic word is: banana"
    bad = (FIXTURES / "agy-1.1.24-stream-error.ndjson").read_text()
    assert panel_seats.antigravity_reply(bad) == ""
    assert 'missing the \\"event\\" field' in panel_seats.error_text(bad)


_SUCCESS = json.dumps({"event": "result", "result": {"status": "SUCCESS", "response": REPLY}})


@pytest.mark.parametrize("stdout", [
    '{"event": "result", "result": "not an object"}',
    # Malformed stays malformed: a later success does not rescue it.
    f"{_SUCCESS}\ngarbage\n{_SUCCESS}",
    '{"event": "result", "result": {"status": "SUCCESS", "response": ["a", "list"]}}',
    "not json at all",
    "",
    # Anything that is not an event, once a result has been seen:
    f"{_SUCCESS}\n" + '{"event": "result", "result": "not an object"}',
    f"{_SUCCESS}\ngarbage after the result",
    f"{_SUCCESS}\n" + '["a", "json", "list"]',
    f"{_SUCCESS}\n" + '{"no": "event key"}',
    f"{_SUCCESS}\n" + '{"event": "result", "result": {"status": "ERROR", "error": "cut off"}}',
])
def test_only_a_final_successful_text_result_is_a_reply(stdout):
    assert panel_seats.antigravity_reply(stdout) == ""


def test_a_failed_result_with_only_a_reason_code_still_says_why():
    stream = json.dumps({"event": "result",
                         "result": {"status": "CANCELLED", "finished_reason": "print_timeout"}})
    assert "print_timeout" in panel_seats.error_text(stream)


def test_a_success_with_trailing_blank_lines_is_still_a_reply():
    assert panel_seats.antigravity_reply(f"{_SUCCESS}\n\n\n") == REPLY
