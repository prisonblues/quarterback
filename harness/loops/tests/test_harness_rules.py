"""Tests for harness_rules — the per-repo .harness-rules resolver.

The interesting one is the two-ref rule: an unattended run must read rules from
the DEFAULT BRANCH, never the working tree, or an upstream-authored dependabot
branch could rewrite the policy governing its own review.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import harness_rules as hr  # noqa: E402


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args],
                          capture_output=True, text=True, check=True)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A real git repo with an 'origin' remote, on branch 'main'.

    origin is a bare clone on disk so `git show origin/main:...` and the
    remote-HEAD detection both behave like the real thing.
    """
    monkeypatch.delenv("HARNESS_UNATTENDED", raising=False)
    work = tmp_path / "myrepo"
    work.mkdir()
    git(work, "init", "-q", "-b", "main")
    git(work, "config", "user.email", "t@example.com")
    git(work, "config", "user.name", "T")
    (work / "README").write_text("x\n")
    git(work, "add", "-A")
    git(work, "commit", "-qm", "init")

    bare = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
    git(work, "remote", "add", "origin", f"https://github.com/acme/myrepo.git")
    git(work, "remote", "set-url", "origin", str(bare))
    git(work, "push", "-q", "origin", "main")
    git(work, "remote", "set-head", "origin", "main")
    return work


def write_rules(repo, obj, commit=False):
    (repo / hr.RULES_FILENAME).write_text(json.dumps(obj))
    if commit:
        git(repo, "add", "-A")
        git(repo, "commit", "-qm", "rules")
        git(repo, "push", "-q", "origin", "main")


# --------------------------------------------------------------- resolution

def test_no_rules_file_yields_defaults(repo):
    cfg = hr.resolve_repo(str(repo))
    assert cfg["auto_merge"] == hr.DEFAULTS["auto_merge"]
    assert cfg["headless_permission_mode"] == "acceptEdits"
    assert cfg["loops"]["issue_executor"] is False
    assert "defaults" in cfg["_rules_from"]


def test_detects_plumbing_from_the_checkout(repo):
    cfg = hr.resolve_repo(str(repo))
    assert cfg["name"] == "myrepo"
    assert cfg["default_branch"] == "main"
    assert cfg["executor_pr_base"] == "main"      # falls back to default branch
    assert cfg["path"] == str(repo)


def test_rules_file_overrides_defaults(repo):
    write_rules(repo, {"auto_merge": "all_green"})
    cfg = hr.resolve_repo(str(repo))
    assert cfg["auto_merge"] == "all_green"
    assert cfg["_rules_from"].endswith(hr.RULES_FILENAME)


def test_nested_blocks_merge_rather_than_replace(repo):
    """Setting one reviewer must not wipe the others, or a repo enabling
    sonarqube would silently lose its claude and codex reviewers."""
    write_rules(repo, {"reviewers": {"sonarqube": {"enabled": True}},
                       "loops": {"issue_executor": True}})
    cfg = hr.resolve_repo(str(repo))
    assert cfg["reviewers"]["sonarqube"]["enabled"] is True
    assert cfg["reviewers"]["claude"]["enabled"] is True       # survived
    assert cfg["reviewers"]["claude"]["model"] == "opus"       # two levels deep
    assert cfg["loops"]["issue_executor"] is True
    assert cfg["loops"]["dependabot_lander"] is False          # survived


def test_detected_fields_cannot_be_spoofed_by_the_rules_file(repo):
    """The checkout in front of us is the authority on what/where it is — a rules
    file naming a different repo must not redirect `gh --repo` at it.

    (github isn't compared to a literal here: this fixture's origin is a local
    bare repo, so the detected owner/name is fixture-shaped. What matters is that
    it comes from the remote and not from the file.)"""
    write_rules(repo, {"path": "/etc", "github": "evil/repo", "default_branch": "attacker"})
    cfg = hr.resolve_repo(str(repo))
    assert cfg["path"] == str(repo)
    assert cfg["github"] != "evil/repo"
    assert cfg["github"] == hr.detect_github(repo)
    assert cfg["default_branch"] == "main"


def test_bad_json_fails_loudly(repo):
    (repo / hr.RULES_FILENAME).write_text("{not json")
    with pytest.raises(SystemExit):
        hr.resolve_repo(str(repo))


# ------------------------------------------- doc comments and unknown reviewers

