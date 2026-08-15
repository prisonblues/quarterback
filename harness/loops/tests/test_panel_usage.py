"""Per-reviewer token usage, read back out of a pinned session.

The board scores reviewers on what they find and how long they take. Tokens are
the third axis, and the one that answers a question duration can't: *within* one
vendor, is the expensive tier worth it — opus over sonnet, codex xhigh over
medium. Same tokenizer, same cache semantics, so directly comparable.

The design constraint these tests exist to hold is that **the findings path never
changes shape to get the telemetry**. Every vendor's JSON output mode moves the
reply inside an envelope, so adopting them would mean four bespoke unwrappers on
the one path that currently works — four new ways to lose the findings, in
exchange for a number. Pinning a session and reading usage afterwards inverts
that: a failed read costs a number and nothing else, which is what most of the
cases below assert.
"""

import json
import subprocess
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import panel  # noqa: E402

FINDINGS = '[{"severity":"P2","file":"a.py","line":1,"title":"t","detail":"d"}]'


def write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records))


def claude_msg(mid: str, *, inp=0, creation=0, read=0, out=0, thinking=0) -> dict:
    return {
        "type": "assistant",
        "uuid": f"line-{mid}-{inp}-{out}",
        "message": {
            "id": mid,
            "usage": {
                "input_tokens": inp,
                "cache_creation_input_tokens": creation,
                "cache_read_input_tokens": read,
                "output_tokens": out,
                "output_tokens_details": {"thinking_tokens": thinking},
            },
        },
    }


# ------------------------------------------------------------------ claude

def test_a_streamed_message_written_twice_is_charged_once(monkeypatch, tmp_path):
    """The trap this reader exists to avoid.

    Claude's transcript records one assistant message on MORE THAN ONE line — a
    streamed reply lands twice, same `message.id`, different line `uuid` — so
    summing the lines charges the reviewer double. That reads on /panel as the
    seat costing twice what it did, which is exactly the judgement the column is
    supposed to inform.
    """
    sid = "sess-dupe"
    dup = claude_msg("msg_1", inp=2, creation=14859, read=24356, out=455, thinking=450)
    other = dict(dup, uuid="a-different-line")
    write_jsonl(tmp_path / ".claude/projects/-some-slug" / f"{sid}.jsonl", [dup, other])
    monkeypatch.setattr(panel.Path, "home", classmethod(lambda cls: tmp_path))

    u = panel.claude_usage([sid])
    assert u["output_tokens"] == 455                       # not 910
    assert u["input_tokens"] == 2 + 14859 + 24356          # not doubled
    assert u["reasoning_tokens"] == 450


def test_claudes_whole_prompt_is_the_input_not_the_uncached_remainder(monkeypatch, tmp_path):
    """`input_tokens` on Claude counts only what it neither cached nor read from
    cache. Recording that verbatim would file a 60k-char diff as a 2-token
    prompt, so the three prompt-side figures are added and the cached slice is
    also kept separately."""
    sid = "sess-cache"
    write_jsonl(tmp_path / ".claude/projects/-slug" / f"{sid}.jsonl",
                [claude_msg("m1", inp=2, creation=100, read=900, out=10)])
    monkeypatch.setattr(panel.Path, "home", classmethod(lambda cls: tmp_path))

    u = panel.claude_usage([sid])
    assert u["input_tokens"] == 1002
    assert u["cached_input_tokens"] == 900


def test_a_multi_message_turn_is_summed(monkeypatch, tmp_path):
    sid = "sess-multi"
    write_jsonl(tmp_path / ".claude/projects/-slug" / f"{sid}.jsonl", [
        claude_msg("m1", inp=10, out=5),
        claude_msg("m2", inp=20, out=7),
    ])
    monkeypatch.setattr(panel.Path, "home", classmethod(lambda cls: tmp_path))
    assert panel.claude_usage([sid]) == panel._usage(30, 12, 0, 0)


