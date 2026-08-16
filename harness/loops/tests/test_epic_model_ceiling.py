"""How capable a model an epic may spend on a sub-issue is its own setting.

`epic.run()` used to read `review_panel.judge_model` for this, and the two keys
agreed only by accident: one answers "which brain adjudicates the panel", the
other "how much capability may an unattended process spend on an implementation".
Once the judge was deliberately moved to a model no seat uses (#81), that accident
became a defect — a judge of `fable` would have routed every sub-issue's
implementation at the top tier, unannounced, on every repo running the defaults.

The first version of this test asserted relationships between `DEFAULTS` literals,
which cannot catch the thing that would actually break: a typo'd key, a restored
fallback to `judge_model`, or the expression being dropped. Those are wiring, and
wiring has to be called. Hence `resolve_ceiling`, and hence these.
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import epic  # noqa: E402
import harness_rules as hr  # noqa: E402


def test_an_explicit_model_wins_over_the_setting():
    """`--model` is the tier the epic was initiated with; the setting is the
    fallback for when nobody said."""
    cfg = {"epic": {"model_ceiling": "sonnet"}}
    assert epic.resolve_ceiling(cfg, "opus") == "opus"


def test_the_setting_is_used_when_no_model_was_given():
    cfg = {"epic": {"model_ceiling": "sonnet"}}
    assert epic.resolve_ceiling(cfg, None) == "sonnet"


def test_a_repo_with_no_epic_block_gets_the_default_not_nothing():
    """The deep merge in resolve_repo() means this cannot happen through the real
    load path — but the function must not depend on that, because the day `epic`
    leaves _DEEP_BLOCKS is the day every repo silently loses model routing."""
    assert epic.resolve_ceiling({}, None) == hr.DEFAULTS["epic"]["model_ceiling"]


def test_the_judge_model_no_longer_influences_the_ceiling():
    """The regression this PR exists to prevent. A repo that tunes its judge must
    not thereby tune what its unattended implementations may cost."""
    cfg = {"epic": {"model_ceiling": "sonnet"},
           "review_panel": {"judge_model": "fable"}}
    assert epic.resolve_ceiling(cfg, None) == "sonnet"


def test_an_explicitly_empty_ceiling_turns_routing_off_and_stays_off():
    """`""` is a repo asking for no model routing, and it is not the same as the
    key being absent. Writing the fallback as `x or DEFAULT` — which is the
    obvious form, and was suggested in review — collapses the two and turns
    routing back on at the top tier for exactly the repos that asked for none."""
    assert epic.resolve_ceiling({"epic": {"model_ceiling": ""}}, None) == ""
    assert epic.allowed_models(epic.resolve_ceiling(
        {"epic": {"model_ceiling": ""}}, None)) == []


def test_a_null_ceiling_is_off_rather_than_a_None_leaking_downstream():
    """JSON `null` is the same request as `""` and must arrive as a string: the
    tier lookups carry this value into `w.model` and into the run state file."""
    ceiling = epic.resolve_ceiling({"epic": {"model_ceiling": None}}, None)
    assert ceiling == ""
    assert isinstance(ceiling, str)
    assert epic.allowed_models(ceiling) == []


def test_an_unrecognised_ceiling_turns_routing_off_as_documented():
    """`harness/loops/README.md` promises that anything outside MODEL_TIERS turns
    routing off. That promise is about `allowed_models`, so it is asserted against
    the real constant rather than against a repeated list."""
    assert epic.resolve_ceiling({"epic": {"model_ceiling": "haiku"}}, None) == "haiku"
    assert epic.allowed_models("haiku") == []


def test_the_documented_tiers_are_the_ones_the_constant_holds():
    """The README states the ordering `sonnet < opus < fable` in prose. Nothing
    tied that sentence to the constant, so a fourth tier would have gone stale
    silently — and no reviewer on this PR's panel could confirm `fable` was even
    in it."""
    assert epic.MODEL_TIERS == ["sonnet", "opus", "fable"]
    assert hr.DEFAULTS["epic"]["model_ceiling"] in epic.MODEL_TIERS
    assert epic.allowed_models("fable") == ["sonnet", "opus", "fable"]
    assert epic.allowed_models("sonnet") == ["sonnet"]


# ------------------------------------------------ through the real load path

def _repo(tmp_path, name):
    """A checkout `resolve_repo` will accept: it insists on an `origin` remote,
    because the harness addresses repos as `gh --repo owner/name`."""
    import subprocess

    repo = tmp_path / name
    repo.mkdir()
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(tmp_path),
           "GIT_CONFIG_GLOBAL": str(tmp_path / ".gitconfig"),
           "GIT_CONFIG_SYSTEM": str(tmp_path / ".gitconfig-system"),
           "GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@example.com",
           "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@example.com"}
    (tmp_path / ".gitconfig").write_text("")
    (tmp_path / ".gitconfig-system").write_text("")

    def git(*args):
        subprocess.run(["git", "-C", str(repo), *args], check=True, env=env,
                       capture_output=True)

    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True, env=env)
    git("commit", "-q", "--allow-empty", "-m", "init")
    git("remote", "add", "origin", "https://github.com/acme/proj.git")
    return repo

def test_a_real_rules_file_reaches_resolve_ceiling_with_the_default_merged_in(tmp_path):
    """Everything above builds `cfg` by hand, which cannot catch the assumption the
    whole design rests on: that `epic` is deep-merged, so a repo declaring an
    `epic` block without `model_ceiling` still gets the default. Round 2 was right
    that nothing exercised it — and no reviewer could confirm `_DEEP_BLOCKS`'
    membership from the diff either, which is the same gap seen from outside.

    So this one goes through `resolve_repo`: a real rules file, a real merge, and
    the resolution called on the result."""
    import json

    repo = _repo(tmp_path, "repo")

    # An epic block that says something ELSE — the case that would lose model
    # routing entirely if `epic` were ever dropped from _DEEP_BLOCKS.
    (repo / hr.RULES_FILENAME).write_text(json.dumps({"epic": {"auto_finish": True}}))
    cfg = hr.resolve_repo(str(repo))
    assert cfg["epic"]["auto_finish"] is True
    assert epic.resolve_ceiling(cfg, None) == hr.DEFAULTS["epic"]["model_ceiling"]

    # And an explicit ceiling in a real file survives the merge unchanged.
    (repo / hr.RULES_FILENAME).write_text(
        json.dumps({"epic": {"model_ceiling": "sonnet"}}))
    assert epic.resolve_ceiling(hr.resolve_repo(str(repo)), None) == "sonnet"


def test_the_judge_model_a_real_rules_file_sets_does_not_move_the_ceiling(tmp_path):
    """The regression this PR exists to prevent, asserted through the loader
    rather than through a hand-built dict."""
    import json

    repo = _repo(tmp_path, "repo2")
    (repo / hr.RULES_FILENAME).write_text(
        json.dumps({"review_panel": {"judge_model": "fable"}}))
    cfg = hr.resolve_repo(str(repo))
    assert cfg["review_panel"]["judge_model"] == "fable"
    assert epic.resolve_ceiling(cfg, None) == hr.DEFAULTS["epic"]["model_ceiling"]