def test_underscore_keys_never_reach_the_resolved_config(repo):
    """Every rules file in the fleet documents itself with `"_": "why"` keys,
    JSON having no comments. Merged through as-is, the one inside a reviewer
    block arrives in cfg["reviewers"] as a bare STRING next to the dicts, and the
    next caller to write the obvious `for name, r in rev.items(): r.get(...)`
    gets an AttributeError from a file whose only sin was explaining itself."""
    write_rules(repo, {
        "_": "top-level prose",
        "reviewers": {"_": "why these seats",
                      "claude": {"model": "opus", "_": "a floating alias"}},
        "epic": {"_": "prose here too", "auto_finish": True},
    })
    cfg = hr.resolve_repo(str(repo))
    assert "_" not in cfg
    assert "_" not in cfg["reviewers"]
    assert "_" not in cfg["epic"]
    assert all(isinstance(r, dict) for r in cfg["reviewers"].values())
    # …and the real settings around them are untouched.
    assert cfg["reviewers"]["claude"]["model"] == "opus"
    assert cfg["epic"]["auto_finish"] is True


def test_underscore_keys_are_not_mistaken_for_reviewer_names(repo, capsys):
    write_rules(repo, {"reviewers": {"_": "prose", "_pi": "more prose"}})
    hr.resolve_repo(str(repo))
    assert "unknown reviewer" not in capsys.readouterr().err


def test_a_typod_reviewer_name_is_shouted_about_and_then_actually_ignored(repo, capsys):
    """The merge is a blind dict update, so `antigravty` is not an error — it
    adds a block nothing reads, and the panel quietly runs one vendor short.
    That silence is the exact failure this harness refuses to have anywhere
    else, and committed to a file it survives every run until someone counts.

    Warned about AND removed. A name only warned about survives into
    cfg["reviewers"], which makes the word "ignored" false and hands every
    caller iterating the resolved mapping a phantom seat."""
    write_rules(repo, {"reviewers": {"antigravty": {"enabled": True}}})
    cfg = hr.resolve_repo(str(repo))
    err = capsys.readouterr().err
    assert "unknown reviewer" in err and "antigravty" in err
    assert "antigravity" in err                  # the known names are listed
    assert "antigravty" not in cfg["reviewers"]
    assert set(cfg["reviewers"]) == set(hr.DEFAULTS["reviewers"])
    # Non-fatal: a rules file shared across a fleet may name a seat only a newer
    # harness knows about, and hard-failing would make it a version pin.
    assert cfg["reviewers"]["claude"]["enabled"] is True


def test_a_typo_in_any_other_block_is_caught_too(repo, capsys):
    """Not a reviewer-specific failure mode. `loops.issue_executer` merges in as
    an inert key while the real setting falls back to its default — and for
    `loops.*` that default is OFF, so a typo silently disables an unattended
    loop with nothing on stderr to say why it stopped running."""
    write_rules(repo, {"loops": {"issue_executer": True},
                       "epic": {"auto_finsh": True},
                       "review_panel": {"judge_modl": "sonnet"}})
    cfg = hr.resolve_repo(str(repo))
    err = capsys.readouterr().err
    for typo in ("issue_executer", "auto_finsh", "judge_modl"):
        assert typo in err and typo not in str(cfg)
    assert "`loops` setting" in err
    assert cfg["loops"]["issue_executor"] is False        # the real one, untouched
    assert cfg["review_panel"]["judge_model"] == hr.DEFAULTS["review_panel"]["judge_model"]


def test_a_typo_at_the_top_level_is_caught_too(repo, capsys):
    """The top level is where `auto_merge`, `enabled` and
    `headless_permission_mode` live, so skipping it left the sweep's blind spot
    on the block that decides whether PRs get merged unattended: `auto_merg`
    merges in inert while the real switch falls back to its default, and the
    only way to find out is to count."""
    write_rules(repo, {"auto_merg": "all_green", "enabld": False,
                       "review_panell": {"judge_model": "sonnet"}})
    cfg = hr.resolve_repo(str(repo))
    err = capsys.readouterr().err
    for typo in ("auto_merg", "enabld", "review_panell"):
        assert typo in err and typo not in cfg
    assert "top-level setting" in err
    assert cfg["auto_merge"] == hr.DEFAULTS["auto_merge"]   # the real one, untouched
    assert cfg["enabled"] is True


def test_the_top_level_names_that_are_not_in_defaults_are_still_known(repo, capsys):
    """`name` and `executor_pr_base` are settable and documented but have no
    DEFAULTS entry, which is why the top level was skipped rather than swept.
    They go in the allowlist instead."""
    write_rules(repo, {"name": "custom", "executor_pr_base": "test"})
    cfg = hr.resolve_repo(str(repo))
    assert capsys.readouterr().err == ""
    assert cfg["name"] == "custom" and cfg["executor_pr_base"] == "test"


def test_a_typod_reviewer_field_is_caught_too(repo, capsys):
    """One level deeper than the seat name, and the quietest failure of the lot:
    `reviewers.pi.enabld` leaves the seat OFF, and every consumer ignores the
    key, so the panel runs a vendor short with nothing on stderr."""
    write_rules(repo, {"reviewers": {"pi": {"enabld": True},
                                     "codex": {"effrot": "high"},
                                     "antigravity": {"modle": "gemini-3.7-flash-high"}}})
    cfg = hr.resolve_repo(str(repo))
    err = capsys.readouterr().err
    for typo in ("enabld", "effrot", "modle"):
        assert typo in err
    assert "`reviewers.pi` setting" in err
    assert "enabld" not in cfg["reviewers"]["pi"]
    assert cfg["reviewers"]["pi"]["enabled"] is False       # the real one, untouched
    assert cfg["reviewers"]["codex"]["effort"] == ""


