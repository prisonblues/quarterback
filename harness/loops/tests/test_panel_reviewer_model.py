"""Pinning a codex model/effort, and failing legibly when the pin doesn't work.

The panel's Claude reviewer is pinned to the floating alias `opus`; Codex is
pinned (if at all) to a versioned build name that gets retired. So the pin's
failure path is the part that has to be right: a model the installed CLI is too
old for is refused by the API, and the panel must say THAT rather than blame
auth and drop a whole vendor into a footnote.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import panel  # noqa: E402

TOO_OLD = (
    'ERROR: {"type":"error","status":400,"error":{"type":"invalid_request_error",'
    '"message":"The \'gpt-5.6-luna\' model requires a newer version of Codex. '
    'Please upgrade to the latest app or CLI and try again."}}'
)


# ---------------------------------------------------------------- argv

def test_unpinned_codex_passes_no_model_flag():
    """Empty == the CLI's own default, which is the global default: no --model."""
    assert panel.codex_args("", "") == ["codex", "exec"]


def test_model_and_effort_are_independent():
    """Effort is a `-c` override, not a flag, and applies to the default model
    too — so someone can raise reasoning without pinning a slug that will rot."""
    assert panel.codex_args("", "high")[-2:] == ["-c", "model_reasoning_effort=high"]
    assert panel.codex_args("gpt-5.6-luna", "") == [
        "codex", "exec", "--model", "gpt-5.6-luna"]
    assert panel.codex_args("gpt-5.6-luna", "high") == [
        "codex", "exec", "--model", "gpt-5.6-luna",
        "-c", "model_reasoning_effort=high"]


# ---------------------------------------------------------------- failing cleanly

def test_typo_effort_is_refused_without_spending_a_run(monkeypatch):
    """A config error is answered as one, before three CLI invocations discover
    it downstream and report it as an opaque non-zero exit."""
    called = []
    monkeypatch.setattr(panel, "run_cli", lambda *a, **k: called.append(a) or (None, None))
    got = panel.review_llm("codex", "gpt-5.6-luna", "p", effort="hi")
    assert got.findings == [] and called == []
    assert "unknown reasoning effort" in got.skip and "'hi'" in got.skip
    assert "xhigh" in got.skip                      # the valid set is stated, not implied


def test_panel_re_exports_the_shared_cli_failure_plumbing():
    """`stderr_gist` and `cli_outcome` live in harness_rules — how headless CLIs
    fail is not a panel question — and are re-exported here because they read as
    part of run_cli's contract. All a re-export owes anyone is being the same
    object; the behaviour is tested where the function lives
    (test_harness_rules.py), so deleting that copy and growing a private one back
    fails there rather than passing here for the wrong reason."""
    import harness_rules

    assert panel.stderr_gist is harness_rules.stderr_gist
    assert panel.cli_outcome is harness_rules.cli_outcome


def test_hint_blames_the_pin_not_the_login():
    """The old code appended '(auth? run `codex login`)' to every non-zero exit —
    a confident wrong answer for exactly the failure a pin is likeliest to cause."""
    err = f"codex (gpt-5.6-luna, high): exited 1 ({TOO_OLD})"
    hint = panel.cli_hint("codex", err, "gpt-5.6-luna")
    assert "gpt-5.6-luna" in hint and "upgrade the CLI" in hint
    assert "codex login" not in hint


def test_hint_still_offers_login_for_a_real_auth_failure():
    err = "codex (CLI default): exited 1 (Provided authentication token is expired.)"
    assert "codex login" in panel.cli_hint("codex", err, "")


def test_label_names_the_model_that_ran():
    """'codex ran' is not the same claim as 'codex ran on the model you pinned'."""
    assert panel.reviewer_label("codex", "gpt-5.6-luna", "high") == "codex (gpt-5.6-luna, high)"
    assert panel.reviewer_label("codex", "") == "codex (CLI default)"
    assert panel.reviewer_label("claude", "opus") == "claude (opus)"
