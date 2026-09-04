"""Tests for harness_rules — the per-repo .harness-rules resolver.

The interesting one is the two-ref rule: an unattended run must read rules from
the DEFAULT BRANCH, never the working tree, or an upstream-authored dependabot
branch could rewrite the policy governing its own review.
"""

import json
import os
import shutil
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
    # The per-box overlay lives OUTSIDE the checkout (#240), so without this every
    # test in this file reads the developer's own `~/.config/quarterback/
    # harness-rules.json` and passes or fails on whether that machine happens to pin
    # a seat. That is the defect in #239 — a suite whose answer depends on the host —
    # and it must not be reintroduced by the feature that made it possible.
    monkeypatch.delenv(hr.BOX_RULES_ENV, raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
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
    git(work, "remote", "add", "origin", "https://github.com/acme/myrepo.git")
    git(work, "remote", "set-url", "origin", str(bare))
    git(work, "push", "-q", "origin", "main")
    git(work, "remote", "set-head", "origin", "main")
    return work


def _commit(repo, *paths):
    """Commit and push EXACTLY these paths.

    `git add -A` for both writers made one fixture impossible to build: writing the
    sample uncommitted and then committing the legacy file swept the sample in too,
    so the test meant to cover "the sample added, the old file not yet untracked"
    silently built "both files tracked" instead, and could not fail for the reason
    its own docstring gave. Naming the path is what makes each state expressible.
    """
    git(repo, "add", "--", *paths)
    git(repo, "commit", "-qm", "rules")
    git(repo, "push", "-q", "origin", "main")


def write_rules(repo, obj, commit=False):
    (repo / hr.RULES_FILENAME).write_text(json.dumps(obj))
    if commit:
        _commit(repo, hr.RULES_FILENAME)


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
        _commit(repo, hr.SAMPLE_FILENAME)


def write_local(repo, obj):
    """The UNTRACKED half — this machine's answer to which reviewer CLIs exist.

    Deliberately never committed: the whole safety argument is that a file git is
    not carrying cannot arrive from the branch of the PR under review.
    """
    (repo / hr.RULES_FILENAME).write_text(json.dumps(obj))


def write_tracked_legacy(repo, obj):
    """The mid-migration state: `.harness-rules` still COMMITTED beside the sample.

    Distinct from `write_local`, which leaves it untracked — trackedness is what
    decides whether the second file is superseded policy or this box's overlay, so a
    test about the former cannot use the helper for the latter.
    """
    (repo / hr.RULES_FILENAME).write_text(json.dumps(obj))
    _commit(repo, hr.RULES_FILENAME)


def _second_repo(tmp_path):
    """A second checkout with its own origin, for the multi-repo dedupe tests.

    Its own bare remote rather than a clone of the first: the point of the tests
    using it is two repos reaching the SAME diagnostic text, which needs two
    independent branch reads to fail rather than one shared one.
    """
    work = tmp_path / "otherrepo"
    work.mkdir()
    git(work, "init", "-q", "-b", "main")
    git(work, "config", "user.email", "t@example.com")
    git(work, "config", "user.name", "T")
    (work / "README").write_text("y\n")
    git(work, "add", "-A")
    git(work, "commit", "-qm", "init")
    bare = tmp_path / "otherorigin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
    git(work, "remote", "add", "origin", str(bare))
    git(work, "push", "-q", "origin", "main")
    git(work, "remote", "set-head", "origin", "main")
    return work


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


def test_a_tracked_rules_file_beside_a_sample_is_not_an_overlay(repo, capsys):
    """Mid-migration: the sample added, the old file not yet untracked.

    Treated as an overlay, the committed rules would be narrowed to seats and the
    rest of the policy silently lost — on a repo whose only mistake was doing the
    two halves of the migration in two commits.

    Three assertions, where this used to make one. It asserted only
    `auto_merge == "none"`, which passes identically whether the tracked file is
    ignored, overlaid, or merged — so the test could not fail for the reason its own
    docstring gives. What actually distinguishes the outcomes is whether the
    SHADOWED file's policy was applied (`epic.sub_pr_merge`, which the shadowed file
    sets to `auto` and the sample leaves at the `gate` default) and whether the
    shadowing was reported at all. It also staged with `git add -A`, which swept the
    "uncommitted" sample into the same commit and built a different state than the
    one described; `_commit` names its paths for that reason.
    """
    write_rules(repo, {"epic": {"sub_pr_merge": "auto"}}, commit=True)
    write_sample(repo, {"auto_merge": "none"}, commit=True)
    assert hr._is_tracked(repo, hr.RULES_FILENAME), "the fixture must be mid-migration"
    cfg = hr.resolve_repo(str(repo), from_default_branch=False)
    assert cfg["auto_merge"] == "none", "the sample supplies the baseline"
    assert cfg["epic"]["sub_pr_merge"] == "gate", (
        "the shadowed file's policy must NOT be merged — a tracked file can arrive "
        "from any branch, which is why trackedness is what decides this")
    err = capsys.readouterr().err
    assert hr.SAMPLE_FILENAME in err and "was read" in err, (
        f"a baseline file that exists and was not read must be reported by name, the "
        f"way the overlay path reports every key it drops. stderr was: {err!r}")


def test_the_ORDINARY_overlay_is_not_reported_as_a_half_done_migration(repo, capsys):
    """The other side of the shadowing report, and the one that decides whether it is
    readable at all.

    An untracked `.harness-rules` beside a sample is not a shadowed baseline — it is
    the per-box overlay, which is the normal fully-migrated state and the entire
    point of the split. Keying the report on "the file exists" rather than on "git is
    carrying it" printed a migration warning on every resolution of every correctly
    configured box, which is how a real diagnostic becomes the noise people filter.
    """
    write_sample(repo, {"reviewers": {"pi": {"enabled": True}}})
    write_local(repo, {"reviewers": {"pi": {"enabled": False}}})
    cfg = hr.resolve_repo(str(repo), from_default_branch=False)
    assert cfg["reviewers"]["pi"]["enabled"] is False, "the overlay applied"
    assert "NOTHING in it was read" not in capsys.readouterr().err


def test_an_unattended_run_reports_a_shadowed_file_too(repo, capsys):
    """Both files on the protected branch, read through `git show` rather than off
    disk — the same event, and the timers are the readers least able to ask anyone
    what happened to the missing policy."""
    write_rules(repo, {"epic": {"sub_pr_merge": "auto"}}, commit=True)
    write_sample(repo, {"auto_merge": "none"}, commit=True)
    cfg = hr.resolve_repo(str(repo), from_default_branch=True)
    assert cfg["epic"]["sub_pr_merge"] == "gate"
    assert hr.RULES_FILENAME in capsys.readouterr().err


def test_a_repo_mid_migration_is_told_how_to_finish_it(repo, capsys):
    """The remedy, not just the diagnosis — the shadowing is one `git rm --cached`
    from being resolved, and the previously-tracked file is the one plausible
    pre-migration state a box can be in (it was the only per-repo knob there was).
    Naming the command is what stops someone deleting the file and losing the local
    edits it holds."""
    write_rules(repo, {"epic": {"sub_pr_merge": "auto"}}, commit=True)
    write_sample(repo, {"auto_merge": "none"}, commit=True)
    hr.resolve_repo(str(repo), from_default_branch=False)
    assert f"git rm --cached {hr.RULES_FILENAME}" in capsys.readouterr().err


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


def test_unattended_does_NOT_read_the_untracked_overlay(repo):
    """A REVERSAL of what this test asserted, and the reason is that its premise was
    false.

    It used to assert that an unattended run honours the overlay, on the argument
    that "the file is untracked, so no PR branch can have introduced it — which is
    the entire distinction the two-ref rule is protecting". That argument covers what
    git CHECKS OUT and nothing else. It says nothing about what code run FROM that
    checkout writes: a test suite, a build or lint step, a git hook, a Makefile
    target — anything invoked while the branch under review is checked out — can
    create `.harness-rules` in the repo root, and the gitignore added by this same
    change means `git status` will not even show it. `_is_tracked` then reports a file
    git is not carrying, the overlay is honoured as this machine's own, and
    `{"reviewers": {"<seat>": {"enabled": false}}}` planted that way shrinks the panel
    reviewing that very PR — which panel.py counts as coverage it did not get. That is
    exactly the poisoning the two-ref read exists to prevent.

    So unattended, the working tree is untrusted, and an untracked file in it is part
    of the working tree. The price is named rather than argued away: an unattended
    panel on a box whose fleet pin is unservable falls back at runtime (#215) instead
    of being configured correctly, which is the right trade for a file nobody can
    review being unable to change a review.
    """
    write_sample(repo, {"auto_merge": "none",
                        "reviewers": {"pi": {"enabled": True, "model": "fleet/pin"}}})
    write_local(repo, {"reviewers": {"pi": {"enabled": False, "model": "local/pin"}}})

    unattended = hr.resolve_repo(str(repo), from_default_branch=True)
    assert unattended["reviewers"]["pi"]["enabled"] is True, (
        "the working-tree overlay must not reach an unattended run — planting this "
        "file is a door a PR branch's own test suite can open")
    assert unattended["reviewers"]["pi"]["model"] == "fleet/pin"

    # Interactive is unchanged, because a human at the keyboard IS the authorization.
    interactive = hr.resolve_repo(str(repo), from_default_branch=False)
    assert interactive["reviewers"]["pi"]["enabled"] is False
    assert interactive["reviewers"]["pi"]["model"] == "local/pin"


def test_an_unattended_run_says_the_overlay_went_unread(repo, capsys):
    """In `describe()`'s line rather than on stderr, and deliberately.

    A box carrying an overlay resolves on every timer tick, so a stderr warning about
    it would print for ever and become the noise people filter — while the one place
    a reader asks "why is codex on the fleet pin here?" is the provenance line that
    exists so which rules applied is never a guess."""
    write_sample(repo, {"auto_merge": "none",
                        "reviewers": {"codex": {"model": "fleet/pin"}}})
    write_local(repo, {"reviewers": {"codex": {"model": "local/pin"}}})
    cfg = hr.resolve_repo(str(repo), from_default_branch=True)
    assert hr.RULES_FILENAME in cfg["_rules_from"] and "not read" in cfg["_rules_from"]
    assert capsys.readouterr().err == "", "not on stderr: this runs on a timer"
    assert cfg["auto_merge"] == "none", "policy still comes from the protected branch"


# ------------------------------------- what the overlay SAYS, not just which keys

def test_the_overlay_refuses_a_non_boolean_enabled(repo, capsys):
    """`"enabled": "false"` was the whole hole, and it is the likeliest hand-edit in
    the file.

    The key filter checked NAMES and never values, so a quoted `false` — a non-empty
    string, and therefore truthy — reached the resolved config and kept the seat ON.
    That is the exact outcome this file exists to prevent, produced by the most
    natural mistake anyone editing JSON by hand makes."""
    write_sample(repo, {"reviewers": {"pi": {"enabled": True}}})
    write_local(repo, {"reviewers": {"pi": {"enabled": "false"}}})
    cfg = hr.resolve_repo(str(repo), from_default_branch=False)
    assert cfg["reviewers"]["pi"]["enabled"] is True, (
        "a value that is not a boolean must be dropped, not coerced — and least of "
        "all coerced to the opposite of what it says")
    err = capsys.readouterr().err
    assert "enabled" in err and "boolean" in err and "TRUTHY" in err, (
        f"reported, or whoever wrote it stays certain the seat is off. stderr: {err!r}")


def test_the_overlay_refuses_an_effort_no_cli_serves(repo, capsys):
    """A typo'd effort is a config error and is answered as one here, rather than
    three CLI invocations later."""
    write_sample(repo, {"reviewers": {"codex": {"effort": "high"}}})
    write_local(repo, {"reviewers": {"codex": {"effort": "maxx"}}})
    cfg = hr.resolve_repo(str(repo), from_default_branch=False)
    assert cfg["reviewers"]["codex"]["effort"] == "high"
    err = capsys.readouterr().err
    assert "maxx" in err and "ultra" in err, (
        f"and it names the levels codex does accept. stderr was: {err!r}")


def test_an_effort_on_a_seat_whose_CLI_takes_NONE_is_refused(repo, capsys):
    """claude has no reasoning-effort knob at all, so `effort` on it is not a typo
    to be corrected but a key with no meaning — said in those words, which is what
    `run_seat` says for the same value arriving the same way."""
    write_sample(repo, {"reviewers": {"claude": {"model": "sonnet"}}})
    write_local(repo, {"reviewers": {"claude": {"effort": "high"}}})
    cfg = hr.resolve_repo(str(repo), from_default_branch=False)
    assert "effort" not in cfg["reviewers"]["claude"]
    assert "no reasoning effort" in capsys.readouterr().err


def test_the_valid_efforts_are_the_ONE_set_the_seats_read():
    """One tuple, two readers. A second copy of this set would not disagree loudly —
    it would quietly stop recognising a level a CLI accepts, or accept one it does
    not, the first time a vendor adds one. `run_seat` rules on the same value this
    resolver now rejects early, so they must be reading the same object."""
    import panel_seats

    assert panel_seats.EFFORTS is hr.EFFORTS
    assert panel_seats.CODEX_EFFORTS is hr.CODEX_EFFORTS
    assert panel_seats.PI_EFFORTS is hr.PI_EFFORTS
    assert panel_seats.AGY_EFFORTS is hr.AGY_EFFORTS


def test_the_overlay_refuses_a_model_that_would_read_as_another_option(repo, capsys):
    """No allowlist — a slug list here would refuse tomorrow's model, which is the
    reason DEFAULTS declines to pin codex globally. A SHAPE check, for the one shape
    that matters in an argv list: `--model` takes the next element, so a "pin"
    beginning with `-` adds an option instead of naming a model."""
    write_sample(repo, {"reviewers": {"codex": {"model": "gpt-5.6-luna"}}})
    write_local(repo, {"reviewers": {"codex": {"model": "-c sandbox=danger"}}})
    cfg = hr.resolve_repo(str(repo), from_default_branch=False)
    assert cfg["reviewers"]["codex"]["model"] == "gpt-5.6-luna"
    assert "not the shape of a model slug" in capsys.readouterr().err


def test_the_overlay_refuses_a_model_that_is_not_a_string(repo, capsys):
    """`null` and `{}` are the two an editor produces while half-way through a
    thought, and both used to sail through into a CLI invocation."""
    write_sample(repo, {"reviewers": {"codex": {"model": "gpt-5.6-luna"}}})
    write_local(repo, {"reviewers": {"codex": {"model": None}, "pi": {"effort": {}}}})
    cfg = hr.resolve_repo(str(repo), from_default_branch=False)
    assert cfg["reviewers"]["codex"]["model"] == "gpt-5.6-luna"
    err = capsys.readouterr().err
    assert "codex.model" in err and "pi.effort" in err and "must be a string" in err


def test_the_overlay_cannot_ENABLE_a_seat_the_sample_turned_off(repo, capsys):
    """The overlay narrows and never widens.

    Turning a seat off is a fact about this machine — the CLI is not installed, the
    gateway refuses the model. Turning one back ON is a decision about this repo: the
    sample may have disabled it for cost, for policy, or to keep a merge quorum
    reachable, and none of those is answered by an untracked file that no PR shows and
    no branch protection can see."""
    write_sample(repo, {"reviewers": {"pi": {"enabled": False}}})
    write_local(repo, {"reviewers": {"pi": {"enabled": True}}})
    cfg = hr.resolve_repo(str(repo), from_default_branch=False)
    assert cfg["reviewers"]["pi"]["enabled"] is False
    err = capsys.readouterr().err
    assert "would ENABLE" in err and "narrow" in err


def test_the_overlay_may_still_turn_a_seat_off_that_the_sample_turned_on(repo):
    """The other direction, asserted beside the refusal so the narrowing rule cannot
    be "fixed" into refusing both."""
    write_sample(repo, {"reviewers": {"pi": {"enabled": True}}})
    write_local(repo, {"reviewers": {"pi": {"enabled": False}}})
    cfg = hr.resolve_repo(str(repo), from_default_branch=False)
    assert cfg["reviewers"]["pi"]["enabled"] is False


def test_an_unknown_seat_is_told_which_seats_exist(repo, capsys):
    """The blanket message was misleading for the one author it was most likely to
    reach: someone who wrote `reviewers.gemini.enabled` — a well-formed key naming a
    seat that does not exist — was told that `reviewers.<seat>.enabled` is the only
    allowed shape, which is what they thought they had written."""
    write_sample(repo, {"reviewers": {"antigravity": {"enabled": True}}})
    write_local(repo, {"reviewers": {"gemini": {"enabled": False}}})
    hr.resolve_repo(str(repo), from_default_branch=False)
    err = capsys.readouterr().err
    assert "not a seat on this panel" in err
    assert "renamed to 'antigravity'" in err, "the one unknown name a fleet file carries"
    for seat in hr.DEFAULTS["reviewers"]:
        assert seat in err, "and the valid names, so the remedy is in the message"


def test_a_non_dict_reviewers_block_in_the_overlay_says_so_as_a_SHAPE(repo, capsys):
    """`"reviewers": "none"` used to be reported identically to a forbidden key, under
    a message telling its author that `reviewers.<seat>.*` was the only allowed
    shape."""
    write_sample(repo, {"auto_merge": "none"})
    write_local(repo, {"reviewers": "none"})
    hr.resolve_repo(str(repo), from_default_branch=False)
    err = capsys.readouterr().err
    assert "must be an object of seats" in err and '"codex"' in err


def test_a_non_dict_seat_entry_in_the_overlay_says_so(repo, capsys):
    write_sample(repo, {"auto_merge": "none"})
    write_local(repo, {"reviewers": {"codex": True}})
    hr.resolve_repo(str(repo), from_default_branch=False)
    assert "must be an object of {enabled, model, effort}" in capsys.readouterr().err


def test_an_overlay_of_nothing_but_dropped_keys_claims_nothing(repo):
    """`_rules_from` is what `describe()` prints, so it must not annotate a run the
    overlay changed nothing about."""
    write_sample(repo, {"auto_merge": "none"})
    write_local(repo, {"auto_merge": "all_green"})
    cfg = hr.resolve_repo(str(repo), from_default_branch=False)
    assert cfg["_rules_from"].endswith(hr.SAMPLE_FILENAME), (
        f"nothing was applied, so nothing is claimed: {cfg['_rules_from']!r}")


def test_the_provenance_names_WHICH_pins_the_overlay_set(repo):
    """It said `(seats)` whenever any overlay applied, so an overlay that repinned
    codex to gpt-5.5 at high effort — touching no seat's on/off state at all — was
    reported as a seat change, in the one string that exists so which rules applied
    is never a guess."""
    write_sample(repo, {"reviewers": {"codex": {"model": "gpt-5.6-luna",
                                               "effort": "max"}}})
    write_local(repo, {"reviewers": {"codex": {"model": "gpt-5.5", "effort": "high"}}})
    cfg = hr.resolve_repo(str(repo), from_default_branch=False)
    assert cfg["_rules_from"].endswith("(model, effort)"), cfg["_rules_from"]
    assert "seats" not in cfg["_rules_from"]


def test_the_same_overlay_problem_is_printed_once_per_process(repo, capsys):
    """`resolve_repo` runs per loop tick and per invocation, so a box whose local file
    carries one stray key would otherwise print the same warning for ever — and a real
    diagnostic repeated for ever is one people learn to filter out, which is the same
    outcome as not printing it."""
    write_sample(repo, {"auto_merge": "none"})
    write_local(repo, {"auto_merge": "all_green"})
    hr.resolve_repo(str(repo), from_default_branch=False)
    assert "auto_merge" in capsys.readouterr().err
    hr.resolve_repo(str(repo), from_default_branch=False)
    assert capsys.readouterr().err == ""


# --------------------------------------------- a file that cannot be read at all

def test_an_overlay_that_is_not_valid_JSON_names_the_file(repo):
    write_sample(repo, {"auto_merge": "none"})
    (repo / hr.RULES_FILENAME).write_text("{oops")
    with pytest.raises(SystemExit) as e:
        hr.resolve_repo(str(repo), from_default_branch=False)
    assert hr.RULES_FILENAME in str(e.value) and "not valid JSON" in str(e.value)


def test_an_overlay_that_is_not_an_OBJECT_names_the_file(repo):
    write_sample(repo, {"auto_merge": "none"})
    (repo / hr.RULES_FILENAME).write_text('["reviewers"]')
    with pytest.raises(SystemExit) as e:
        hr.resolve_repo(str(repo), from_default_branch=False)
    assert "must hold a JSON object, not list" in str(e.value)


@pytest.mark.parametrize("body", ['["auto_merge"]', '"auto_merge"', "null", "3"])
def test_a_BASELINE_that_is_not_an_object_gets_the_same_answer(repo, body):
    """The overlay path said this in one line while the baseline path let it through
    to `strip_comments` and the merge loop, where it surfaced as an AttributeError or
    a TypeError from somewhere inside `resolve_repo` with no filename in it. One shape
    of mistake, one shape of answer, whichever half of the split made it."""
    (repo / hr.SAMPLE_FILENAME).write_text(body)
    with pytest.raises(SystemExit) as e:
        hr.resolve_repo(str(repo), from_default_branch=False)
    assert "must hold a JSON object" in str(e.value)
    assert hr.SAMPLE_FILENAME in str(e.value)


def test_a_baseline_read_from_the_BRANCH_is_checked_the_same_way(repo):
    """Both refs, because the same file arrives by two routes and only one of them
    used to be checked."""
    write_sample(repo, ["not", "an", "object"])
    with pytest.raises(SystemExit) as e:
        hr.resolve_repo(str(repo), from_default_branch=True)
    assert "must hold a JSON object, not list" in str(e.value)
    assert f"origin/main:{hr.SAMPLE_FILENAME}" in str(e.value)


def test_a_corrupt_sample_on_the_branch_names_the_ref_and_the_file(repo):
    (repo / hr.SAMPLE_FILENAME).write_text("{nope")
    _commit(repo, hr.SAMPLE_FILENAME)
    with pytest.raises(SystemExit) as e:
        hr.resolve_repo(str(repo), from_default_branch=True)
    assert f"origin/main:{hr.SAMPLE_FILENAME} is not valid JSON" in str(e.value)


def test_a_CORRUPT_sample_does_not_fall_back_to_the_legacy_file(repo):
    """Missing and corrupt are deliberately not the same event.

    Absence means "use the defaults", which is the whole point of dropping the
    registry. A file written to say something and unreadable must not quietly defer
    to the stale policy beside it — that is policy going silent, which is the one
    failure this module is built around."""
    write_rules(repo, {"auto_merge": "all_green"}, commit=True)
    (repo / hr.SAMPLE_FILENAME).write_text("{nope")
    _commit(repo, hr.SAMPLE_FILENAME)
    with pytest.raises(SystemExit):
        hr.resolve_repo(str(repo), from_default_branch=True)


def test_a_branch_that_cannot_be_read_is_not_a_repo_with_no_rules(repo, capsys):
    """Two different facts that resolved to one answer. `git show` failing was read
    as "the branch does not carry this file", so a checkout whose `origin/<default>`
    is simply not fetched came back "none on origin/main (defaults)" — a claim about
    the repo it had no evidence for, and now the difference the panel's refusal reads.
    """
    git(repo, "update-ref", "-d", "refs/remotes/origin/main")
    cfg = hr.resolve_repo(str(repo), from_default_branch=True)
    assert cfg["_rules_baseline"] == ""
    assert "unreadable" in cfg["_rules_from"]
    assert "could not be read" in capsys.readouterr().err


# ----------------------------------------- the per-box overlay, outside the repo

def write_box(tmp_path, obj):
    """The PER-BOX half — what this machine's providers serve, for every repo on it."""
    f = tmp_path / "xdg" / "quarterback" / hr.BOX_RULES_FILENAME
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(obj))
    return f


