"""Choosing the panel's members, and the antigravity reviewer's argv.

Two things are being pinned here. First, that `--reviewers` REPLACES the repo's
configured set rather than filtering it — the whole point of the flag is to run
a reviewer the rules have switched off, so a version that could only narrow
would be useless in exactly the repo you reach for it in. Second, that a name
the panel doesn't know is refused loudly: silently dropping `antigravty` produces a
one-reviewer panel whose report reads like a two-reviewer one, which is the
failure mode the whole "a lost reviewer is shouted" design exists to prevent.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import panel  # noqa: E402

# A repo that runs claude+codex and has deliberately turned the others off.
RULES = {
    "claude": {"enabled": True, "model": "opus"},
    "codex": {"enabled": True, "model": "", "effort": ""},
    "antigravity": {"enabled": False, "model": "", "effort": ""},
    "sonarqube": {"enabled": False},
}


# ---------------------------------------------------------------- selection

def test_no_flag_uses_the_repos_own_rules():
    selected, note = panel.select_reviewers(RULES, None)
    assert selected == {"claude", "codex"}
    assert note is None          # nothing overridden -> nothing to announce


def test_flag_replaces_rather_than_filters():
    """`--reviewers antigravity` runs it in a repo whose rules disable it."""
    selected, note = panel.select_reviewers(RULES, "antigravity")
    assert selected == {"antigravity"}
    assert "antigravity" in note and "overridden" in note


def test_single_vendor_read_is_one_reviewer():
    """The /panel-review-pr 'just the codex part' case."""
    assert panel.select_reviewers(RULES, "codex")[0] == {"codex"}


def test_whitespace_and_case_are_tolerated():
    selected, _ = panel.select_reviewers(RULES, " Codex , ANTIGRAVITY ")
    assert selected == {"codex", "antigravity"}


def test_sonarqube_is_selectable_too():
    assert "sonarqube" in panel.select_reviewers(RULES, "codex,sonarqube")[0]


def test_unknown_reviewer_is_refused_not_dropped():
    """A typo must not degrade the panel silently."""
    with pytest.raises(SystemExit) as e:
        panel.select_reviewers(RULES, "codex,antigravty")
    assert "antigravty" in str(e.value)
    assert "antigravity" in str(e.value)  # the valid set is stated, not implied


def test_empty_list_is_refused():
    with pytest.raises(SystemExit):
        panel.select_reviewers(RULES, "  ,  ")


def test_a_repo_missing_a_reviewer_block_entirely_is_fine():
    """An older .harness-rules predating antigravity must not KeyError."""
    assert panel.select_reviewers({"claude": {"enabled": True}}, None)[0] == {"claude"}


# ----------------------------------------------------------- antigravity argv

def test_antigravity_runs_headless_with_the_prompt_in_argv():
    """-p is the non-interactive mode, and this is the ONE seat whose prompt has
    to travel in argv — agy reads one from nowhere else."""
    args = panel.antigravity_args("", "", "review this", timeout=1800)
    assert args == ["agy", "--mode", "plan", "--print-timeout", "1800s",
                    "-p", "review this"]


def test_antigravity_is_told_the_same_deadline_the_panel_is_waiting():
    """agy self-aborts at its own --print-timeout (default 5m0s) regardless of
    how long run_cli is prepared to wait. Unset, the seat reviews on a
    five-minute clock while the report claims the panel's full budget."""
    args = panel.antigravity_args("", "", "p", timeout=900)
    assert args[args.index("--print-timeout") + 1] == "900s"
    assert panel.antigravity_args("", "", "p")[
        panel.antigravity_args("", "", "p").index("--print-timeout") + 1
    ] == f"{panel.CLI_TIMEOUT}s"


def test_antigravity_pins_model_and_effort():
    """agy spells these --model/--effort. Getting either wrong costs a whole
    vendor at runtime, and the CLI is absent on some fleet machines, so the
    failure would look like 'skipped' rather than 'you passed a bad flag'."""
    args = panel.antigravity_args("gemini-3-pro", "high", "p")
    assert args[-4:] == ["--model", "gemini-3-pro", "--effort", "high"]


def test_unpinned_antigravity_passes_neither_flag():
    args = panel.antigravity_args("", "", "p")
    assert "--model" not in args and "--effort" not in args


def test_antigravity_binary_is_agy_not_the_reviewer_name():
    """The seat is named for the vendor; the executable is `agy`. A which() on
    the reviewer name would report every host as 'CLI absent'."""
    assert panel.CLI_BIN["antigravity"] == "agy"
    assert panel.antigravity_args("", "", "p")[0] == "agy"
