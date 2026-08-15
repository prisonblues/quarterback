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


def test_pi_is_a_panel_member():
    assert "pi" in panel.LLM_REVIEWERS
    assert "pi" in panel.ALL_REVIEWERS


def test_a_reviewer_may_not_edit_the_tree_it_reviews():
    """pi ships read/bash/edit/write. The panel wants an opinion, not a fix — and
    the diff is in the prompt, so it needs no tools to form one. This is pi's
    equivalent of agy's `--mode plan`."""
    args = panel.pi_args("", "", "review this")
    assert "--no-tools" in args


def test_a_review_is_not_a_conversation():
    """One run per PR, resumed by nobody — so it stays out of the session store."""
    assert "--no-session" in panel.pi_args("", "", "p")


def test_model_is_a_provider_qualified_pattern():
    """The distinguishing feature: `openrouter/moonshotai/kimi-k3`, not a slug.
    Passed through verbatim, since pi resolves the provider, not the panel."""
    args = panel.pi_args("openrouter/moonshotai/kimi-k3", "", "p")
    assert args[args.index("--model") + 1] == "openrouter/moonshotai/kimi-k3"


def test_effort_is_one_config_key_spelled_differently_per_cli():
    """`effort` in .harness-rules -> `--thinking` here, `model_reasoning_effort`
    on codex. Same knob, so it keeps the same config name."""
    assert panel.pi_args("", "high", "p")[-2:] == ["--thinking", "high"]
    assert panel.codex_args("", "high", "p")[-2:] == ["-c", "model_reasoning_effort=high"]


def test_unpinned_pi_passes_neither_flag():
    assert panel.pi_args("", "", "p") == ["pi", "-p", "--no-session", "--no-tools", "p"]


def test_effort_sets_are_per_cli_not_unioned(monkeypatch):
    """pi has off/minimal, codex has ultra. Validating against a union would
    accept `ultra` for pi and `off` for codex, and each would fail at the CLI
    with a worse message than the config error it actually is."""
    assert "off" in panel.PI_EFFORTS and "off" not in panel.CODEX_EFFORTS
    assert "ultra" in panel.CODEX_EFFORTS and "ultra" not in panel.PI_EFFORTS

    called = []
    monkeypatch.setattr(panel, "run_cli", lambda *a, **k: called.append(a) or (None, None))
    got = panel.review_llm("pi", "openrouter/moonshotai/kimi-k3", "p", effort="ultra")
    assert called == [] and "unknown reasoning effort" in got.skip and "minimal" in got.skip


def test_a_cli_with_no_effort_knob_says_so(monkeypatch):
    """claude takes no reasoning level. Setting one is a config error worth
    naming, not a flag to quietly drop on the floor."""
    called = []
    monkeypatch.setattr(panel, "run_cli", lambda *a, **k: called.append(a) or (None, None))
    got = panel.review_llm("claude", "sonnet", "p", effort="high")
    assert called == [] and "takes no reasoning effort" in got.skip