def test_the_box_file_answers_for_a_repo_that_says_nothing(repo, tmp_path):
    """#240. The fact is "what will THIS MACHINE serve", so one file on the box has to
    answer for every checkout and every worktree on it. Storing it per-checkout stored
    it N times and propagated it none: a fresh worktree resolved the fleet pin, its
    provider refused it, and the agent holding it rediscovered the machine's own
    configuration and told its peers in prose."""
    write_sample(repo, {"reviewers": {"codex": {"model": "gpt-5.6-luna",
                                                "effort": "max"}}})
    write_box(tmp_path, {"reviewers": {"codex": {"model": "gpt-5.5", "effort": "high"}}})
    cfg = hr.resolve_repo(str(repo), from_default_branch=False)
    assert cfg["reviewers"]["codex"]["model"] == "gpt-5.5"
    assert cfg["reviewers"]["codex"]["effort"] == "high"
    assert hr.BOX_RULES_FILENAME in cfg["_rules_from"], (
        "the line that exists to say which rules applied has to name this one too")


def test_the_repo_overlay_wins_per_key_without_erasing_the_box(repo, tmp_path):
    """Per KEY, not per seat. A box that pins the model and a repo that pins only the
    effort should end with both — the more specific answer wins where they collide and
    the machine's survives where they do not."""
    write_sample(repo, {"reviewers": {"codex": {"model": "gpt-5.6-luna"}}})
    write_box(tmp_path, {"reviewers": {"codex": {"model": "gpt-5.5", "effort": "high"}}})
    write_local(repo, {"reviewers": {"codex": {"effort": "medium"}}})
    cfg = hr.resolve_repo(str(repo), from_default_branch=False)
    assert cfg["reviewers"]["codex"]["effort"] == "medium", "repo wins the collision"
    assert cfg["reviewers"]["codex"]["model"] == "gpt-5.5", (
        "and the box's answer on a key the repo said nothing about survives")


