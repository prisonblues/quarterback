"""The pi reviewer — a fourth vendor, and the first one that fronts many.

pi is not another single-vendor CLI: it reaches providers the other three can't
(kimi, deepseek, inkling…), which is the reason to have it on the panel at all.
That shapes its config — `model` is a full `provider/id` pattern rather than a
bare slug — and it means the read-only contract has to be enforced differently,
because pi ships edit and write tools where agy has a plan mode.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import panel  # noqa: E402
import panel_seats  # noqa: E402  — run_cli lives here since #129


def test_pi_is_a_panel_member():
    assert "pi" in panel.LLM_REVIEWERS
    assert "pi" in panel.ALL_REVIEWERS


SID = "panel-test"
SDIR = Path("/tmp/panel-test-session")


def pi_args(model="", effort=""):
    """pi_args with this test module's fixed session, which every call needs."""
    return panel.pi_args(model, effort, SID, SDIR)


def test_a_reviewer_may_not_edit_the_tree_it_reviews():
    """pi ships read/bash/edit/write. The panel wants an opinion, not a fix — and
    the diff arrives on stdin, so it needs no tools to form one. Unlike agy's
    `--mode plan`, this one is a real guarantee."""
    assert "--no-tools" in pi_args()


def test_a_review_is_not_a_conversation():
    """One run per PR, resumed by nobody — so it stays out of the user's session
    store. `--no-session` used to say that by throwing the session away; the
    session is now kept, because it is where the turn's token usage is read from,
    and the same guarantee is met by writing it somewhere private instead."""
    args = pi_args()
    assert "--no-session" not in args
    assert args[args.index("--session-id") + 1] == SID
    assert args[args.index("--session-dir") + 1] == str(SDIR)


def test_the_session_is_pinned_up_front_not_matched_afterwards():
    """`/panel-review-pr` fans out up to 4 concurrent panels, each running its
    own pi. Finding "our" session afterwards by mtime would hand one panel
    another's numbers, so the id is chosen before the CLI starts."""
    assert "--session-id" in pi_args()


def test_a_reviews_session_never_reaches_the_users_store(monkeypatch):
    """The private directory is per-run and temporary: a panel that runs all day
    must not leave a session behind for every PR it looked at."""
    seen = {}
    monkeypatch.setattr(panel.shutil, "which", lambda _: "/usr/bin/pi")

    def capture(args, *a, **k):
        # A thunk, because each attempt needs its own session id.
        argv = args() if callable(args) else args
        seen["dir"] = Path(argv[argv.index("--session-dir") + 1])
        assert seen["dir"].is_dir()          # it exists while the CLI runs
        return "[]", None

    monkeypatch.setattr(panel_seats, "run_cli", capture)
    panel.review_llm("pi", "openrouter/moonshotai/kimi-k3", "p")
    assert not seen["dir"].exists()          # and is gone once the member returns


def test_model_is_a_provider_qualified_pattern():
    """The distinguishing feature: `openrouter/moonshotai/kimi-k3`, not a slug.
    Passed through verbatim, since pi resolves the provider, not the panel."""
    args = pi_args("openrouter/moonshotai/kimi-k3")
    assert args[args.index("--model") + 1] == "openrouter/moonshotai/kimi-k3"


def test_effort_is_one_config_key_spelled_differently_per_cli():
    """`effort` in .harness-rules -> `--thinking` here, `model_reasoning_effort`
    on codex. Same knob, so it keeps the same config name."""
    assert pi_args(effort="high")[-2:] == ["--thinking", "high"]
    assert panel.codex_args("", "high")[-2:] == ["-c", "model_reasoning_effort=high"]


def test_unpinned_pi_passes_neither_flag():
    assert pi_args() == ["pi", "-p", "--session-id", SID, "--session-dir", str(SDIR),
                         "--no-tools"]


def test_effort_sets_are_per_cli_not_unioned(monkeypatch):
    """pi has off/minimal, codex has ultra. Validating against a union would
    accept `ultra` for pi and `off` for codex, and each would fail at the CLI
    with a worse message than the config error it actually is."""
    assert "off" in panel.PI_EFFORTS and "off" not in panel.CODEX_EFFORTS
    assert "ultra" in panel.CODEX_EFFORTS and "ultra" not in panel.PI_EFFORTS

    called = []
    monkeypatch.setattr(panel_seats, "run_cli", lambda *a, **k: called.append(a) or (None, None))
    got = panel.review_llm("pi", "openrouter/moonshotai/kimi-k3", "p", effort="ultra")
    assert called == [] and "unknown reasoning effort" in got.skip and "minimal" in got.skip


def test_a_cli_with_no_effort_knob_says_so(monkeypatch):
    """claude takes no reasoning level. Setting one is a config error worth
    naming, not a flag to quietly drop on the floor."""
    called = []
    monkeypatch.setattr(panel_seats, "run_cli", lambda *a, **k: called.append(a) or (None, None))
    got = panel.review_llm("claude", "sonnet", "p", effort="high")
    assert called == [] and "takes no reasoning effort" in got.skip
