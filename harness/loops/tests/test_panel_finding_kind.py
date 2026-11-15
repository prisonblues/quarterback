"""The judge types each finding as a regression, a claim_miss or an observation (#623).

This slice records the type and nothing more: no stop rule, floor or budget reads it.
"""

import ast
import inspect
import re

import panel
import panel_core
import panel_rounds
import test_panel_dials as dials


def _said():
    return panel_rounds.Finding(reviewer="codex", severity="P2", file="a.py",
                                line=9, title="a defect", detail="")


def _verdict(**extra):
    return {"id": "F01", "members": [0], "real": True, "severity": "P2",
            "file": "a.py", "line": 9, "synthesis": "merged", "reason": "why", **extra}


def test_the_judge_is_asked_for_a_kind_and_the_schema_accepts_exactly_the_three():
    prompt = panel_core.JUDGE_PROMPT.format(findings="F", coverage="C", ci="", diff="D")
    assert '"kind": "regression|claim_miss|observation"' in prompt
    assert "THIS IS THE DEFAULT AND THE COMMON CASE" in prompt.split("KIND.")[1]
    item = panel_core.JUDGE_SCHEMA["properties"]["verdicts"]["items"]
    assert item["properties"]["kind"]["enum"] == list(panel_core.FINDING_KINDS)
    assert "kind" not in item["required"]


def test_a_typed_verdict_carries_its_kind_and_evidence_onto_the_record():
    [c] = panel_rounds._parse_verdicts(
        [_verdict(kind="regression", evidence="test_x fails: 1 != 2")], [_said()], 34)
    d = c.as_dict()
    assert (d.get("kind"), d.get("kind_said"), d.get("evidence")) == (
        "regression", "regression", "test_x fails: 1 != 2")


def test_a_regression_with_nothing_shown_is_counted_as_an_observation():
    """The decided rule: a regression only on a demonstrated failure. The judge's
    own word is kept beside the count so the downgrade can be seen."""
    [c] = panel_rounds._parse_verdicts(
        [_verdict(kind="regression", evidence="  ")], [_said()], 34)
    d = c.as_dict()
    assert (d.get("kind"), d.get("kind_said"), d.get("evidence")) == (
        "observation", "regression", "")


def test_an_unreadable_or_missing_kind_is_untyped_not_an_observation():
    for raw in ({}, {"kind": "bug"}, {"kind": ["regression"]}, {"kind": None}):
        [c] = panel_rounds._parse_verdicts([_verdict(**raw)], [_said()], 34)
        d = c.as_dict()
        assert (d.get("kind"), d.get("kind_said")) == ("", ""), raw


def test_a_finding_the_judge_never_ruled_on_is_untyped():
    [c] = panel_rounds._parse_verdicts([], [_said()], 34)
    assert c.verdict == "unjudged"
    assert c.as_dict().get("kind") == ""


def test_the_kind_normaliser_is_case_blind_and_keeps_an_observation_without_evidence():
    assert panel_core._finding_kind(" Claim_Miss ", '"it does X"') == (
        "claim_miss", "claim_miss", '"it does X"')
    assert panel_core._finding_kind("observation", "") == ("observation", "observation", "")


def _canon(kind="", kind_said=""):
    return panel_rounds.Canonical(id="x", severity="P3", file="a.py", line=1,
                                  synthesis="s", verdict="confirmed",
                                  kind=kind, kind_said=kind_said or kind)


def test_the_summary_counts_kinds_and_the_downgrades():
    line = panel.kind_line([_canon("regression"), _canon("observation"),
                            _canon("observation", "claim_miss"), _canon()])
    assert line == "_Kinds: regression 1 · observation 2 · untyped 1 · downgraded 1_"


def test_the_kinds_line_is_only_label_count_pairs():
    """The structural answer to five wording findings: a fixed table has no sentence
    to drift from what it counted."""
    labels = "|".join(panel.KIND_LINE_LABELS)
    pair = rf"(?:{labels}) \d+"
    shape = re.compile(rf"^_Kinds: {pair}(?: · {pair})*_$")
    for kinds in (["regression"], ["observation"] * 3, ["claim_miss", ""],
                  ["observation", "regression", "claim_miss"]):
        line = panel.kind_line([_canon(k) for k in kinds])
        assert shape.match(line), line
    assert shape.match(panel.kind_line([_canon("observation", "regression")] * 2))