def test_the_box_file_corrects_a_legacy_repo_too(repo, tmp_path):
    """The asymmetric gate, stated as a test because it looks like an oversight. The
    REPO overlay needs a `.sample` baseline beside it, or a repo whose only config is
    an uncommitted `.harness-rules` has its whole policy demoted to a seat toggle. The
    BOX file needs no such guard: it cannot be the baseline, because it is not in the
    checkout — and a legacy repo whose committed rules name a pin this machine cannot
    serve is exactly the case that should still be corrected."""
    write_tracked_legacy(repo, {"reviewers": {"codex": {"model": "gpt-5.6-luna"}}})
    write_box(tmp_path, {"reviewers": {"codex": {"model": "gpt-5.5"}}})
    cfg = hr.resolve_repo(str(repo), from_default_branch=False)
    assert cfg["_rules_baseline"] == hr.RULES_FILENAME, "legacy layout, as set up"
    assert cfg["reviewers"]["codex"]["model"] == "gpt-5.5"


def test_a_box_file_named_but_missing_is_a_hard_exit(repo, tmp_path, monkeypatch):
    """Somebody pointed at a file. Falling back to "this box has no answer" would be
    the silent-policy failure this module exists to prevent — and it would present as
    a seat quietly running an unservable pin, which is the failure we already cannot
    see. An UNSET variable with no XDG file is the ordinary case and says nothing."""
    write_sample(repo, {"reviewers": {"codex": {"model": "gpt-5.6-luna"}}})
    monkeypatch.setenv(hr.BOX_RULES_ENV, str(tmp_path / "nowhere.json"))
    with pytest.raises(SystemExit) as e:
        hr.resolve_repo(str(repo), from_default_branch=False)
    assert "does not exist" in str(e.value) and hr.BOX_RULES_ENV in str(e.value)