def test_claude_states_no_cost(monkeypatch, tmp_path):
    """`--output-format json` does put a cost on stdout, but that mode wraps the
    findings — the trade this design refuses. So the retrospective path reports
    tokens only, and a null cost means "not stated", never "free"."""
    sid = "sess-cost"
    write_jsonl(tmp_path / ".claude/projects/-slug" / f"{sid}.jsonl",
                [claude_msg("m1", inp=1, out=1)])
    monkeypatch.setattr(panel.Path, "home", classmethod(lambda cls: tmp_path))
    assert "cost_usd" not in panel.claude_usage([sid])


def test_a_missing_transcript_reports_nothing_rather_than_zero(monkeypatch, tmp_path):
    """None, not a zeroed dict: the board must be able to tell "not recorded"
    from "spent nothing", or an unread transcript is averaged in as a free run."""
    monkeypatch.setattr(panel.Path, "home", classmethod(lambda cls: tmp_path))
    assert panel.claude_usage(["never-ran"]) is None


def test_a_half_written_last_line_costs_a_message_not_the_read(monkeypatch, tmp_path):
    """Transcripts are appended to as the turn runs, so the tail can be a partial
    flush. One unparseable line must not throw away the lines that did parse."""
    sid = "sess-partial"
    p = tmp_path / ".claude/projects/-slug" / f"{sid}.jsonl"
    write_jsonl(p, [claude_msg("m1", inp=10, out=5)])
    with p.open("a") as fh:
        fh.write('{"type":"assistant","message":{"id":"m2","usa')
    monkeypatch.setattr(panel.Path, "home", classmethod(lambda cls: tmp_path))
    assert panel.claude_usage([sid])["input_tokens"] == 10


# ---------------------------------------------------------------------- pi

def pi_record(inp, out, cache_read=0, cache_write=0, reasoning=0, cost=None) -> dict:
    usage = {"input": inp, "output": out, "cacheRead": cache_read,
             "cacheWrite": cache_write, "reasoning": reasoning}
    if cost is not None:
        usage["cost"] = {"total": cost}
    return {"type": "message", "message": {"role": "assistant", "usage": usage}}


def test_pis_cache_reads_sit_beside_its_input_not_inside_it(tmp_path):
    """pi's own totalTokens adds input + output + cacheRead + cacheWrite, so its
    `input` excludes the cache — the opposite of codex, where it's included.
    Normalising here is what keeps `input_tokens` meaning one thing on the board.
    """
    write_jsonl(tmp_path / "2026-08-14T23-17_s1.jsonl",
                [pi_record(453, 27, cache_read=64, cache_write=8, reasoning=14)])
    u = panel.pi_usage(tmp_path, ["s1"])
    assert u["input_tokens"] == 453 + 64 + 8
    assert u["cached_input_tokens"] == 64
    assert u["output_tokens"] == 27
    assert u["reasoning_tokens"] == 14


def test_pis_own_cost_is_recorded_because_the_vendor_states_it(tmp_path):
    """The one seat that states a price. Recorded as stated and never derived: a
    run priced from a table at today's rates is silently wrong when the board is
    queried in six weeks."""
    write_jsonl(tmp_path / "ts_s2.jsonl", [pi_record(1, 1, cost=0.0017832)])
    assert panel.pi_usage(tmp_path, ["s2"])["cost_usd"] == 0.001783


def test_pi_reporting_no_cost_reports_none_rather_than_zero(tmp_path):
    write_jsonl(tmp_path / "ts_s3.jsonl", [pi_record(1, 1)])
    assert "cost_usd" not in panel.pi_usage(tmp_path, ["s3"])


def test_pi_is_found_by_session_id_not_by_timestamp(tmp_path):
    """pi names the file `<timestamp>_<id>.jsonl`. Globbing the id is what keeps
    four concurrent panels from reading each other's numbers."""
    write_jsonl(tmp_path / "2026-01-01T00-00_ours.jsonl", [pi_record(5, 5)])
    write_jsonl(tmp_path / "2026-12-31T23-59_theirs.jsonl", [pi_record(999, 999)])
    assert panel.pi_usage(tmp_path, ["ours"])["input_tokens"] == 5


def test_a_missing_pi_session_reports_nothing(tmp_path):
    assert panel.pi_usage(tmp_path, ["absent"]) is None


# ------------------------------------------------------------------- codex

