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
    assert panel.codex_args("", "", "review this") == ["codex", "exec", "review this"]


def test_model_and_effort_are_independent():
    """Effort is a `-c` override, not a flag, and applies to the default model
    too — so someone can raise reasoning without pinning a slug that will rot."""
    assert panel.codex_args("", "high", "p")[-2:] == ["-c", "model_reasoning_effort=high"]
    assert panel.codex_args("gpt-5.6-luna", "", "p") == [
        "codex", "exec", "p", "--model", "gpt-5.6-luna"]
    assert panel.codex_args("gpt-5.6-luna", "high", "p") == [
        "codex", "exec", "p", "--model", "gpt-5.6-luna",
        "-c", "model_reasoning_effort=high"]


# ---------------------------------------------------------------- failing cleanly

def test_typo_effort_is_refused_without_spending_a_run(monkeypatch):
    """A config error is answered as one, before three CLI invocations discover
    it downstream and report it as an opaque non-zero exit."""
    called = []
    monkeypatch.setattr(panel, "run_cli", lambda *a, **k: called.append(a) or (None, None))
    finds, skip, _ms = panel.review_llm("codex", "gpt-5.6-luna", "p", effort="hi")
    assert finds == [] and called == []
    assert "unknown reasoning effort" in skip and "'hi'" in skip
    assert "xhigh" in skip                      # the valid set is stated, not implied


def test_stderr_gist_lifts_the_api_sentence_over_housekeeping():
    """A codex older than its own models cache logs a decode error on EVERY run;
    the naive stderr tail reported that and buried the real complaint."""
    noisy = "\n".join([
        "ERROR codex_models_manager::cache: failed to load models cache: unknown variant `max`",
        TOO_OLD,
        "ERROR codex_api::endpoint::responses_websocket: failed to connect to websocket",
    ])
    assert panel.stderr_gist(noisy) == (
        "The 'gpt-5.6-luna' model requires a newer version of Codex. "
        "Please upgrade to the latest app or CLI and try again.")


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