def test_the_box_file_is_not_read_unattended_either(repo, tmp_path):
    """It is no more reviewed than the repo's half, so the rule is the same one: the
    unattended path reads NOTHING out of the box's own configuration. The cost is
    named in the module docstring and it is the right price."""
    write_sample(repo, {"reviewers": {"codex": {"model": "gpt-5.6-luna"}}})
    write_box(tmp_path, {"reviewers": {"codex": {"model": "gpt-5.5"}}})
    cfg = hr.resolve_repo(str(repo), from_default_branch=True)
    assert cfg["reviewers"]["codex"]["model"] == "gpt-5.6-luna", (
        "unattended takes the fleet pin off the protected branch, not this box's")


def test_a_dropped_key_says_which_of_the_two_files_said_it(repo, tmp_path, capsys):
    """With one file the sentence could leave the location implicit. With two, a reader
    told `auto_merge` was ignored has to know WHICH file to go and edit."""
    write_sample(repo, {"reviewers": {"codex": {"model": "gpt-5.6-luna"}}})
    box = write_box(tmp_path, {"auto_merge": "all",
                               "reviewers": {"codex": {"model": "gpt-5.5"}}})
    hr.resolve_repo(str(repo), from_default_branch=False)
    err = capsys.readouterr().err
    assert "`auto_merge` is not a provider fact" in err
    assert str(box) in err, f"the sentence must name the file. got:\n{err}"