def test_sonarqube_connection_details_are_known_fields(repo, capsys):
    """Documented in the loops README and deliberately absent from DEFAULTS —
    there is no sensible default host, org or project key — so the field sweep
    has to know them by name, or every configured Sonar seat is warned about and
    dropped and the hard gate silently stops running."""
    write_rules(repo, {"reviewers": {"sonarqube": {
        "enabled": True, "project_key": "acme_myrepo", "organization": "acme",
        "host": "https://sonarcloud.io", "token_env": "SONARQUBE_TOKEN"}}})
    cfg = hr.resolve_repo(str(repo))
    assert capsys.readouterr().err == ""
    assert cfg["reviewers"]["sonarqube"]["project_key"] == "acme_myrepo"


def test_a_renamed_seat_is_told_where_it_went(repo, capsys):
    """`gemini` is the one unknown name a shared fleet rules file is likely to
    carry, because it was a valid DEFAULTS key until the seat moved to Google's
    Antigravity CLI. "No reviewer of that name exists" leaves the reader to
    infer the rename; naming it is one line and the difference between a warning
    that explains and one that puzzles."""
    write_rules(repo, {"reviewers": {"gemini": {"enabled": True}}})
    hr.resolve_repo(str(repo))
    err = capsys.readouterr().err
    assert "renamed to 'antigravity'" in err


def test_the_warning_is_not_repeated_for_the_same_file(repo, capsys):
    """resolve_repo runs in panel.py, epic.py and lander.py — epic per run, and
    it also shells out to panel.py, which resolves again. Undeduped, one typo'd
    seat prints the same warning several times per epic run and trains the
    reader to skip exactly the message meant to be loud."""
    write_rules(repo, {"reviewers": {"antigravty": {"enabled": True}}})
    hr.resolve_repo(str(repo))
    assert "antigravty" in capsys.readouterr().err
    hr.resolve_repo(str(repo))
    assert capsys.readouterr().err == ""


def test_the_unknown_names_are_returned_not_just_printed():
    """The return value is what resolve_repo drops them by, so it is not
    decoration — asserted here so it cannot quietly become dead again."""
    rules = {"reviewers": {"antigravty": {}}, "loops": {"nope": 1}}
    assert hr.unknown_keys(rules) == {"reviewers": ["antigravty"], "loops": ["nope"]}
    assert hr.unknown_keys({"reviewers": "not a dict"}) == {}
    assert hr.unknown_keys({}) == {}
    # The top level and each reviewer's fields have labels of their own, which is
    # how resolve_repo knows where to drop them from.
    assert hr.unknown_keys({"auto_merg": "all_green"}) == {hr.TOP_LEVEL: ["auto_merg"]}
    assert hr.unknown_keys({"reviewers": {"pi": {"enabld": True}}}) == {
        "reviewers.pi": ["enabld"]}


def test_known_reviewers_are_silent(repo, capsys):
    write_rules(repo, {"reviewers": {n: {"enabled": True} for n in hr.DEFAULTS["reviewers"]}})
    hr.resolve_repo(str(repo))
    assert capsys.readouterr().err == ""


def test_every_documented_setting_is_a_known_one(repo, capsys):
    """The typo sweep validates against DEFAULTS, so DEFAULTS has to carry every
    key a reader is told they may set — `judge_max_diff_chars` was documented in
    the README and in DEFAULTS' own comment while being absent from the block,
    which would now warn about it and drop it."""
    write_rules(repo, {"review_panel": {"max_diff_chars": 60_000,
                                        "judge_max_diff_chars": 40_000},
                       "reviewers": {"claude": {"max_diff_chars": 10_000}}})
    cfg = hr.resolve_repo(str(repo))
    assert capsys.readouterr().err == ""
    assert cfg["review_panel"]["judge_max_diff_chars"] == 40_000
    # Reviewer FIELDS are swept too, so the same rule applies one level deeper:
    # max_diff_chars is a real per-reviewer override with no DEFAULTS entry, and
    # the sweep has to allow it by name rather than warn about it.
    assert cfg["reviewers"]["claude"]["max_diff_chars"] == 10_000