def codex_stream(*turns) -> str:
    lines = ['{"type": "thread.started", "thread_id": "t"}',
             '{"type": "item.completed", "item": {"type": "agent_message", "text": "OK"}}']
    for inp, cached, out, reasoning in turns:
        lines.append(json.dumps({"type": "turn.completed", "usage": {
            "input_tokens": inp, "cached_input_tokens": cached,
            "cache_write_input_tokens": 0, "output_tokens": out,
            "reasoning_output_tokens": reasoning}}))
    return "\n".join(lines) + "\n"


def test_codex_usage_comes_off_its_event_stream():
    """codex cannot pin a session id for a NEW run, and picking our rollout out of
    ~/.codex/sessions by mtime races the up-to-4 concurrent panels. So this seat
    reads usage from stdout — which it can do without wrapping the findings,
    because --output-last-message hands those over as a plain-text file."""
    u = panel.codex_usage(codex_stream((13767, 11008, 5, 0)))
    assert u["input_tokens"] == 13767 and u["cached_input_tokens"] == 11008
    assert u["output_tokens"] == 5


def test_every_turn_of_a_codex_run_is_charged():
    """Summed, not last-wins: a run that took two turns spent both."""
    u = panel.codex_usage(codex_stream((100, 10, 5, 2), (200, 20, 7, 3)))
    assert u["input_tokens"] == 300 and u["output_tokens"] == 12
    assert u["reasoning_tokens"] == 5


def test_codex_stdout_without_usage_reports_nothing():
    assert panel.codex_usage('{"type": "turn.started"}') is None
    assert panel.codex_usage("") is None
    assert panel.codex_usage(None) is None


def test_codex_argv_asks_for_the_stream_and_the_plain_text_reply(tmp_path):
    f = tmp_path / "reply.txt"
    args = panel.codex_args("gpt-5.6-luna", "high", f)
    assert args[-3:] == ["--json", "--output-last-message", str(f)]
    # The old shape is still what an unmetered call produces, so nothing else
    # that builds codex argv had to change. (The prompt itself goes on stdin —
    # `codex exec` with no positional argument reads it there.)
    assert panel.codex_args("", "") == ["codex", "exec"]


# ------------------------------------------------- review_llm, end to end

def test_codex_findings_come_from_the_file_not_the_event_stream(monkeypatch):
    """The whole point: `--json` fills stdout with events, so the findings would
    be unparseable there. They arrive as plain text in the reply file and go
    through the same parser as every other seat."""
    monkeypatch.setattr(panel.shutil, "which", lambda _: "/usr/bin/codex")

    def fake(args, *a, on_output=None, **k):
        # `args` is a thunk for codex now, as it already was for the
        # session-pinned seats: every attempt gets its own reply path so a
        # failed one cannot inherit an earlier attempt's text. Resolve it the
        # way `run_cli` does.
        argv = args() if callable(args) else args
        Path(argv[argv.index("--output-last-message") + 1]).write_text(FINDINGS)
        out = codex_stream((90, 10, 20, 4))
        on_output(out)
        return out, None

    monkeypatch.setattr(panel, "run_cli", fake)
    run = panel.review_llm("codex", "gpt-5.6-luna", "p")
    assert run.skip is None and len(run.findings) == 1
    assert run.usage["input_tokens"] == 90 and run.usage["output_tokens"] == 20


def test_a_reviewer_that_burned_tokens_then_failed_still_reports_them(monkeypatch):
    """A timeout after the model has already read a 60k-char diff is the most
    expensive outcome the panel has. Dropping its usage would make the seat that
    wastes the most look like the seat that costs nothing."""
    def burned(args, *a, on_output=None, **k):
        on_output(codex_stream((5000, 0, 3, 0)))
        return None, "codex: timed out after 600s"

    monkeypatch.setattr(panel.shutil, "which", lambda _: "/usr/bin/codex")
    monkeypatch.setattr(panel, "run_cli", burned)
    run = panel.review_llm("codex", "gpt-5.6-luna", "p")
    assert "timed out" in run.skip
    assert run.usage["input_tokens"] == 5000