# ------------------------------------- a file the branch carries but cannot serve

def test_a_present_but_unreadable_file_never_defers_to_the_one_beside_it(repo, monkeypatch):
    """#238-F01. `ls-tree` confirming a path and `git show` then failing is git
    failing, not the file being absent — and the old code appended a `problems` line
    and `continue`d, which handed the run to the legacy `.harness-rules` sitting
    beside the sample. On a repo mid-migration that is superseded policy governing a
    run whose operator believes the sample is in force, selected by a transient error
    and announced in a line nobody has to read. `_baseline_json` states the rule this
    restores: fall back past a name the branch does not carry, never past one it
    cannot read."""
    write_sample(repo, {"reviewers": {"pi": {"enabled": True}}})
    write_tracked_legacy(repo, {"reviewers": {"pi": {"enabled": False}}})
    real = hr._git

    def flaky(path, *args):
        if args[0] == "show" and args[1].endswith(hr.SAMPLE_FILENAME):
            return subprocess.CompletedProcess(
                args, 128, "", "fatal: unable to read sha1 file\n")
        return real(path, *args)

    monkeypatch.setattr(hr, "_git", flaky)
    with pytest.raises(SystemExit) as e:
        hr.resolve_repo(str(repo), from_default_branch=True)
    said = str(e.value)
    assert hr.SAMPLE_FILENAME in said and "could not be read" in said
    assert "refusing to fall back" in said, (
        "the exit has to say WHY it did not use the file beside it, or the next "
        "reader restores the fallback as an obvious improvement")


def test_the_block_shape_is_checked_before_anything_walks_the_blocks(repo):
    """#238-F06. `_check_block_shape` used to run after `warn_unknown_keys` and the
    unknown-key drop, both of which walk `_DEEP_BLOCKS` as mappings — so
    `{"reviewers": "all"}` reached them first and raised whatever a string raises: an
    AttributeError with no filename in it, which is the outcome the shape check was
    written to replace. Near miss worth naming: `'pi' in 'all'` is a substring test
    that answers True, so a malformed block could be read as a membership answer."""
    write_sample(repo, {"reviewers": "all"})
    with pytest.raises(SystemExit) as e:
        hr.resolve_repo(str(repo), from_default_branch=False)
    said = str(e.value)
    assert "`reviewers` must be a JSON object" in said
    assert hr.SAMPLE_FILENAME in said, "the exit names the file, which is the point"


def test_an_unknown_seat_is_still_dropped_rather_than_type_checked(repo, capsys):
    """The other half of #238-F06, and the reason the check was SPLIT rather than
    moved. `reviewers.gemini` names a seat nothing reads; the answer to an unknown
    NAME is the warning plus a drop, not a hard exit about the type it happened to
    hold. Moving the whole shape check earlier would have turned every unknown seat
    into a fatal error on the strength of its value — the version-pin failure
    `warn_unknown_keys` exists to avoid."""
    write_sample(repo, {"reviewers": {"gemini": True}})
    cfg = hr.resolve_repo(str(repo), from_default_branch=False)
    assert "gemini" not in cfg["reviewers"], "warned about AND removed"
    assert "unknown reviewer" in capsys.readouterr().err


def test_two_repos_hitting_one_branch_failure_both_get_told(repo, tmp_path, capsys):
    """#238-F04. `_report` deduped on `(where, problem)`, and on the unattended read
    `where` is `origin/main:.harness-rules.sample` — true of every checkout on the
    box, with no repo identity in the problem sentence either. A timer looping
    `discover()` printed the first repo's diagnostic and then treated every later
    repo's identical text as the noise the dedupe exists to suppress, which inverts
    it: the diagnostics reaching this reporter are the ones saying policy went
    silent."""
    second = _second_repo(tmp_path)
    for r in (repo, second):
        git(r, "update-ref", "-d", "refs/remotes/origin/main")
        hr.resolve_repo(str(r), from_default_branch=True)
    err = capsys.readouterr().err
    assert err.count("could not be read") == 2, (
        "one line per repo — the second is a different repo's policy going silent, "
        f"not a repeat of the first. got:\n{err}")


# --------------------------------------------- the shape of the baseline's blocks

def test_a_reviewers_block_that_is_not_an_object_is_a_clear_exit(repo):
    """`'pi' in 'all'` is a SUBSTRING match that answers True, and `{**"all"}` then
    raises a TypeError with no filename in it. The merge below is deliberately blind,
    so a block of the wrong type travels through it and detonates somewhere else."""
    write_sample(repo, {"reviewers": "all"})
    with pytest.raises(SystemExit) as e:
        hr.resolve_repo(str(repo), from_default_branch=False)
    assert "`reviewers` must be a JSON object, not str" in str(e.value)