def test_defaults_carry_every_seat_the_panel_can_run(repo):
    """The rules files promise that an omitted key falls back to DEFAULTS. That
    is only true of seats DEFAULTS actually has — a repo enabling `antigravity`
    with no base entry was relying on spelling out every field itself.

    Compared against panel's OWN registry rather than a literal set, because the
    drift this catches is exactly what the literal cannot see: DEFAULTS said
    `gemini` long after panel had moved to `antigravity`, so the seat the panel
    could run was warned about as unknown and the seat only DEFAULTS knew was
    silently accepted. harness_rules must not import panel — a test is the only
    place the two registries can be held against each other."""
    import panel

    assert set(hr.DEFAULTS["reviewers"]) == set(panel.ALL_REVIEWERS)
    write_rules(repo, {"reviewers": {"antigravity": {"model": "gemini-3.7-flash-high"}}})
    cfg = hr.resolve_repo(str(repo))
    assert cfg["reviewers"]["antigravity"]["enabled"] is False    # inherited
    assert cfg["reviewers"]["antigravity"]["model"] == "gemini-3.7-flash-high"


# ------------------------------------------------- shared CLI-failure plumbing
# The behavioural tests live HERE, where the functions live. panel re-exports
# stderr_gist, and test_panel_reviewer_model.py asserts that re-export is the
# same object — which is all a re-export owes anyone.

TOO_OLD = (
    'ERROR: {"type":"error","status":400,"error":{"type":"invalid_request_error",'
    '"message":"The \'gpt-5.6-luna\' model requires a newer version of Codex. '
    'Please upgrade to the latest app or CLI and try again."}}'
)


def test_stderr_gist_lifts_the_api_sentence_over_housekeeping():
    """A codex older than its own models cache logs a decode error on EVERY run;
    the naive stderr tail reported that and buried the real complaint."""
    noisy = "\n".join([
        "ERROR codex_models_manager::cache: failed to load models cache: unknown variant `max`",
        TOO_OLD,
        "ERROR codex_api::endpoint::responses_websocket: failed to connect to websocket",
    ])
    assert hr.stderr_gist(noisy) == (
        "The 'gpt-5.6-luna' model requires a newer version of Codex. "
        "Please upgrade to the latest app or CLI and try again.")


# The real thing, from `agy` 1.1.12 — the message the blank-output path exists to
# surface. Note what it does NOT contain: the word "error".
DENIED = ('jetski: no output produced — a tool required the "command" permission '
          "that headless mode cannot prompt for, so it was auto-denied. Add an "
          "allow-rule under permissions.allow in settings.json.")


@pytest.mark.parametrize("order", [(DENIED, "ERROR warmup: could not prime the index"),
                                   ("ERROR warmup: could not prime the index", DENIED)])
def test_stderr_gist_prefers_the_settled_cause_over_unrelated_error_noise(order):
    """The noise filter only knows four housekeeping strings by name, so ANY
    other line carrying the word "error" used to outrank the sentence that names
    the remedy — and the denial sentence does not contain "error" at all. On a
    blank run this is the one line the operator gets, and it has to point at the
    permission, not at a warm-up that had nothing to do with it."""
    assert hr.stderr_gist("\n".join(order)) == DENIED


def test_stderr_gist_of_nothing_is_nothing():
    assert hr.stderr_gist("") == "" and hr.stderr_gist("  \n \n") == ""


def _proc(stdout="", stderr="", rc=0):
    return subprocess.CompletedProcess(["cli"], rc, stdout=stdout, stderr=stderr)


def test_cli_outcome_names_both_shapes_of_nothing():
    """A zero exit with empty stdout is a failure, not an empty review: "found
    nothing" and "produced nothing" are opposite claims a bare "" cannot tell
    apart."""
    assert hr.cli_outcome(_proc(stdout="[]")) == ""
    assert hr.cli_outcome(_proc(stdout="[]", rc=3)) == "exited 3"
    assert hr.cli_outcome(_proc(stdout="  \n\t")) == "exited 0 but produced no output"
    assert hr.cli_outcome(_proc()) == "exited 0 but produced no output"


def test_cli_failure_gist_reads_stderr_only_when_the_run_explains_nothing():
    """The gate, in one place, because both drivers need it and two copies of it
    had already drifted. A CLI that REPLIED at exit 0 and also logged warm-up
    chatter has not failed at running — blaming "loaded 3 plugins" for a reply
    that simply was not JSON is a confident wrong cause."""
    answered = _proc(stdout="prose, not JSON", stderr="loaded 3 plugins")
    assert hr.cli_failure_gist(answered, "no JSON in reply") == "no JSON in reply"
    blank = _proc(stderr="error: everything is on fire")
    assert hr.cli_failure_gist(blank, "no JSON in reply") == "error: everything is on fire"
    # Nothing on stderr either: the shape of the failure still beats silence.
    assert hr.cli_failure_gist(_proc(rc=2), "no JSON in reply") == "exited 2"
    assert hr.cli_failure_gist(_proc()) == "exited 0 but produced no output"


# ------------------------------------------------- the two-ref trust boundary