def test_an_unreadable_session_costs_a_number_and_never_the_findings(monkeypatch):
    """The trade this whole approach is chosen for. Nothing at all is written
    where claude's transcript would be, and the review still lands."""
    monkeypatch.setattr(panel.shutil, "which", lambda _: "/usr/bin/claude")
    monkeypatch.setattr(panel, "run_cli", lambda *a, **k: (FINDINGS, None))
    monkeypatch.setattr(panel, "claude_usage",
                        lambda _sids: (_ for _ in ()).throw(OSError("no transcript")))
    run = panel.review_llm("claude", "opus", "p")
    assert run.skip is None and len(run.findings) == 1
    assert run.usage is None


def capture_argv(monkeypatch, cli, reviewer, model):
    """The argv one attempt of `reviewer` would run.

    `args` is a thunk for the session-pinned seats, so a test that wants the
    command line has to call it — the same thing run_cli does per attempt.
    """
    seen = {}

    def run(args, *a, **k):
        seen["argv"] = args() if callable(args) else args
        return FINDINGS, None

    monkeypatch.setattr(panel.shutil, "which", lambda _: cli)
    monkeypatch.setattr(panel, "run_cli", run)
    monkeypatch.setattr(panel, "claude_usage", lambda _sids: None)
    panel.review_llm(reviewer, model, "p")
    return seen["argv"]


def test_the_claude_session_is_pinned_on_the_command_line(monkeypatch):
    assert "--session-id" in capture_argv(monkeypatch, "/usr/bin/claude", "claude", "opus")


def test_the_pinned_id_is_a_bare_uuid(monkeypatch):
    """`claude --session-id` refuses anything that is not a valid UUID — it exits
    with "Invalid session ID" before reviewing anything. A readable prefix on the
    id would therefore not lose the token count, it would kill the reviewer, so
    the format is pinned by a test rather than by a comment."""
    argv = capture_argv(monkeypatch, "/usr/bin/claude", "claude", "opus")
    sid = argv[argv.index("--session-id") + 1]
    assert str(uuid.UUID(sid)) == sid          # raises if it isn't a plain UUID


def test_every_attempt_gets_its_own_session(monkeypatch):
    """`run_cli` retries a flake up to three times, and claude REFUSES a session
    id that already exists ("Session ID … is already in use"). Reusing one
    would turn the retry that exists to recover a flake into a guaranteed second
    failure — so the argv is a thunk and each attempt mints a fresh id."""
    ids = []

    def flaky(args, *a, **k):
        # The real run_cli calls the thunk once per attempt; do the same.
        for _ in range(3):
            argv = args() if callable(args) else args
            ids.append(argv[argv.index("--session-id") + 1])
        return FINDINGS, None

    monkeypatch.setattr(panel.shutil, "which", lambda _: "/usr/bin/claude")
    monkeypatch.setattr(panel, "run_cli", flaky)
    monkeypatch.setattr(panel, "claude_usage", lambda sids: {"seen": len(sids)})
    run = panel.review_llm("claude", "opus", "p")
    assert len(set(ids)) == 3                    # three attempts, three ids
    assert run.usage == {"seen": 3}              # and all three are read back


def test_codex_is_charged_for_the_attempts_that_failed_too(monkeypatch):
    """`run_cli` returns only the LAST attempt's stdout, but a codex attempt that
    burned 5k tokens before exiting non-zero still spent them. The pinned seats
    get this for free (every session is read); codex needs every attempt's
    stdout handed over, or the seat that flakes most looks like the cheapest."""
    attempts = [
        (codex_stream((1000, 0, 10, 0)), 1),     # flaked, tokens already spent
        (codex_stream((2000, 0, 20, 0)), 0),     # landed
    ]

    class Proc:
        def __init__(self, out, rc):
            self.stdout, self.returncode, self.stderr = out, rc, "flake"

    def fake_run(argv, **k):
        out, rc = attempts.pop(0)
        Path(argv[argv.index("--output-last-message") + 1]).write_text(FINDINGS)
        return Proc(out, rc)

    monkeypatch.setattr(panel.shutil, "which", lambda _: "/usr/bin/codex")
    monkeypatch.setattr(panel.subprocess, "run", fake_run)
    run = panel.review_llm("codex", "gpt-5.6-luna", "p")
    assert run.skip is None and len(run.findings) == 1
    assert run.usage["input_tokens"] == 3000     # both attempts, not just the one that worked
    assert run.usage["output_tokens"] == 30