def test_a_round_the_judge_did_not_type_prints_no_kind_line():
    assert panel.kind_line([_canon(), _canon()]) == ""
    assert panel.kind_line([]) == ""


def test_a_claim_miss_with_nothing_quoted_is_counted_as_an_observation():
    [c] = panel_rounds._parse_verdicts([_verdict(kind="claim_miss")], [_said()], 34)
    d = c.as_dict()
    assert (d.get("kind"), d.get("kind_said")) == ("observation", "claim_miss")


def test_a_null_evidence_is_valid_to_the_schema_and_reads_as_empty():
    item = panel_core.JUDGE_SCHEMA["properties"]["verdicts"]["items"]
    assert "null" in item["properties"]["evidence"]["type"]
    assert panel_core._finding_kind("observation", None) == ("observation", "observation", "")


def test_two_judge_candidates_that_differ_only_in_kind_are_two_readings():
    a = panel_core._verdict_reading(_verdict(kind="regression", evidence="test_x fails"))
    b = panel_core._verdict_reading(_verdict(kind="claim_miss", evidence="test_x fails"))
    assert a != b


def test_every_parsed_field_is_compared_and_the_parser_reads_only_through_it():
    """The structural answer to a field parsed but not compared (three findings over
    two rounds, `premise` among them): the parser reads a verdict ONLY through
    `_verdict_fields`, and the reading is built from every field it returns."""
    fields = panel_core._verdict_fields(_verdict(kind="regression", evidence="e"))
    reading = panel_core._verdict_reading(_verdict(kind="regression", evidence="e"))
    assert reading == tuple(v for k, v in fields.items() if k != "members_raw")
    tree = ast.parse(inspect.getsource(panel_rounds._parse_verdicts))
    direct = [n.lineno for n in ast.walk(tree)
              if isinstance(n, (ast.Subscript, ast.Attribute))
              and isinstance(getattr(n, "value", None), ast.Name) and n.value.id == "v"]
    assert direct == [], f"_parse_verdicts reads `v` directly at lines {direct}"


def test_two_candidates_that_differ_only_in_premise_are_two_readings():
    assert (panel_core._verdict_reading(_verdict(premise="separate"))
            != panel_core._verdict_reading(_verdict(premise="invalidates")))


def test_the_rendered_list_counts_only_the_rows_it_lists(monkeypatch, capsys, tmp_path):
    """A finding below the fix floor is listed elsewhere, so the kinds line under
    **To fix** must not count it."""
    def adjudicate(clusters, *a, **k):
        flat = [f for grp in clusters for f in grp]
        kinds = {"P2": ("regression", "test_y fails"), "P4": ("observation", "")}
        return ([panel.Canonical(id=panel._finding_id(34, i + 1), severity=f.severity,
                                 file=f.file, line=f.line, synthesis=f.title,
                                 verdict="confirmed", reported_by=[f], rationale="real",
                                 kind=kinds[f.severity][0], kind_said=kinds[f.severity][0],
                                 evidence=kinds[f.severity][1])
                 for i, f in enumerate(flat)], None, panel.CoverageRuling())

    dials.stub(monkeypatch, [dials.finding("P2", "breaks y"),
                             dials.finding("P4", "could be general", file="b.py")],
               config=dials.cfg(fix_severity_floor="P3"))
    monkeypatch.setattr(panel, "adjudicate", adjudicate)
    out = tmp_path / "r1.json"
    assert panel.run("board", 34, post=False, json_file=str(out), record=False,
                     round_no=1, baseline=[], max_rounds=2, scope="auto") == 0
    report = capsys.readouterr().out
    assert "### To fix (1)" in report
    assert "_Kinds: regression 1_" in report
    assert "**P2** _regression_" in report