def test_unattended_ignores_the_working_tree(repo):
    """The whole point: an uncommitted (or PR-branch-only) escalation is invisible
    to the unattended runner, which sees only what is on the default branch."""
    write_rules(repo, {"headless_permission_mode": "bypassPermissions",
                       "loops": {"dependabot_lander": True}}, commit=False)

    interactive = hr.resolve_repo(str(repo), from_default_branch=False)
    assert interactive["headless_permission_mode"] == "bypassPermissions"

    unattended = hr.resolve_repo(str(repo), from_default_branch=True)
    assert unattended["headless_permission_mode"] == "acceptEdits"     # the default
    assert unattended["loops"]["dependabot_lander"] is False
    assert "origin/main" in unattended["_rules_from"]


def test_unattended_honours_committed_rules(repo):
    write_rules(repo, {"loops": {"dependabot_lander": True}}, commit=True)
    cfg = hr.resolve_repo(str(repo), from_default_branch=True)
    assert cfg["loops"]["dependabot_lander"] is True
    # The branch AND the file. This used to assert `== "origin/main"`, which was
    # enough while one filename could ever be the answer; two can now
    # (`.harness-rules.sample` is preferred, `.harness-rules` is the legacy
    # fallback) and `describe()` puts this string in front of a human so that
    # which rules applied is "never a guess" — a provenance that names the branch
    # and not the file is exactly the guess it promises not to be.
    assert cfg["_rules_from"] == f"origin/main:{hr.RULES_FILENAME}"


def test_a_pr_branch_cannot_escalate_itself(repo):
    """Concretely the dependabot case: rules committed on a side branch, and
    that branch checked out, must not change what an unattended run honours."""
    write_rules(repo, {"loops": {"dependabot_lander": True}}, commit=True)
    git(repo, "checkout", "-q", "-b", "dependabot/pip/evil")
    write_rules(repo, {"loops": {"dependabot_lander": True},
                       "auto_merge": "all_green",
                       "headless_permission_mode": "bypassPermissions"}, commit=False)
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "escalate")

    cfg = hr.resolve_repo(str(repo), from_default_branch=True)
    assert cfg["auto_merge"] != "all_green"
    assert cfg["headless_permission_mode"] == "acceptEdits"


def test_env_var_selects_unattended_mode(repo, monkeypatch):
    write_rules(repo, {"auto_merge": "all_green"}, commit=False)
    monkeypatch.setenv("HARNESS_UNATTENDED", "1")
    assert hr.unattended() is True
    assert hr.resolve_repo(str(repo))["auto_merge"] != "all_green"


# ----------------------------------------------------------------- lookup

def test_find_repo_by_path_name_and_cwd(repo, tmp_path, monkeypatch):
    assert hr.find_repo(str(repo)) == repo

    monkeypatch.setattr(hr, "REPO_ROOT", tmp_path)
    assert hr.find_repo("myrepo") == repo

    monkeypatch.chdir(repo)
    assert hr.find_repo(None) == repo


def test_find_repo_from_a_subdirectory(repo, monkeypatch):
    sub = repo / "a" / "b"
    sub.mkdir(parents=True)
    monkeypatch.chdir(sub)
    assert hr.find_repo(None) == repo


def test_unknown_name_and_non_repo_raise(tmp_path, monkeypatch):
    monkeypatch.setattr(hr, "REPO_ROOT", tmp_path)
    with pytest.raises(hr.RepoNotFound):
        hr.find_repo("nope")
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(hr.RepoNotFound):
        hr.find_repo(str(plain))


def test_repo_without_origin_is_refused(tmp_path):
    """The harness addresses repos via `gh --repo owner/name`; no remote, no run."""
    p = tmp_path / "local"
    p.mkdir()
    git(p, "init", "-q", "-b", "main")
    git(p, "config", "user.email", "t@example.com")
    git(p, "config", "user.name", "T")
    (p / "f").write_text("x")
    git(p, "add", "-A")
    git(p, "commit", "-qm", "c")
    with pytest.raises(SystemExit):
        hr.resolve_repo(str(p))


# ---------------------------------------------------------------- discovery

def test_discover_finds_only_repos_with_a_rules_file(repo, tmp_path):
    (tmp_path / "other").mkdir()
    write_rules(repo, {"loops": {"dependabot_lander": True}})
    found = hr.discover(tmp_path)
    assert repo in found
    assert tmp_path / "other" not in found


def test_discover_on_a_missing_root_is_empty(tmp_path):
    assert hr.discover(tmp_path / "nope") == []


@pytest.mark.parametrize("url,expected", [
    ("git@github.com:acme/thing.git", "acme/thing"),
    ("https://github.com/acme/thing.git", "acme/thing"),
    ("https://github.com/acme/thing", "acme/thing"),
])
def test_detect_github_url_forms(repo, url, expected):
    git(repo, "remote", "set-url", "origin", url)
    assert hr.detect_github(repo) == expected


# ------------------------------------------------------------------ .env

