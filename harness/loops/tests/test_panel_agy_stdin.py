"""The antigravity seat's transport: its prompt on stdin, its reply from the stream.

Driven through the real `review_llm` → `run_seat` → `run_cli` path with a fake `agy`
on PATH that records what it was handed, so these hold the invocation that would
actually run rather than a double of it.
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

FAKE_AGY = f"""#!{sys.executable}
import json, os, sys
out = OUT_PATH
stdin = sys.stdin.read()
with open(out, "w") as f:
    json.dump({{"argv": sys.argv[1:], "stdin": stdin}}, f)
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
    return lambda: json.loads(out.read_text()) if out.exists() else None


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