def test_a_seat_that_is_not_an_object_is_a_clear_exit(repo):
    write_sample(repo, {"reviewers": {"pi": True}})
    with pytest.raises(SystemExit) as e:
        hr.resolve_repo(str(repo), from_default_branch=False)
    assert "`reviewers.pi` must be a JSON object, not bool" in str(e.value)


def test_a_deep_block_that_is_not_an_object_is_a_clear_exit(repo):
    """Not reviewers-specific: `"epic": "auto"` would have travelled all the way into
    epic.py before anything noticed."""
    write_sample(repo, {"epic": "auto"})
    with pytest.raises(SystemExit) as e:
        hr.resolve_repo(str(repo), from_default_branch=False)
    assert "`epic` must be a JSON object, not str" in str(e.value)


def test_an_unknown_seat_is_still_warned_about_rather_than_type_checked(repo, capsys):
    """The shape check runs AFTER the unknown-name drop, so a name nothing reads gets
    the rename hint rather than a hard exit about its type — `reviewers.gemini` is an
    unknown seat whatever it holds."""
    write_sample(repo, {"reviewers": {"gemini": "off"}})
    cfg = hr.resolve_repo(str(repo), from_default_branch=False)
    assert "gemini" not in cfg["reviewers"]
    assert "renamed to 'antigravity'" in capsys.readouterr().err


# ------------------------------------------------- trackedness, when git will not say

def test_a_git_failure_reads_as_TRACKED_rather_than_as_an_overlay(repo, capsys,
                                                                 monkeypatch):
    """The single boolean used to carry the whole tracked/untracked argument, and it
    failed in the PERMISSIVE direction: `returncode == 0` for yes and anything else
    for no put a missing git binary, a contended index lock and a partial index in the
    same bucket as a genuinely untracked file. On the mid-migration repo this module
    worries about, one transient failure was enough to demote a committed rules file
    to an overlay and drop its policy with a warning blaming the file."""
    write_sample(repo, {"reviewers": {"pi": {"enabled": True}}})
    write_local(repo, {"reviewers": {"pi": {"enabled": False}}})
    real = hr._git

    def flaky(path, *args):
        if args[:2] == ("ls-files", "--error-unmatch"):
            return subprocess.CompletedProcess(
                args, 128, "", "fatal: index file smaller than expected\n")
        return real(path, *args)

    monkeypatch.setattr(hr, "_git", flaky)
    cfg = hr.resolve_repo(str(repo), from_default_branch=False)
    assert cfg["reviewers"]["pi"]["enabled"] is True, (
        "git could not say, so the file is treated as tracked — the answer that "
        "cannot honour a file nobody has established is a local one")
    err = capsys.readouterr().err
    assert "cannot tell whether git is carrying it" in err and "TRACKED" in err


def test_a_genuinely_untracked_file_is_still_an_overlay(repo):
    """The other exit of the same branch: `ls-files --error-unmatch` exits 1 for "not
    in the index" and reserves everything else for its own failures, which is what
    makes the two distinguishable at all."""
    assert hr._is_tracked(repo, "README") is True
    assert hr._is_tracked(repo, hr.RULES_FILENAME) is False


# ------------------------------------------------- who can see a repo, and gate it

def test_discover_sees_a_repo_that_has_only_migrated_to_the_sample(tmp_path):
    """The regression the rename would otherwise have shipped: a repo that moved its
    policy into `.harness-rules.sample` and needs no per-box overlay carries no
    `.harness-rules` at all, so a sweep looking only for that name stops seeing the
    repo — silently, and only on the unattended path, which is the one nobody is
    watching."""
    (tmp_path / "migrated").mkdir()
    (tmp_path / "migrated" / hr.SAMPLE_FILENAME).write_text("{}")
    (tmp_path / "legacy").mkdir()
    (tmp_path / "legacy" / hr.RULES_FILENAME).write_text("{}")
    (tmp_path / "both").mkdir()
    (tmp_path / "both" / hr.SAMPLE_FILENAME).write_text("{}")
    (tmp_path / "both" / hr.RULES_FILENAME).write_text("{}")
    (tmp_path / "unenrolled").mkdir()
    assert hr.discover(tmp_path) == [tmp_path / "both", tmp_path / "legacy",
                                     tmp_path / "migrated"]


@pytest.mark.parametrize("layout,expected", [
    ("none", ""), ("sample", hr.SAMPLE_FILENAME), ("legacy", hr.RULES_FILENAME),
])
def test_resolve_repo_always_says_WHICH_file_supplied_the_baseline(repo, layout,
                                                                  expected):
    """The field the review gate reads, pinned on its producer.

    A gate is only as good as the fact it reads, and this one has exactly one
    producer. `_rules_baseline` is a FILENAME because sniffing it out of the human
    provenance blurb cannot be done safely — `.harness-rules.sample` contains
    `.harness-rules` — so the field has to be present on every path resolution can
    take, including the one where nothing was found."""
    if layout == "sample":
        write_sample(repo, {"auto_merge": "none"})
    elif layout == "legacy":
        write_rules(repo, {"auto_merge": "none"}, commit=True)
    for unattended in (False, True):
        cfg = hr.resolve_repo(str(repo), from_default_branch=unattended)
        assert cfg["_rules_baseline"] == expected, f"unattended={unattended}"


def test_a_repo_with_no_rules_file_still_resolves_for_the_other_loops(repo):
    """The gate is on the two REVIEW paths and deliberately not here.

    `epic`, `lander` and `preland` all resolve, and every default is the safe end of
    its own switch — no auto-merge, no unattended loop, edit-only headless agents — so
    an unconfigured repo gets a run that does LESS rather than one nobody asked for.
    Refusing in the resolver would take the whole harness down on any repo that has
    not enrolled."""
    cfg = hr.resolve_repo(str(repo))
    assert cfg["_rules_baseline"] == ""
    assert cfg["auto_merge"] == hr.DEFAULTS["auto_merge"]
    assert cfg["loops"]["dependabot_lander"] is False


# ------------------------------------------------- the appetite blocks (#85/#86)

def test_issue_pickup_merges_rather_than_replaces(repo):
    """Setting one key must not wipe the others, or a repo relaxing the unlabelled
    rule would silently lose the skip list AND the human-triage requirement."""
    write_rules(repo, {"issue_pickup": {"skip_when_unlabelled": False}})
    cfg = hr.resolve_repo(str(repo))
    assert cfg["issue_pickup"]["skip_when_unlabelled"] is False
    assert cfg["issue_pickup"]["skip_labels"] == ["needs-human/*"]
    assert cfg["issue_pickup"]["require_human_triage"] is True
    assert cfg["issue_pickup"]["allowed_authors"] == []