def test_run_cli_rebuilds_the_command_line_for_each_attempt(monkeypatch):
    """The mechanism the above relies on, pinned at its own level."""
    seen = []
    calls = {"n": 0}

    class Proc:
        returncode = 1
        stdout = ""
        stderr = "transient"

    def fake_run(argv, **k):
        seen.append(list(argv))
        calls["n"] += 1
        return Proc()

    monkeypatch.setattr(panel.subprocess, "run", fake_run)
    panel.run_cli(lambda: ["cli", str(calls["n"])], "label", attempts=3)
    assert seen == [["cli", "0"], ["cli", "1"], ["cli", "2"]]


def test_a_fixed_argv_still_works_unchanged(monkeypatch):
    """Every other caller passes a plain list and must be unaffected."""
    seen = []

    class Proc:
        returncode = 0
        stdout = "out"
        stderr = ""

    monkeypatch.setattr(panel.subprocess, "run",
                        lambda argv, **k: (seen.append(list(argv)), Proc())[1])
    assert panel.run_cli(["cli", "x"], "label") == ("out", None)
    assert seen == [["cli", "x"]]


def test_a_member_that_flaked_then_landed_is_charged_for_both(monkeypatch, tmp_path):
    """Two attempts is two turns of tokens. Reading only the last session would
    under-report exactly the reviewer that is costing the most to keep."""
    write_jsonl(tmp_path / ".claude/projects/-slug/first.jsonl",
                [claude_msg("m1", inp=100, out=10)])
    write_jsonl(tmp_path / ".claude/projects/-slug/second.jsonl",
                [claude_msg("m2", inp=200, out=20)])
    monkeypatch.setattr(panel.Path, "home", classmethod(lambda cls: tmp_path))
    u = panel.claude_usage(["first", "second"])
    assert u["input_tokens"] == 300 and u["output_tokens"] == 30


def test_antigravity_reviews_exactly_as_before_and_reports_no_tokens(monkeypatch):
    """`agy` has no session id to pin and states usage only in the JSON mode this
    design declines. It is left uninstrumented rather than half-converted, and a
    null renders as "not recorded" rather than as a free reviewer."""
    seen = {}
    monkeypatch.setattr(panel.shutil, "which", lambda _: "/usr/bin/agy")
    monkeypatch.setattr(panel, "run_cli",
                        lambda args, *a, **k: (seen.update(args=args), (FINDINGS, None))[1])
    run = panel.review_llm("antigravity", "gemini-3-pro", "p")
    assert run.usage is None and len(run.findings) == 1
    assert seen["args"] == panel.antigravity_args("gemini-3-pro", "", "p")


def test_a_real_timeout_still_hands_over_what_the_cli_printed():
    """The existing timeout test stubs `run_cli` and calls the callback by hand,
    so it covers the plumbing ABOVE `run_cli` and never the line that lost the
    number. This drives a real subprocess to a real `subprocess.TimeoutExpired`.

    A timeout is the most expensive outcome the panel has — the model read the
    whole diff and thought for the entire budget before being killed — and codex
    is the one seat whose usage is read only from stdout, so dropping it here
    recorded the costliest run as free. `TimeoutExpired.stdout` carries what was
    printed before the kill, as bytes even under `text=True`."""
    seen = []
    script = ("import sys, time; sys.stdout.write('partial output'); "
              "sys.stdout.flush(); time.sleep(30)")
    out, err = panel.run_cli([sys.executable, "-c", script],
                             "codex", timeout=1, on_output=seen.append)
    assert out is None and "timed out" in err
    assert seen and "partial output" in (seen[0] or "")