def test_read_dotenv_forms(tmp_path):
    (tmp_path / ".env").write_text(
        "# a comment\n"
        "\n"
        "SONARQUBE_TOKEN=plain-value\n"
        "export EXPORTED=via-export\n"
        'DQ="double quoted"\n'
        "SQ='single quoted'\n"
        "EMPTY=\n"
        "  SPACED  =  padded  \n"
        "no_equals_here\n"
    )
    d = hr.read_dotenv(tmp_path)
    assert d["SONARQUBE_TOKEN"] == "plain-value"
    assert d["EXPORTED"] == "via-export"
    assert d["DQ"] == "double quoted"
    assert d["SQ"] == "single quoted"
    assert d["EMPTY"] == ""
    assert d["SPACED"] == "padded"
    assert "no_equals_here" not in d


def test_read_dotenv_keeps_hash_inside_a_value(tmp_path):
    """A '#' is a comment only at the start of a line. Truncating a credential
    mid-value at a '#' is a miserable failure to debug."""
    (tmp_path / ".env").write_text("TOKEN=abc#def=ghi\n")
    assert hr.read_dotenv(tmp_path)["TOKEN"] == "abc#def=ghi"


def test_read_dotenv_missing_file_is_empty(tmp_path):
    assert hr.read_dotenv(tmp_path) == {}


def test_dotenv_is_tracked(repo):
    (repo / ".env").write_text("TOKEN=x\n")
    assert hr.dotenv_is_tracked(repo) is False       # untracked
    git(repo, "add", "-f", ".env")
    git(repo, "commit", "-qm", "oops")
    assert hr.dotenv_is_tracked(repo) is True        # committed — a real leak


def test_the_documented_judge_default_is_the_one_that_ships():
    """A literal pin, kept deliberately. The relationship test below cannot catch
    a default-vs-loader drift — if both move together it stays green — and after
    the de-hardcoding above nothing else in the suite asserts that the model the
    READMEs name is the model that loads."""
    assert hr.DEFAULTS["review_panel"]["judge_model"] == "sonnet"


def test_the_judge_is_not_the_same_model_as_a_seat():
    """#78's independence rule, as far as a default can carry it: the model that
    adjudicates must not be the model that raised the finding. Asserted against
    the relationship rather than against a literal, because swapping a seat's
    model must fail this too, not quietly restore the thing it is here to prevent.

    **Every seat, not just the enabled ones.** A reviewer that ships disabled with
    the judge's model passes an enabled-only check and then collides the moment a
    repo turns it on — which is precisely the condition the rule exists to reject,
    arriving by the one route the test did not look down. This is a defaults-level
    check with no runtime config in play, so the broader assertion costs nothing."""
    judge = hr.DEFAULTS["review_panel"]["judge_model"]
    seats = {name: r.get("model") for name, r in hr.DEFAULTS["reviewers"].items()
             if r.get("model")}
    assert judge, "an empty judge_model resolves to the claude CLI's default, which may be a seat"
    assert seats, ("no seat declares a model, so the assertion below is vacuous — "
                   "a DEFAULTS edit dropping the model keys would pass this silently")
    assert judge not in seats.values(), f"judge {judge!r} is also a seat's model: {seats}"


def test_the_epic_ceiling_is_pinned_to_what_the_old_fallback_resolved_to():
    """The epic's spending ceiling and the panel's adjudicator were one key, and
    they agreed only by accident. Since the judge became deliberately unlike a
    seat, that fallback would have routed every sub-issue's implementation at
    whatever tier the judge happens to sit at.

    `opus` is pinned as a literal **on purpose**: it is what the old fallback
    resolved to, so changing it is a behaviour change for every repo on the
    defaults, not a refactor. What is deliberately NOT asserted is that the
    ceiling differs from the judge — that is today's coincidence, not the rule,
    and encoding it would make a future editor move the ceiling to keep a test
    green when the judge legitimately moves. The rule itself is that these are
    independent keys, and it is asserted where it lives: `resolve_ceiling` in
    `harness/loops/tests/test_epic_model_ceiling.py`, which calls the wiring."""
    assert hr.DEFAULTS["epic"]["model_ceiling"] == "opus"


# ------------------------------------------- the tracked/untracked split (#207 fleet)

def write_sample(repo, obj, commit=True):
    """The TRACKED half — policy, on the protected branch."""
    (repo / hr.SAMPLE_FILENAME).write_text(json.dumps(obj))
    if commit:
        git(repo, "add", "-A")
        git(repo, "commit", "-qm", "sample")
        git(repo, "push", "-q", "origin", "main")


def write_local(repo, obj):
    """The UNTRACKED half — this machine's answer to which reviewer CLIs exist.

    Deliberately never committed: the whole safety argument is that a file git is
    not carrying cannot arrive from the branch of the PR under review.
    """
    (repo / hr.RULES_FILENAME).write_text(json.dumps(obj))