def test_issue_filing_merges_rather_than_replaces(repo):
    write_rules(repo, {"issue_filing": {"max_per_run": 3}})
    cfg = hr.resolve_repo(str(repo))
    assert cfg["issue_filing"]["max_per_run"] == 3
    assert cfg["issue_filing"]["require_dedup_check"] is True
    assert cfg["issue_filing"]["unattended"] is False


def test_a_typo_in_issue_pickup_is_reported(repo, capsys):
    """`skip_when_unlabeled` (one l) merges in inert while the real setting stays
    on, so the operator sees refusals they believe they turned off."""
    write_rules(repo, {"issue_pickup": {"skip_when_unlabeled": False}})
    cfg = hr.resolve_repo(str(repo))
    assert "skip_when_unlabeled" in capsys.readouterr().err
    assert cfg["issue_pickup"]["skip_when_unlabelled"] is True


def test_a_typo_in_issue_filing_is_reported(repo, capsys):
    write_rules(repo, {"issue_filing": {"max_per_runs": 9}})
    cfg = hr.resolve_repo(str(repo))
    assert "max_per_runs" in capsys.readouterr().err
    assert cfg["issue_filing"]["max_per_run"] == 1


def test_the_unknown_key_report_covers_the_new_blocks():
    assert hr.unknown_keys({"issue_pickup": {"nope": 1}}) == {"issue_pickup": ["nope"]}
    assert hr.unknown_keys({"issue_filing": {"nope": 1}}) == {"issue_filing": ["nope"]}


def test_a_repo_with_no_rules_file_gets_the_closed_end_of_both_gates(repo):
    """The module docstring claims DEFAULTS is "the safe end of every switch".
    Two more switches now, and the claim has to keep being true of them."""
    cfg = hr.resolve_repo(str(repo))
    assert cfg["issue_pickup"]["enabled"] is False
    assert cfg["issue_pickup"]["only_labels"] == []
    assert cfg["issue_pickup"]["allowed_authors"] == []
    assert cfg["issue_filing"]["unattended"] is False
    assert cfg["issue_filing"]["max_per_run"] == 1


# ------------------------------------------------------------------ the modes
#
# #178. Two ways of working exist in this fleet and nothing named either, which
# cost this repo a wrong attribution on 2026-08-17 and two agents' uncommitted
# work on 2026-08-25. These cover the declaration, the two axes coming apart, and
# the one alarm the declaration makes possible.

@pytest.fixture(autouse=False)
def fresh_reports():
    """`_report` prints each problem once per PROCESS, and these tests assert on
    what it printed. Without this the second test to provoke the same sentence
    about the same repo sees an empty stderr and fails for a reason that has
    nothing to do with what it is testing."""
    hr._reported.clear()
    yield
    hr._reported.clear()


def test_mode_defaults_to_cleanroom_the_safe_end_of_both_axes(repo):
    """The default has to be the mode that ISOLATES, for the same reason every
    other default here is the closed one: the cost of being wrong is ceremony a
    repo did not want, and the cost of the other way round is a destroyed tree."""
    mode = hr.resolve_mode(hr.resolve_repo(str(repo)))
    assert (mode.name, mode.isolation, mode.landing) == ("cleanroom", "worktree", "pr")
    assert mode.mixed is False
    assert mode.label == "CLEANROOM"
    assert mode.glyph == "⌂"


def test_naming_jungle_takes_both_axes_from_the_preset(repo):
    write_rules(repo, {"mode": {"name": "jungle"}})
    mode = hr.resolve_mode(hr.resolve_repo(str(repo)))
    assert (mode.isolation, mode.landing) == ("shared", "direct")
    assert mode.mixed is False
    assert mode.label == "JUNGLE"
    assert mode.glyph == "~"


def test_an_axis_can_be_overridden_on_its_own(repo):
    """#178's "cleanroom tree, jungle plan" — a coherent way to work that the two
    names still describe, and the reason the axes are not hard-wired to a name."""
    write_rules(repo, {"mode": {"name": "cleanroom", "landing": "direct"}})
    mode = hr.resolve_mode(hr.resolve_repo(str(repo)))
    assert (mode.isolation, mode.landing) == ("worktree", "direct")
    assert mode.mixed is True
    assert mode.label == "CLEANROOM tree · JUNGLE plan"
    assert mode.glyph == "⌂"      # from ISOLATION: the half you need at a glance


def test_setting_one_key_does_not_drop_the_others(repo):
    """`mode` is in _DEEP_BLOCKS, so a repo naming only the mode keeps the axes."""
    write_rules(repo, {"mode": {"name": "jungle"}})
    assert hr.resolve_repo(str(repo))["mode"]["isolation"] is None


def test_an_unknown_mode_name_warns_and_falls_back_to_the_isolating_end(repo, capsys,
                                                                        fresh_reports):
    write_rules(repo, {"mode": {"name": "clanroom"}})
    mode = hr.resolve_mode(hr.resolve_repo(str(repo)))
    err = capsys.readouterr().err
    assert "clanroom" in err and "jungle" in err       # names the typo AND the options
    assert (mode.name, mode.isolation) == ("cleanroom", "worktree")
    assert mode.problems


def test_an_unknown_axis_value_warns_and_takes_the_preset(repo, capsys, fresh_reports):
    """The silent-typo failure one level down from `unknown_keys`: the KEY is
    spelled perfectly and the value is not, so nothing above this notices."""
    write_rules(repo, {"mode": {"name": "jungle", "isolation": "worktee"}})
    mode = hr.resolve_mode(hr.resolve_repo(str(repo)))
    assert "worktee" in capsys.readouterr().err
    assert mode.isolation == "shared"                  # jungle's, not cleanroom's


def test_a_typo_in_a_mode_key_is_reported(repo, capsys):
    write_rules(repo, {"mode": {"isolaton": "shared"}})
    hr.resolve_repo(str(repo))
    assert "isolaton" in capsys.readouterr().err
    assert hr.unknown_keys({"mode": {"isolaton": 1}}) == {"mode": ["isolaton"]}


def test_every_preset_is_a_full_set_of_known_axis_values():
    """A third mode added to MODES gets this for free: a preset that omits an axis,
    or spells a value the axis does not take, would resolve to a KeyError inside
    `resolve_mode` at the moment somebody's session started."""
    for name, spec in hr.MODES.items():
        assert set(spec) == set(hr.MODE_AXES), name
        for axis, value in spec.items():
            assert value in hr.MODE_AXES[axis], (name, axis, value)


# ------------------------------------------------- which checkout is this one

def test_the_primary_checkout_is_told_from_a_worktree(repo, tmp_path):
    wt = tmp_path / "myrepo-side"
    git(repo, "worktree", "add", "-q", "-b", "side", str(wt))
    assert hr.tree_of(repo).primary is True
    assert hr.tree_of(wt).primary is False
    # Both see the same set: the count is a property of the CHECKOUT, not of the
    # directory the question was asked from.
    assert hr.tree_of(repo).worktrees == hr.tree_of(wt).worktrees == 2


