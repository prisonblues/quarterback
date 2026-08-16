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
