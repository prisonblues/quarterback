"""The grok reviewer — a fifth vendor, and the one that keeps its read tools.

Two things here are unlike every other seat and are the reason this file exists.

Its prompt travels in a FILE. `agy` is the only member whose prompt has nowhere
to go but argv, and the whole argv-clamping path exists for it; grok looked like
a second one (its `-p` wants a flag value and it reads no stdin) and is not, so
these tests pin that it never becomes one.

And it is given read tools where codex and pi are stripped of them. That is a
reversal of this file's usual answer, made because a toolless grok does not review
quietly — it never emits findings at all, streaming its intention to open the file
until the CLI timeout. What makes the tools safe is the sandbox profile and the
denial of everything that is a network channel, so those are pinned too: a future
tidy-up that "simplifies" `--sandbox strict` to codex's `read-only` spelling, or
drops the MCP pair from the denylist, would restore a reviewer that can read the
real checkout or call the user's connectors.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import panel  # noqa: E402
import panel_seats  # noqa: E402  — run_cli lives here since #129

PROMPT_FILE = Path("/tmp/panel-test-grok/prompt.txt")


def grok_args(model="", effort=""):
    return panel.grok_args(model, effort, PROMPT_FILE)


def test_grok_is_a_panel_member():
    assert "grok" in panel.LLM_REVIEWERS
    assert "grok" in panel.ALL_REVIEWERS


def test_the_prompt_travels_in_a_file_not_argv():
    """The reason grok is not a second seat the kernel's argv limit binds. A
    change to `-p <prompt>` would put a whole diff in one argv element, which
    fails at execve with nothing in the error."""
    args = grok_args()
    assert args[args.index("--prompt-file") + 1] == str(PROMPT_FILE)
    assert "-p" not in args and "--single" not in args


def test_the_prompt_file_is_outside_the_repo_the_seat_can_see(monkeypatch):
    """Measured, not hypothetical: a trial run with the prompt inside the cwd had
    grok list the directory, find it, and read its own instructions back as the
    code under review."""
    seen = {}
    monkeypatch.setattr(panel.shutil, "which", lambda _: "/usr/bin/grok")

    def capture(args, *a, cwd=None, **k):
        argv = args() if callable(args) else args
        seen["prompt"] = Path(argv[argv.index("--prompt-file") + 1])
        seen["cwd"] = Path(cwd)
        assert seen["prompt"].is_file()      # it exists while the CLI runs
        return "[]", None

    monkeypatch.setattr(panel_seats, "run_cli", capture)
    panel.review_llm("grok", "grok-4.6", "review this")
    assert seen["cwd"] not in seen["prompt"].parents
    assert not seen["prompt"].exists()       # and is gone once the member returns


def test_the_prompt_is_sent_as_written(monkeypatch):
    """A review prompt is a diff full of `@@`, `@paths` and leading `/` — the
    syntax an expanding CLI reaches for. `--verbatim` is what stops it."""
    monkeypatch.setattr(panel.shutil, "which", lambda _: "/usr/bin/grok")
    written = {}

    def capture(args, *a, stdin_text=None, **k):
        argv = args() if callable(args) else args
        written["text"] = Path(argv[argv.index("--prompt-file") + 1]).read_text()
        written["stdin"] = stdin_text
        return "[]", None

    monkeypatch.setattr(panel_seats, "run_cli", capture)
    panel.review_llm("grok", "grok-4.6", "@@ -1 +1 @@\n/usr @app/x.py")
    assert written["text"] == "@@ -1 +1 @@\n/usr @app/x.py"
    assert written["stdin"] is None          # the file IS the channel
    assert "--verbatim" in grok_args()


def test_the_mcp_pair_is_denied_and_not_merely_left_off_the_allowlist():
    """`--tools` is documented to disable default tool injection and does not do
    it for `search_tool`/`use_tool`: a run given only `read_file` still enumerated
    31 quarterback MCP tools. grok reads MCP servers from `~/.claude.json` too, so
    the servers are the user's and no clean cwd closes this."""
    args = grok_args()
    denied = args[args.index("--disallowed-tools") + 1].split(",")
    assert "search_tool" in denied and "use_tool" in denied


def test_a_reviewer_does_not_spawn_a_second_brain_the_report_cannot_name():
    assert "Agent" in grok_args()[grok_args().index("--disallowed-tools") + 1]
    assert "--no-subagents" in grok_args()


def test_the_seat_gets_read_tools_and_nothing_that_writes_or_executes():
    """Kept on purpose — see the module docstring. What is NOT on the allowlist
    is the whole point: no shell, no edit/write, no web."""
    allowed = grok_args()[grok_args().index("--tools") + 1].split(",")
    assert set(allowed) == {"read_file", "grep", "list_dir"}
    assert "--disable-web-search" in grok_args()


def test_the_sandbox_bounds_reads_not_merely_writes():
    """`strict`, NOT codex's `read-only` spelling. grok's `read-only` profile
    leaves reads unrestricted at filesystem root, which is the hole that lets a
    seat review the real checkout instead of the diff it was handed."""
    assert grok_args()[grok_args().index("--sandbox") + 1] == "strict"


def test_permission_mode_is_pinned_because_the_fleets_config_says_yolo():
    """`~/.grok/config.toml` here sets `permission_mode = "always-approve"`. An
    unpinned seat inherits it and auto-approves every tool call, with no
    confirmation a headless run could withhold."""
    assert grok_args()[grok_args().index("--permission-mode") + 1] == "default"


def test_a_review_does_not_carry_into_the_next_one():
    assert "--no-memory" in grok_args()


def test_model_and_effort_are_pinned_with_groks_own_spellings():
    args = grok_args("grok-4.6", "high")
    assert args[-4:] == ["--model", "grok-4.6", "--reasoning-effort", "high"]


def test_unpinned_grok_passes_neither_flag():
    args = grok_args()
    assert "--model" not in args and "--reasoning-effort" not in args


def test_grok_takes_four_effort_levels_not_codexs_six():
    """Its CLI validates the level itself and exits before the turn starts, so a
    typo costs a startup rather than a turn — but only if this set matches."""
    assert panel.EFFORTS["grok"] == ("low", "medium", "high", "xhigh")
    assert "max" not in panel.EFFORTS["grok"]


def test_an_unknown_effort_is_answered_before_any_process_starts(monkeypatch):
    monkeypatch.setattr(panel.shutil, "which", lambda _: "/usr/bin/grok")
    monkeypatch.setattr(panel_seats, "run_cli",
                        lambda *a, **k: pytest.fail("the CLI must not run"))
    run = panel.review_llm("grok", "grok-4.6", "p", effort="max")
    assert "max" in run.skip and "xhigh" in run.skip


def test_the_reply_is_stdout_not_a_file(monkeypatch):
    """codex is the one seat whose stdout is not its reply. grok's is, so the
    stdout-emptiness test stays the right question for it."""
    monkeypatch.setattr(panel.shutil, "which", lambda _: "/usr/bin/grok")
    monkeypatch.setattr(panel_seats, "run_cli",
                        lambda *a, replied=None, **k: (
                            '{"findings": [], "could_not_assess": []}', None)
                        if replied is None else (_ for _ in ()).throw(
                            AssertionError("grok must not be a reply-file seat")))
    run = panel.review_llm("grok", "grok-4.6", "p")
    assert run.skip is None
    assert run.findings == []