def test_each_codex_attempt_gets_its_own_reply_file(monkeypatch):
    """`--output-last-message` is written by the attempt that gets that far. On
    one shared path an attempt that wrote nothing served the PREVIOUS attempt's
    text as its own findings — a stale reply consumed as a later run's answer —
    and it made the reparse retry a guaranteed no-op for codex alone, since it
    re-read the same bytes while still costing a full turn of tokens.

    The reply is the LAST attempt's, matching `run_cli`, which returns the last
    attempt's stdout. A file that attempt never wrote is no reply, rather than
    whatever happens to be on disk."""
    seen = []

    def fake(args, *a, on_output=None, **k):
        # `run_cli` resolves the thunk once per attempt. Resolve it twice to
        # stand in for a run whose first attempt replied and whose second died
        # before writing.
        for _ in range(2):
            argv = args() if callable(args) else args
            seen.append(argv[argv.index("--output-last-message") + 1])
        Path(seen[0]).write_text(FINDINGS)      # only the first attempt replied
        out = codex_stream((90, 10, 20, 4))
        if on_output:
            on_output(out)
        return out, None

    monkeypatch.setattr(panel.shutil, "which", lambda _: "/usr/bin/codex")
    monkeypatch.setattr(panel, "run_cli", fake)
    run = panel.review_llm("codex", "gpt-5.6-luna", "p")
    assert len(set(seen)) == len(seen), "a fresh path per attempt, never reused"
    # Four, not two: the last attempt wrote no reply, so the reply is missing
    # rather than stale, the parse fails, and the reparse retry actually runs.
    # On a shared path it would have re-read the first attempt's bytes and been
    # a guaranteed no-op that still cost a turn.
    assert len(seen) == 4
    assert run.findings == [], "the last attempt's missing reply is not the first's"
    assert run.skip and "output" in run.skip
    # Both turns are charged — 90 twice. A retry that produced nothing still
    # spent what it spent, and this is the seat whose usage is read only from
    # stdout, so the losing turn has to count or the flaking seat looks cheap.
    assert run.usage["input_tokens"] == 180


def test_a_figure_a_vendor_spelled_as_a_float_is_still_a_figure():
    """`455.0` is ordinary in JSON emitted from a language with one number type,
    and neither codex's nor pi's schema is pinned here. Reading it as 0 was the
    worst available answer: `found` is set from the presence of a usage dict, not
    from a non-zero total, so the run was recorded as instrumented AND free — a
    zero the board cannot tell from a measured one. A fractional figure stays 0;
    no vendor bills 1.5 tokens, and truncating it would invent a number."""
    assert panel._int(455.0) == 455
    assert panel._int(1.5) == 0
    assert panel._int(True) == 0 and panel._int("455") == 0
    assert panel._int(455) == 455


def test_messages_that_identify_themselves_in_no_way_are_all_counted(monkeypatch, tmp_path):
    """The inverse of the double-count the dedup exists to stop, and the one that
    flatters an expensive seat. `msg.get("id") or requestId or uuid` fell through
    to None, so the first id-less assistant message went into `seen` and every
    later one in the turn was skipped as its duplicate."""
    sid = "sess-anon"
    anon = {"type": "assistant", "message": {"usage": {"input_tokens": 10, "output_tokens": 1}}}
    write_jsonl(tmp_path / ".claude/projects/-slug" / f"{sid}.jsonl", [anon, dict(anon)])
    monkeypatch.setattr(panel.Path, "home", classmethod(lambda cls: tmp_path))
    u = panel.claude_usage([sid])
    assert u["input_tokens"] == 20 and u["output_tokens"] == 2


def test_one_session_under_two_slugs_is_read_whole_and_the_same_way_twice(monkeypatch, tmp_path):
    """`Path.glob` returns filesystem order, so `files[0]` made the read
    nondeterministic — a different number on different runs with nothing to say
    which. Every match is read; the message-id dedup keeps a record seen twice
    from being charged twice."""
    sid = "sess-two-slugs"
    write_jsonl(tmp_path / ".claude/projects/-slug-a" / f"{sid}.jsonl",
                [claude_msg("m1", inp=10, out=5)])
    write_jsonl(tmp_path / ".claude/projects/-slug-b" / f"{sid}.jsonl",
                [claude_msg("m1", inp=10, out=5),        # the same message again
                 claude_msg("m2", inp=20, out=7)])
    monkeypatch.setattr(panel.Path, "home", classmethod(lambda cls: tmp_path))
    u = panel.claude_usage([sid])
    assert u["input_tokens"] == 30 and u["output_tokens"] == 12