def test_the_sample_supplies_the_baseline(repo):
    """`.harness-rules.sample` is read where `.harness-rules` used to be."""
    write_sample(repo, {"auto_merge": "none", "loops": {"dependabot_lander": True}})
    for unattended in (False, True):
        cfg = hr.resolve_repo(str(repo), from_default_branch=unattended)
        assert cfg["auto_merge"] == "none", f"unattended={unattended}"
        assert cfg["loops"]["dependabot_lander"] is True
        assert hr.SAMPLE_FILENAME in cfg["_rules_from"]


def test_a_repo_with_only_the_legacy_file_is_unchanged(repo):
    """Every repo in the fleet on the day this shipped, and the reason the reader
    falls back rather than switching over.

    A tracked `.harness-rules` and no sample is the old layout. It has to resolve
    exactly as before — attended and unattended — or this change is a silent
    policy wipe across three repos rather than a migration."""
    write_rules(repo, {"auto_merge": "none", "epic": {"sub_pr_merge": "auto"}},
                commit=True)
    for unattended in (False, True):
        cfg = hr.resolve_repo(str(repo), from_default_branch=unattended)
        assert cfg["auto_merge"] == "none", f"unattended={unattended}"
        assert cfg["epic"]["sub_pr_merge"] == "auto"


def test_an_untracked_rules_file_with_no_sample_is_still_the_whole_config(repo):
    """The regression this file exists for, and it was a live bug for one commit.

    "Untracked" alone was the first gate on the overlay, which is wrong for the
    repo that has not committed its rules yet — mid-migration, a fresh clone, or
    a test fixture. Its entire policy was being demoted to a seat toggle and
    dropped on the floor, with a warning that read like the file was at fault.
    The overlay needs BOTH: a sample supplied the baseline, AND this file is
    untracked."""
    write_local(repo, {"auto_merge": "none", "headless_permission_mode": "bypassPermissions"})
    cfg = hr.resolve_repo(str(repo), from_default_branch=False)
    assert cfg["auto_merge"] == "none", (
        "an untracked .harness-rules with no sample beside it is the legacy "
        "layout, not an overlay — its policy must still apply")
    assert cfg["headless_permission_mode"] == "bypassPermissions"


def test_a_tracked_rules_file_beside_a_sample_is_not_an_overlay(repo):
    """Mid-migration: the sample added, the old file not yet untracked.

    Treated as an overlay, the committed rules would be narrowed to seats and the
    rest of the policy silently lost — on a repo whose only mistake was doing the
    two halves of the migration in two commits."""
    write_sample(repo, {"auto_merge": "none"}, commit=False)
    write_rules(repo, {"epic": {"sub_pr_merge": "auto"}}, commit=True)
    cfg = hr.resolve_repo(str(repo), from_default_branch=False)
    # The sample wins as baseline, and the tracked file is not applied as an
    # overlay — but nothing is silently reinterpreted either.
    assert cfg["auto_merge"] == "none"


def test_the_local_overlay_turns_a_seat_off(repo):
    """The case this whole split is for: a box without `agy` on PATH.

    A seat enabled in the sample but absent from the machine would otherwise veto
    every round's `confident` for ever, because panel.py counts a reviewer that
    never ran as coverage it did not get."""
    write_sample(repo, {"reviewers": {"antigravity": {"enabled": True},
                                      "pi": {"enabled": True}}})
    write_local(repo, {"reviewers": {"antigravity": {"enabled": False},
                                     "pi": {"enabled": False}}})
    cfg = hr.resolve_repo(str(repo), from_default_branch=False)
    assert cfg["reviewers"]["antigravity"]["enabled"] is False
    assert cfg["reviewers"]["pi"]["enabled"] is False
    assert cfg["reviewers"]["claude"]["enabled"] is True, "untouched seats stay as they were"
    assert hr.RULES_FILENAME in cfg["_rules_from"], (
        "a resolved config the overlay changed must say so — describe() is what a "
        "human reads to know which rules applied")


def test_the_local_overlay_cannot_change_policy(repo, capsys):
    """The security property, and the reason the overlay is narrowed at all.

    An untracked file is reviewed by nobody: it never appears in a PR, so branch
    protection cannot see it. Left unrestricted it is a way to widen `auto_merge`
    on a box with no review — and the unattended timers would honour it."""
    write_sample(repo, {"auto_merge": "none", "epic": {"sub_pr_merge": "gate"}})
    write_local(repo, {"auto_merge": "all",
                       "epic": {"sub_pr_merge": "auto"},
                       "reviewers": {"pi": {"enabled": False}}})
    cfg = hr.resolve_repo(str(repo), from_default_branch=False)
    assert cfg["auto_merge"] == "none", "the local file must not widen auto-merge"
    assert cfg["epic"]["sub_pr_merge"] == "gate", "nor open a merge gate"
    assert cfg["reviewers"]["pi"]["enabled"] is False, "the seat toggle still applies"
    err = capsys.readouterr().err
    assert "auto_merge" in err and "epic" in err, (
        f"a dropped policy key must be REPORTED, not silently ignored — someone "
        f"who set it is otherwise certain it took effect. stderr was: {err!r}")