def test_a_lone_clone_dispenses_nothing(repo):
    """The false positive this clause exists to prevent. A private checkout that
    nobody cuts worktrees from is primary and is NOT shared, and nagging its owner
    is how a true alarm gets trained into noise."""
    tree = hr.tree_of(repo)
    assert (tree.primary, tree.dispenses) == (True, False)
    assert hr.mode_violation(hr.resolve_mode({}), tree) is None


def test_a_worktree_json_makes_the_primary_checkout_a_shared_one(repo):
    (repo / ".worktree.json").write_text("{}\n")
    assert hr.tree_of(repo).dispenses is True


def test_an_existing_worktree_says_the_same_thing_without_being_declared(repo, tmp_path):
    git(repo, "worktree", "add", "-q", "-b", "side", str(tmp_path / "myrepo-side"))
    assert hr.tree_of(repo).dispenses is True


def test_somewhere_that_is_not_a_checkout_raises_no_alarm(tmp_path):
    tree = hr.tree_of(tmp_path)
    assert (tree.primary, tree.dispenses) == (False, False)
    assert hr.mode_violation(hr.resolve_mode({}), tree) is None


# ------------------------------------------------------------- and the alarm

def test_the_alarm_fires_on_a_cleanroom_repo_in_its_shared_checkout(repo):
    (repo / ".worktree.json").write_text("{}\n")
    said = hr.mode_violation(hr.resolve_mode({}), hr.tree_of(repo))
    assert said and "create-worktree" in said
    assert "CLEANROOM" not in said     # the caller has just printed the mode line


def test_the_alarm_is_silent_in_a_worktree_of_the_same_repo(repo, tmp_path):
    (repo / ".worktree.json").write_text("{}\n")
    wt = tmp_path / "myrepo-side"
    git(repo, "worktree", "add", "-q", "-b", "side", str(wt))
    assert hr.mode_violation(hr.resolve_mode({}), hr.tree_of(wt)) is None


def test_a_jungle_repo_is_never_told_off_for_using_its_shared_checkout(repo):
    """One direction only. A jungle repo worked in the shared tree is the mode
    working, and #178 is explicit that the harness must not push it toward the
    ceremony a cleanroom repo wants."""
    (repo / ".worktree.json").write_text("{}\n")
    write_rules(repo, {"mode": {"name": "jungle"}})
    mode = hr.resolve_mode(hr.resolve_repo(str(repo)))
    assert hr.mode_violation(mode, hr.tree_of(repo)) is None


def test_a_mixed_repo_is_judged_on_the_isolation_axis_alone(repo):
    """`landing: direct` says nothing about trees, so the tree alarm ignores it."""
    (repo / ".worktree.json").write_text("{}\n")
    write_rules(repo, {"mode": {"name": "cleanroom", "landing": "direct"}})
    mode = hr.resolve_mode(hr.resolve_repo(str(repo)))
    assert hr.mode_violation(mode, hr.tree_of(repo)) is not None


# ------------------------------------------ what a codex review found (#448)
#
# Four defects an adversarial read turned up after the first cut, each
# reproduced before it was believed. Kept together so the next person changing
# `mode_violation` or `tree_of` can see which cases were bought the hard way.

def test_declaring_cleanroom_is_enough_on_its_own(repo):
    """The dangerous state the first cut reported as fine: a repo that ASKED for
    cleanroom, its primary checkout, and no worktree ever cut from it. Requiring
    `dispenses` of every repo aimed at the lone private clone and caught the first
    collision instead — nothing has to have gone wrong yet for this to be wrong."""
    write_rules(repo, {"mode": {"name": "cleanroom"}})
    mode = hr.resolve_mode(hr.resolve_repo(str(repo)))
    tree = hr.tree_of(repo)
    assert (mode.declared, tree.dispenses) == (True, False)
    assert hr.mode_violation(mode, tree) is not None


def test_pinning_the_axis_counts_as_declaring_it(repo):
    """`{"isolation": "worktree"}` with no mode name is a choice about trees."""
    write_rules(repo, {"mode": {"isolation": "worktree"}})
    assert hr.resolve_mode(hr.resolve_repo(str(repo))).declared is True


def test_an_undeclared_repo_still_gets_the_benefit_of_the_doubt(repo):
    """The other half of the same split, and why it is not just "always warn"."""
    mode = hr.resolve_mode(hr.resolve_repo(str(repo)))
    assert mode.declared is False
    assert hr.mode_violation(mode, hr.tree_of(repo)) is None


def test_a_misspelled_mode_is_not_a_declaration(repo, capsys, fresh_reports):
    """It falls back to cleanroom, and must not then warn MORE confidently on the
    strength of a typo than it would have with the key absent."""
    write_rules(repo, {"mode": {"name": "clanroom"}})
    mode = hr.resolve_mode(hr.resolve_repo(str(repo)))
    capsys.readouterr()
    assert (mode.name, mode.declared) == ("cleanroom", False)
    assert hr.mode_violation(mode, hr.tree_of(repo)) is None


def test_a_mode_name_that_is_not_a_string_warns_rather_than_raising(capsys,
                                                                   fresh_reports):
    """`name not in MODES` hashes its operand, so a JSON array raised TypeError out
    of a resolver whose contract is that a bad value warns and falls back."""
    mode = hr.resolve_mode({"mode": {"name": ["jungle"]}})
    assert "must be a string" in capsys.readouterr().err
    assert (mode.name, mode.declared) == ("cleanroom", False)


def test_a_deleted_worktree_does_not_count_as_one(repo, tmp_path):
    """Git keeps a registration after its directory is gone, marked `prunable`,
    until somebody prunes. Counting those said "this checkout hands out worktrees"
    about one that currently hands out none."""
    wt = tmp_path / "myrepo-side"
    git(repo, "worktree", "add", "-q", "-b", "side", str(wt))
    assert hr.tree_of(repo).worktrees == 2
    shutil.rmtree(wt)
    assert "prunable" in git(repo, "worktree", "list", "--porcelain").stdout
    assert hr.tree_of(repo).worktrees == 1
    assert hr.tree_of(repo).dispenses is False


def test_the_marker_is_found_when_the_git_dir_lives_elsewhere(tmp_path):
    """`.worktree.json` was looked for beside the COMMON GIT DIR, which is the
    checkout only in the ordinary `<root>/.git` layout. With `--separate-git-dir`
    the parent of the git directory is not the working tree, so a shared checkout
    read as private. The work-tree path comes from git now."""
    work, elsewhere = tmp_path / "w", tmp_path / "elsewhere.git"
    work.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main",
                    f"--separate-git-dir={elsewhere}", str(work)], check=True)
    (work / ".worktree.json").write_text("{}\n")
    assert (work / ".git").is_file()            # a FILE pointing at elsewhere.git
    assert hr.tree_of(work).dispenses is True