def test_a_pi_session_rolled_into_a_second_file_is_charged_in_full(tmp_path):
    """`sorted(files)[0]` deliberately took the EARLIEST timestamp, so the later
    and larger half of a rolled turn was dropped and the seat under-charged with
    no signal that anything was missing."""
    write_jsonl(tmp_path / "2026-01-01T00-00_rolled.jsonl", [pi_record(100, 10)])
    write_jsonl(tmp_path / "2026-01-01T01-00_rolled.jsonl", [pi_record(200, 20)])
    u = panel.pi_usage(tmp_path, ["rolled"])
    assert u["input_tokens"] == 300 and u["output_tokens"] == 30


def test_a_codex_turn_that_wrote_no_reply_is_retried_not_accepted(monkeypatch):
    """`--json` cost codex the blank-reply guard, and nothing said so.

    `cli_outcome` asks whether STDOUT is empty, and under `--json` codex's stdout
    is ALWAYS non-empty — thread.started, item.completed, turn.completed. So the
    test could never fire for codex again, and with it went run_cli's up-to-3
    blank-reply retries and the BLANK_RETRY_MAX_S slow-blank rule, on the one
    seat whose reply lands in a file rather than on stdout. A turn that emitted
    events and wrote no `--output-last-message` was returned as a SUCCESS.

    That is v2.17's guarantee — "a reviewer that produced nothing has failed, and
    says why" — silently lost on the seat this release changes most."""
    attempts = []

    def fake_run(argv, **kw):
        attempts.append(argv)
        # Events on stdout, exactly as `--json` produces, and no reply file.
        return subprocess.CompletedProcess(argv, 0, codex_stream((90, 10, 20, 4)), "")

    monkeypatch.setattr(panel.subprocess, "run", fake_run)
    out, err = panel.run_cli(["codex", "exec"], "codex", replied=lambda: False)

    assert out is None
    assert "wrote no reply" in err
    assert len(attempts) == 3, "the blank-reply retries are alive again"


def test_a_seat_whose_stdout_is_its_reply_is_unaffected(monkeypatch):
    """`replied` is passed only for the file-delivering seat. Everywhere else
    `cli_outcome`'s stdout test is still exactly the right question, and a second
    predicate would be a second way to ask it."""
    def fake_run(argv, **kw):
        return subprocess.CompletedProcess(argv, 0, "real findings", "")

    monkeypatch.setattr(panel.subprocess, "run", fake_run)
    out, err = panel.run_cli(["claude", "-p"], "claude")
    assert out == "real findings" and err is None


def test_a_field_the_vendor_never_stated_is_absent_not_a_measured_zero(tmp_path):
    """The invariant the whole feature rests on — "null means not recorded, never
    spent nothing" — held per SEAT but not per FIELD, which is what `ReviewerIn`
    advertises ("all independently optional").

    `_usage` emitted all four keys unconditionally and `_int(u.get(...))` returns
    0 for an absent key, so pi omitting `reasoning`, or codex omitting the cache
    figure on a cold turn, both recorded a hard 0 that the board then stored,
    summed and rendered as a measurement."""
    write_jsonl(tmp_path / "ts_bare.jsonl",
                [{"type": "message",
                  "message": {"role": "assistant", "usage": {"input": 100, "output": 10}}}])
    u = panel.pi_usage(tmp_path, ["bare"])
    assert u == {"input_tokens": 100, "output_tokens": 10}
    assert "reasoning_tokens" not in u and "cached_input_tokens" not in u

    # A turn that DID state them still reports them, zero included: a stated zero
    # is a measurement and must survive.
    write_jsonl(tmp_path / "ts_full.jsonl",
                [pi_record(100, 10, cache_read=0, reasoning=0)])
    full = panel.pi_usage(tmp_path, ["full"])
    assert full["reasoning_tokens"] == 0 and full["cached_input_tokens"] == 0


def test_codex_omitting_the_cache_figure_reports_nothing_for_it():
    stream = json.dumps({"type": "turn.completed",
                         "usage": {"input_tokens": 90, "output_tokens": 20}})
    u = panel.codex_usage(stream)
    assert u == {"input_tokens": 90, "output_tokens": 20}