def test_the_local_overlay_sets_the_model_and_effort_a_provider_will_serve(repo):
    """The case the per-box file exists for, and a reversal of this test's own
    earlier assertion.

    It used to assert the opposite — that `model` and `effort` are policy, because
    "a box that could pin `model` locally could quietly move the panel onto a tier
    nobody agreed to pay for". That cost concern is real and is not what happens
    here: what a provider WILL SERVE is a fact about the machine, not a preference
    about spend. On this host codex routes through an employer Azure gateway
    serving gpt-5.5 while the fleet pins gpt-5.6-luna at `max`, and the gateway
    refuses both, independently (#215). With the pin unsettable per box the only
    outcomes are a lost seat or a runtime fallback that reviews on an unnamed
    model — and the fallback is what shipped, so the board's cost history now
    records `codex (CLI default)`, which is the exact vagueness the pins exist to
    prevent.

    The residual risk is kept honest rather than argued away: a local file CAN
    move a seat to a costlier model, and what stops that being silent is that the
    panel names the model that actually ran in its header and records it per seat
    on the board. A spend that nobody agreed to is visible in the same place the
    agreement would have been.
    """
    write_sample(repo, {"reviewers": {"codex": {"enabled": True, "model": "gpt-5.6-luna",
                                                "effort": "max"}}})
    write_local(repo, {"reviewers": {"codex": {"model": "gpt-5.5", "effort": "high"}}})
    cfg = hr.resolve_repo(str(repo), from_default_branch=False)
    assert cfg["reviewers"]["codex"]["model"] == "gpt-5.5"
    assert cfg["reviewers"]["codex"]["effort"] == "high"
    assert cfg["reviewers"]["codex"]["enabled"] is True, (
        "a key the overlay does not mention must keep the sample's value")


def test_the_local_overlay_still_cannot_set_anything_that_decides_a_merge(repo, capsys):
    """The line that did NOT move when model/effort crossed it.

    An untracked file is reviewed by nobody, so it must not be able to widen
    `auto_merge` or open a merge gate. A pin is a fact about a provider; a merge
    gate is a policy, and policy stays in the tracked sample where a human
    reviewing a branch can see it."""
    write_sample(repo, {"auto_merge": "none", "epic": {"sub_pr_merge": "gate"},
                        "reviewers": {"codex": {"model": "gpt-5.6-luna"}}})
    write_local(repo, {"auto_merge": "all",
                       "epic": {"sub_pr_merge": "auto"},
                       "reviewers": {"codex": {"model": "gpt-5.5",
                                               "max_diff_chars": 999}}})
    cfg = hr.resolve_repo(str(repo), from_default_branch=False)
    assert cfg["auto_merge"] == "none", "the local file must not widen auto-merge"
    assert cfg["epic"]["sub_pr_merge"] == "gate", "nor open a merge gate"
    assert cfg["reviewers"]["codex"]["model"] == "gpt-5.5", "but the pin still applies"
    err = capsys.readouterr().err
    assert "auto_merge" in err and "epic" in err and "max_diff_chars" in err, (
        f"every dropped key must be REPORTED — a budget is not a provider fact "
        f"either, and someone who set one is otherwise certain it took effect. "
        f"stderr was: {err!r}")


def test_an_unknown_seat_in_the_local_overlay_is_reported_not_applied(repo, capsys):
    """lexray's `.harness-rules` sets `reviewers.gemini`, which is not a seat name
    (`antigravity` is). Silently accepting it leaves someone certain they turned a
    reviewer off that was never called that — and it stays on."""
    write_sample(repo, {"reviewers": {"antigravity": {"enabled": True}}})
    write_local(repo, {"reviewers": {"gemini": {"enabled": False}}})
    cfg = hr.resolve_repo(str(repo), from_default_branch=False)
    assert "gemini" not in cfg["reviewers"]
    assert cfg["reviewers"]["antigravity"]["enabled"] is True
    assert "gemini" in capsys.readouterr().err


def test_unattended_honours_the_local_seat_overlay(repo):
    """The timers need it too, and it is safe for them for the same reason.

    A missing CLI is missing whoever started the run, so an unattended panel must
    not enable a seat this box cannot run. The file is untracked, so no PR branch
    can have introduced it — which is the entire distinction the two-ref rule is
    protecting, and it is not weakened by reading a file git never carried."""
    write_sample(repo, {"auto_merge": "none", "reviewers": {"pi": {"enabled": True}}})
    write_local(repo, {"reviewers": {"pi": {"enabled": False}}})
    cfg = hr.resolve_repo(str(repo), from_default_branch=True)
    assert cfg["reviewers"]["pi"]["enabled"] is False
    assert cfg["auto_merge"] == "none", "policy still comes from the protected branch"
