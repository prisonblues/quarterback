"""`qb-doctor`, tested against the two ways a doctor can lie.

The first is the obvious one: report a broken thing as working. The second is the
one #204 is actually about and the one every fixture below is aimed at — report a
check it could not make as a check that passed. Three of the six symptoms on that
issue are instances of the second (`prune-worktrees` calling a skipped database
scan "Clean", `worktree-holder`'s exit 4 that `/tree-shake` proceeded on,
`qb-reconcile`'s unattributable `stopped`), and #324 settled the same argument for
CI results a day before this landed. So `unknown` gets as many tests as `fail`.

Everything here runs with no board, no systemd and no network: the checks that
reach outward take their transport through `http_get` / `run_cmd` / `shutil.which`,
which are monkeypatched, and the checks that read a repo get a real one built in
`tmp_path`. That is deliberate rather than convenient — this suite runs inside
flake.nix's `worktree-tests` sandbox, which has git and has no network at all, so
a test that needed one would SKIP there and be green about nothing.

Run: pytest harness/tests/test_qb_doctor.py
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import re
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parent.parent / "bin"
HARNESS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BIN))


def _load():
    """`qb-doctor` has no `.py`, so it is loaded by path the way `test_qb_reconcile.py`
    loads `qb-reconcile` — registered in `sys.modules` before execution, because
    `@dataclass` resolves its own module through `sys.modules[cls.__module__]`.
    """
    loader = importlib.machinery.SourceFileLoader("qb_doctor", str(BIN / "qb-doctor"))
    spec = importlib.util.spec_from_loader("qb_doctor", loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules["qb_doctor"] = module
    loader.exec_module(module)
    return module


qd = _load()


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #

@pytest.fixture(autouse=True)
def _hermetic_git(monkeypatch, tmp_path):
    """No global or system git config reaches these tests.

    Not tidiness. This host carries a global `core.hooksPath` (a nix store path
    holding a gitleaks `pre-commit`), so a fresh fixture repo inherits it and the
    unhooked case reports "points elsewhere" rather than "unset" — while the same
    test in flake.nix's sandbox, where `HOME=$TMPDIR` and there is no global
    config, takes the other branch. A suite whose assertions depend on the
    developer's `~/.gitconfig` is the fourth layer from #204's own comments,
    happening to the suite that checks for it.
    """
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(tmp_path / "gitconfig-system"))
    # And no developer's environment either. `release_tagger` reads QB_RELEASE_TAG
    # first, exactly as the pre-push hook does, so a shell that has it set would put
    # every repo below in scope for the merges row whatever the fixture built.
    monkeypatch.delenv("QB_RELEASE_TAG", raising=False)


def _git(repo: Path, *args: str) -> str:
    out = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                         check=True)
    return out.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A real git repository with one commit and no hooks installed."""
    r = tmp_path / "repo"
    r.mkdir()
    subprocess.run(["git", "init", "-q", str(r)], check=True)
    _git(r, "config", "user.email", "t@example.com")
    _git(r, "config", "user.name", "T")
    (r / "a.txt").write_text("one\n")
    _git(r, "add", "a.txt")
    _git(r, "-c", "core.hooksPath=/nonexistent", "commit", "-qm", "init")
    return r


@pytest.fixture
def githooks(tmp_path: Path) -> Path:
    """A stand-in for `harness/githooks/`, holding the two files that ship today."""
    d = tmp_path / "githooks"
    d.mkdir()
    for name in ("reference-transaction", "qb-hook-forward"):
        (d / name).write_text("#!/bin/sh\nexit 0\n")
        (d / name).chmod(0o755)
    return d


def host_for(repo: Path, githooks: Path | None = None, **over) -> qd.Host:
    """A `Host` pointed at one repo, with everything else switched off by default."""
    common = Path(subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--path-format=absolute", "--git-common-dir"],
        capture_output=True, text=True, check=True).stdout.strip())
    defaults = dict(repo=repo, common_git_dir=common, base_url=None, token=None,
                    human_url=None, harness_bin=None, source_harness=HARNESS,
                    githooks=githooks, client_repo=repo)
    defaults.update(over)
    return qd.Host(**defaults)


def install_hooks(repo: Path, githooks: Path, *, omit: str = "") -> Path:
    """What `qb-hooks install` leaves behind, built by hand so a test can omit one file."""
    common = Path(_git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir"))
    hooks = common / "qb-hooks"
    hooks.mkdir(exist_ok=True)
    for src in githooks.iterdir():
        if src.name == omit:
            continue
        (hooks / src.name).write_text(src.read_text())
        (hooks / src.name).chmod(0o755)
    _git(repo, "config", "core.hooksPath", str(hooks))
    return hooks


# --------------------------------------------------------------------------- #
# the vocabulary, and the rule it exists to enforce
# --------------------------------------------------------------------------- #

def test_the_verdict_vocabulary_is_closed_and_a_check_cannot_invent_one():
    """`#324` made CI state a closed vocabulary for the reason this one is closed:
    the values feed a count, a glyph and an exit code, so a fifth invented at a
    call site leaves the numerator while still counting as coverage."""
    assert qd.VERDICTS == ("ok", "warn", "fail", "unknown")
    with pytest.raises(AssertionError):
        qd.Check("x", "y", "z", "skipped")


def test_unknown_is_not_healthy_and_does_not_render_as_ok():
    """The whole point. A check that could not be made must not reach a reader
    wearing the same word as a check that passed."""
    assert "unknown" not in qd.HEALTHY
    assert qd._GLYPHS["unknown"] != qd._GLYPHS["ok"]
    assert qd._COLOURS["unknown"] != qd._COLOURS["ok"]
    rendered = qd.render([qd.Check("edge", "-", "could not look", "unknown")], colour=False)
    assert "ok" not in rendered.split("—")[-1]


def test_the_summary_spells_every_count_and_refuses_to_call_an_unknown_run_fine():
    ok = qd.summary([qd.Check("a", "-", "", "ok")])
    assert ok == "1 ok, 0 warn, 0 fail, 0 unknown — wired up"

    unknown = qd.summary([qd.Check("a", "-", "", "ok"), qd.Check("b", "-", "", "unknown")])
    assert "1 unknown" in unknown
    assert "not the same as it being fine" in unknown
    assert not unknown.endswith("wired up")

    warned = qd.summary([qd.Check("a", "-", "", "warn")])
    assert "residue nothing installs away" in warned

    failed = qd.summary([qd.Check("a", "-", "", "fail"), qd.Check("b", "-", "", "unknown")])
    assert "NOT wired up" in failed


@pytest.mark.parametrize("verdicts,expected", [
    (["ok", "ok"], 0),
    (["ok", "warn"], 0),
    (["ok", "unknown"], 1),
    (["ok", "fail"], 2),
    (["fail", "unknown"], 2),
])
def test_the_exit_code_tells_could_not_check_apart_from_failed(verdicts, expected):
    """`qb-reconcile` draws the same line for the same reason: a run that could not
    make a check does not get to look like a run that made it and found nothing."""
    assert qd.exit_code([qd.Check(str(i), "-", "", v) for i, v in enumerate(verdicts)]) == expected


# --------------------------------------------------------------------------- #
# hooks — installed, not merely present in git
# --------------------------------------------------------------------------- #

def test_an_unhooked_repo_fails_even_though_the_sources_are_right_there(repo, githooks):
    """`ls harness/githooks/reference-transaction` succeeds on a host with no guard.
    The sources exist in this fixture and the repo is still unguarded."""
    check = qd.check_hooks(host_for(repo, githooks))
    assert check.verdict == "fail"
    assert "core.hooksPath is unset" in check.detail
    assert check.fix == ["qb-hooks", "install", "--repo", str(repo)]


def test_a_hooks_path_pointing_somewhere_else_is_not_installed(repo, githooks, tmp_path):
    other = tmp_path / "elsewhere"
    other.mkdir()
    _git(repo, "config", "core.hooksPath", str(other))
    check = qd.check_hooks(host_for(repo, githooks))
    assert check.verdict == "fail"
    assert "points elsewhere" in check.detail


def test_a_fully_hooked_repo_is_ok_and_names_what_is_installed(repo, githooks):
    install_hooks(repo, githooks)
    check = qd.check_hooks(host_for(repo, githooks))
    assert check.verdict == "ok"
    assert check.extra["installed"] == ["qb-hook-forward", "reference-transaction"]


def test_the_expected_hook_set_is_derived_from_the_sources_not_listed_in_this_file(
        repo, githooks):
    """#343 is adding a `pre-push` guard in another worktree. A hard-coded pair here
    would report a repo with no `pre-push` as fully hooked from the day it lands —
    which is this tool's own subject happening to this tool."""
    (githooks / "pre-push").write_text("#!/bin/sh\nexit 0\n")
    (githooks / "pre-push").chmod(0o755)
    assert qd.expected_hooks(host_for(repo, githooks)) == {
        "reference-transaction", "qb-hook-forward", "pre-push"}

    install_hooks(repo, githooks, omit="pre-push")
    check = qd.check_hooks(host_for(repo, githooks))
    assert check.verdict == "fail"
    assert check.extra["missing"] == ["pre-push"]
    assert "missing 1 of 3" in check.detail


def test_a_hook_dir_that_cannot_be_compared_is_unknown_rather_than_ok(repo, githooks):
    """No `githooks/` beside this script means the file list is unknowable. Reported
    as such: `core.hooksPath` being right says nothing about what is under it."""
    install_hooks(repo, githooks)
    check = qd.check_hooks(host_for(repo, githooks=None))
    assert check.verdict == "unknown"
    assert "cannot be checked" in check.detail


# --------------------------------------------------------------------------- #
# stash — the residue a guard cannot retroactively clear
# --------------------------------------------------------------------------- #

def _stash(repo: Path, message: str) -> None:
    (repo / "a.txt").write_text((repo / "a.txt").read_text() + message + "\n")
    subprocess.run(["git", "-C", str(repo), "-c", "core.hooksPath=/nonexistent",
                    "stash", "push", "-q", "-m", message], check=True)


def test_a_clean_guarded_ref_is_ok(repo, githooks):
    install_hooks(repo, githooks)
    check = qd.check_stash(host_for(repo, githooks))
    assert check.verdict == "ok"
    assert "guard active" in check.detail


def test_a_clean_but_unguarded_ref_is_a_warning_not_an_ok(repo, githooks):
    """Empty because nobody has stashed yet, not because anything stops them."""
    check = qd.check_stash(host_for(repo, githooks))
    assert check.verdict == "warn"
    assert "nothing guards the shared ref" in check.detail


def test_pre_guard_entries_survive_the_install_and_are_counted_rather_than_hidden(
        repo, githooks):
    """The state this repo was in for weeks: freshly guarded, still mined. The hook
    deliberately allows deletions, so installing it drains nothing."""
    _stash(repo, "one")
    _stash(repo, "two")
    install_hooks(repo, githooks)
    check = qd.check_stash(host_for(repo, githooks))
    assert check.verdict == "warn"
    assert check.extra["entries"] == 2
    assert "guard active, 2 pre-guard entries remain" in check.detail
    assert check.detail != "ok"


def test_entries_with_no_guard_at_all_are_a_failure(repo, githooks):
    _stash(repo, "one")
    check = qd.check_stash(host_for(repo, githooks))
    assert check.verdict == "fail"
    assert "NO guard installed" in check.detail


def test_one_entry_is_singular(repo, githooks):
    _stash(repo, "only")
    install_hooks(repo, githooks)
    assert "1 pre-guard entry remain" in qd.check_stash(host_for(repo, githooks)).detail


# --------------------------------------------------------------------------- #
# reconcile — a timer nobody can ask about
# --------------------------------------------------------------------------- #

def test_no_systemd_is_unknown_and_never_a_pass(monkeypatch, repo):
    monkeypatch.setattr(qd.shutil, "which", lambda _n: None)
    check = qd.check_reconcile(host_for(repo))
    assert check.verdict == "unknown"
    assert check.fix is None


def _systemctl(answers: dict) -> object:
    """A fake `systemctl`, keyed on the verb: `{"is-enabled": (rc, out, err), ...}`."""
    def fake(argv, **_kw):
        return answers.get(argv[2] if argv[0] == "systemctl" else "", (0, "", ""))
    return fake


@pytest.mark.parametrize("stdout,stderr", [
    ("", "Failed to get unit file state for qb-reconcile.timer: No such file or directory"),
    ("not-found", ""),
])
def test_a_unit_that_was_never_installed_fails_and_says_where_the_units_live(
        monkeypatch, repo, stdout, stderr):
    """The exact state of every host on the fleet until 2026-08-22: the units were
    in `harness/loops/systemd/` and on no machine, and the plan went 39% stale.

    Both spellings, because systemd answers `not-found` on stdout on this fleet and
    puts the same fact on stderr elsewhere. The exit code is 4 either way, and 4 for
    `inactive` too, which is why nothing here reads it as the discriminator."""
    monkeypatch.setattr(qd.shutil, "which", lambda _n: "/usr/bin/systemctl")
    monkeypatch.setattr(qd, "run_cmd", _systemctl({
        "is-enabled": (4, stdout, stderr), "is-active": (4, "inactive", "")}))
    check = qd.check_reconcile(host_for(repo))
    assert check.verdict == "fail"
    assert "harness/loops/systemd/" in check.manual
    assert check.fix is None, "a unit that does not exist cannot be enabled"


@pytest.mark.parametrize("broken", ["is-enabled", "is-active"])
def test_a_dead_user_bus_is_unknown_whichever_question_it_swallows(monkeypatch, repo, broken):
    """Found by Codex, twice over. "No such unit" and "no bus to ask" share an exit
    code and an empty stdout and are opposite answers: one sends you to install units
    that are already there, the other says the check never happened. And the bus can
    swallow EITHER question — the first fix guarded only `is-enabled`, which left
    `is-enabled=enabled, is-active=` reported as a broken timer."""
    working = {"is-enabled": (0, "enabled", ""), "is-active": (0, "active", "")}
    working[broken] = (1, "", "Failed to connect to bus: No medium found")
    monkeypatch.setattr(qd.shutil, "which", lambda _n: "/usr/bin/systemctl")
    monkeypatch.setattr(qd, "run_cmd", _systemctl(working))
    check = qd.check_reconcile(host_for(repo))
    assert check.verdict == "unknown"
    assert "could not answer" in check.detail
    assert "Failed to connect to bus" in check.detail


def test_a_bus_error_ending_in_no_such_file_is_still_a_bus_error(monkeypatch, repo):
    """`No such file or directory` is systemd's wording for a missing unit file AND
    for a missing bus socket, so the tail of the message settles nothing. Codex made
    this point about the first fix, which matched on that phrase alone."""
    monkeypatch.setattr(qd.shutil, "which", lambda _n: "/usr/bin/systemctl")
    monkeypatch.setattr(qd, "run_cmd", _systemctl({
        "is-enabled": (1, "", "Failed to connect to bus: No such file or directory"),
        "is-active": (1, "", "Failed to connect to bus: No such file or directory")}))
    assert qd.check_reconcile(host_for(repo)).verdict == "unknown"


def test_an_installed_but_disabled_timer_is_fixable(monkeypatch, repo):
    """`systemctl` exits non-zero for perfectly good answers — 1 for `disabled`,
    4 for `inactive` — so nothing here may treat the exit code as a failure."""
    monkeypatch.setattr(qd.shutil, "which", lambda _n: "/usr/bin/systemctl")
    monkeypatch.setattr(qd, "run_cmd", _systemctl({
        "is-enabled": (1, "disabled", ""), "is-active": (3, "inactive", "")}))
    check = qd.check_reconcile(host_for(repo))
    assert check.verdict == "fail"
    assert check.detail == "is-enabled=disabled, is-active=inactive"
    assert check.fix == ["systemctl", "--user", "enable", "--now", "qb-reconcile.timer"]


def test_an_enabled_active_timer_is_ok(monkeypatch, repo):
    monkeypatch.setattr(qd.shutil, "which", lambda _n: "/usr/bin/systemctl")
    monkeypatch.setattr(qd, "run_cmd", _systemctl({
        "is-enabled": (0, "enabled", ""), "is-active": (0, "active", "")}))
    assert qd.check_reconcile(host_for(repo)).verdict == "ok"


# --------------------------------------------------------------------------- #
# board — the version line, and the range that is not one
# --------------------------------------------------------------------------- #

def _openapi(version: str) -> tuple[int, str, None]:
    return 200, json.dumps({"openapi": "3.1.0", "info": {"version": version}}), None


@pytest.fixture
def app_repo(repo: Path) -> Path:
    (repo / "app").mkdir()
    (repo / "app" / "main.py").write_text('app = FastAPI(title="quarterback", version="2.77.0")\n')
    return repo


def test_the_declared_version_is_read_from_the_source_not_imported(app_repo):
    """This runs on hosts with no venv and no database; importing `app.main` needs both."""
    assert qd.declared_version(app_repo) == "2.77.0"
    assert qd.declared_version(app_repo / "nope") is None


@pytest.mark.parametrize("served,verdict,phrase", [
    ("2.77.0", "ok", "matching this checkout"),
    ("2.48.0", "fail", "the image is behind"),
    ("2.80.0", "warn", "the CHECKOUT is behind"),
])
def test_the_two_version_lines_are_compared_in_both_directions(
        monkeypatch, app_repo, served, verdict, phrase):
    """The whole of #204's original value statement: nothing else can make this
    comparison, because each layer can only see itself."""
    monkeypatch.setattr(qd, "http_get", lambda _u, _h=None: _openapi(served))
    check = qd.check_board(host_for(app_repo, base_url="https://board.example"))
    assert check.verdict == verdict
    assert phrase in check.detail
    assert check.extra == {"served": served, "declared": "2.77.0"}


def test_a_board_that_will_not_state_its_version_is_unknown_even_when_probing_answers(
        monkeypatch, app_repo):
    """Capability probing establishes `>= 2.75`, and a floor is not a version: it can
    say the board is new enough for one feature while saying nothing about the next.
    So the fallback reports the floor as CONTEXT under an `unknown`, never as a pass."""
    def fake(url, _headers=None):
        if url.endswith("/openapi.json"):
            return 404, "", None
        if url.endswith("/review-queue"):
            return 405, "", None
        return 404, "", None

    monkeypatch.setattr(qd, "http_get", fake)
    check = qd.check_board(host_for(app_repo, base_url="https://board.example"))
    assert check.verdict == "unknown"
    assert ">= 2.75" in check.detail
    assert "a floor and not a version" in check.detail
    assert check.extra["floor"] == "2.75"


def test_an_unreachable_board_is_unknown_rather_than_stale(monkeypatch, app_repo):
    monkeypatch.setattr(qd, "http_get", lambda _u, _h=None: (0, "", "URLError: refused"))
    check = qd.check_board(host_for(app_repo, base_url="https://board.example"))
    assert check.verdict == "unknown"
    assert "URLError" in check.detail


def test_no_board_configured_is_unknown_not_a_guess(app_repo):
    """`qb-env`'s rule, kept: an unset base URL is an error, never a default —
    guessing would point this agent at another island's board."""
    assert qd.check_board(host_for(app_repo)).verdict == "unknown"


# --------------------------------------------------------------------------- #
# client — importable is not the same as pointing here
# --------------------------------------------------------------------------- #

def test_no_venv_at_all_fails_with_the_command_that_builds_one(repo):
    check = qd.check_client(host_for(repo))
    assert check.verdict == "fail"
    assert "uv venv" in check.manual


def test_the_client_row_follows_the_configured_checkout_not_the_one_being_checked(
        repo, tmp_path):
    """`qb-mcp` execs `$QUARTERBACK_REPO/mcp/.venv/bin/python` whatever directory you
    are standing in, so pointing `--repo` at a worktree must not make the client row
    describe a venv nothing runs."""
    elsewhere = tmp_path / "configured"
    elsewhere.mkdir()
    check = qd.check_client(host_for(repo, client_repo=elsewhere))
    assert str(elsewhere) in check.subject
    assert str(repo) not in check.subject


# --------------------------------------------------------------------------- #
# edge — the boundary between a person and an agent
# --------------------------------------------------------------------------- #

def _edge(agent_status: int, human: tuple[int, str] | None):
    def fake(url, headers=None):
        if human is not None and url.startswith("https://browser"):
            return human[0], human[1], None
        return agent_status, "", None
    return fake


def test_an_agent_vhost_that_accepts_a_forged_person_is_the_worst_case(monkeypatch, repo):
    """`Remote-User` is an ordinary header. If the agent vhost stops stripping it,
    anything that can reach the board can post as Rich — so this is checked before
    the half that has actually been broken since v2.39."""
    monkeypatch.setattr(qd, "http_get", _edge(200, None))
    check = qd.check_edge(host_for(repo, base_url="https://agent", human_url="https://browser"))
    assert check.verdict == "fail"
    assert "ACCEPTED a self-supplied Remote-User" in check.detail


@pytest.mark.parametrize("status", [404, 500, 502])
def test_an_agent_vhost_answering_neither_yes_nor_no_leaves_the_property_untested(
        monkeypatch, repo, status):
    """Found by Codex. The first cut took everything-but-200 as proof of a refusal,
    so a 404 from an old image and a 502 from a dead app both read as "nobody can
    forge a person" — a security property vouched for by a request that never
    reached the code enforcing it."""
    monkeypatch.setattr(qd, "http_get", _edge(status, (200, json.dumps({"kind": "human"}))))
    check = qd.check_edge(host_for(repo, base_url="https://agent", human_url="https://browser"))
    assert check.verdict == "unknown"
    assert "neither an acceptance nor a refusal" in check.detail


def test_forward_auth_with_no_session_is_unknown_and_names_the_runbook(monkeypatch, repo):
    """From an agent host there is no signed-in session, so the honest answer is
    that nothing here can see whether `HUMAN_EDGE_SECRET` is set."""
    monkeypatch.setattr(qd, "http_get", _edge(401, (302, "")))
    check = qd.check_edge(host_for(repo, base_url="https://agent", human_url="https://browser"))
    assert check.verdict == "unknown"
    assert "no session" in check.detail
    assert qd.EDGE_RUNBOOK in check.manual


#: What `app/auth.py` actually answers a `Remote-User` the edge did not vouch for.
#: Copied from `_NOT_FROM_THE_EDGE` rather than paraphrased, because the point of the
#: assertion is that the body is recognisable, and a paraphrase would test the
#: paraphrase.
BOARD_403 = json.dumps({"detail": (
    "that Remote-User was not asserted by the edge: the human-only endpoints need the "
    "proxy's X-Edge-Auth secret (HUMAN_EDGE_SECRET) alongside it, because a header anyone "
    "can send cannot be the boundary between a person and an agent. See DEPLOY.md.")})


def test_a_refused_human_write_fails_and_says_it_needs_a_person(monkeypatch, repo):
    """The state since v2.39: the edge authenticated a person and the app would not
    take them. Two stores hold the secret and nothing compares them, so the only
    detector is this end-to-end request."""
    monkeypatch.setattr(qd, "http_get", _edge(401, (403, BOARD_403)))
    check = qd.check_edge(host_for(repo, base_url="https://agent", human_url="https://browser"))
    assert check.verdict == "fail"
    assert "HUMAN_EDGE_SECRET" in check.detail
    assert check.fix is None, "a secret in 1Password and a redeploy is not qb-doctor's to run"
    assert qd.EDGE_RUNBOOK in check.manual


@pytest.mark.parametrize("body", [
    "",
    '{"status":"KO","message":"Authentication failed"}',
    "<html><body>Sign in</body></html>",
    '{"detail":"Not Found"}',
])
def test_a_refusal_from_the_auth_proxy_is_not_evidence_about_the_secret(
        monkeypatch, repo, body):
    """Found by Codex on the second pass. The proxy answers 401/403 to a caller with
    no session and so does the app to an unvouched person — identical codes, opposite
    diagnoses. Reading the proxy's as the app's tells somebody their secret is broken
    when all they are is signed out."""
    monkeypatch.setattr(qd, "http_get", _edge(401, (403, body)))
    check = qd.check_edge(host_for(repo, base_url="https://agent", human_url="https://browser"))
    assert check.verdict == "unknown"
    assert "auth proxy refused this host" in check.detail


def test_human_writes_accepted_is_the_only_pass(monkeypatch, repo):
    monkeypatch.setattr(qd, "http_get", _edge(401, (200, json.dumps({"kind": "human"}))))
    check = qd.check_edge(host_for(repo, base_url="https://agent", human_url="https://browser"))
    assert check.verdict == "ok"
    assert check.detail == "human writes accepted"


def test_a_browser_vhost_answering_as_an_agent_is_not_a_pass(monkeypatch, repo):
    """A 200 is not enough: `kind` is what says a person got through."""
    monkeypatch.setattr(qd, "http_get", _edge(401, (200, json.dumps({"kind": "agent"}))))
    assert qd.check_edge(
        host_for(repo, base_url="https://agent", human_url="https://browser")).verdict == "fail"


def test_an_unknown_browser_vhost_is_unknown_rather_than_skipped(monkeypatch, repo):
    monkeypatch.setattr(qd, "http_get", _edge(401, None))
    check = qd.check_edge(host_for(repo, base_url="https://agent"))
    assert check.verdict == "unknown"
    assert "QUARTERBACK_HUMAN_URL" in check.manual


# --------------------------------------------------------------------------- #
# harness — content, because there is no version to read
# --------------------------------------------------------------------------- #

def test_harness_drift_is_reported_in_one_direction_only(tmp_path):
    """A file the INSTALL has and the checkout does not is a harness newer than this
    branch, which is normal on any branch that has not added a script. Reporting it
    would make the row red for every developer, and a row that is red for everyone
    is a row nobody reads."""
    src, installed = tmp_path / "src", tmp_path / "installed"
    src.mkdir(), installed.mkdir()
    (src / "same").write_text("x")
    (installed / "same").write_text("x")
    (src / "changed").write_text("new")
    (installed / "changed").write_text("old")
    (src / "added-here").write_text("y")
    (installed / "only-installed").write_text("z")

    differ, missing = qd._harness_drift(src, installed)
    assert differ == {"changed"}
    assert missing == {"added-here"}


def test_the_comparison_is_against_the_checkout_not_against_this_scripts_own_harness(
        repo, tmp_path):
    """Found by Codex, and the most important finding on this change. Comparing the
    PATH harness against the harness this script came from is comparing an installed
    harness with ITSELF whenever `qb-doctor` is run from PATH — which is every
    installed use there will ever be. The row would have reported `ok` forever on the
    one check whose whole subject is a stale install."""
    (repo / "harness" / "bin").mkdir(parents=True)
    (repo / "harness" / "bin" / "qb-thing").write_text("new\n")
    installed = tmp_path / "store" / "bin"
    installed.mkdir(parents=True)
    (installed / "qb-thing").write_text("old\n")

    host = host_for(repo, harness_bin=installed, source_harness=installed.parent)
    assert qd.checkout_harness_bin(host) == repo / "harness" / "bin"
    check = qd.check_harness(host)
    assert check.verdict == "fail"
    assert check.extra["differ"] == ["qb-thing"]


def test_a_repo_with_no_harness_falls_back_to_this_scripts_own_tree(repo, tmp_path):
    """`--repo` pointed at some other project still has a question worth asking —
    "is the harness on PATH the one I have" — so the fallback is the script's tree."""
    host = host_for(repo, harness_bin=tmp_path, source_harness=HARNESS)
    assert qd.checkout_harness_bin(host) == HARNESS / "bin"


def test_no_harness_on_path_is_unknown(repo):
    check = qd.check_harness(host_for(repo, harness_bin=None))
    assert check.verdict == "unknown"
    assert "nothing to compare" in check.detail


def test_a_harness_that_is_this_checkout_is_ok(repo):
    check = qd.check_harness(host_for(repo, harness_bin=HARNESS / "bin"))
    assert check.verdict == "ok"
    assert "IS this checkout" in check.detail


# --------------------------------------------------------------------------- #
# harness — what the PACKAGING rewrites, which is not drift (#353)
# --------------------------------------------------------------------------- #

#: What `patchShebangs` leaves behind: an interpreter in the store, so an installed
#: harness does not depend on the user's PATH. Any absolute path would do here —
#: what the tests are about is that the first line differs and nothing else does.
STORE_SHEBANG = "#!/nix/store/0000000000000000000000000000000-bash-5.3p9/bin/bash"


def _wrapper(installed: Path, name: str, body: str) -> None:
    """`wrapProgram $out/bin/<name>`, as nix leaves the directory.

    The script moves to `.<name>-wrapped` and a generated one takes its name,
    setting a variable and exec-ing the file it displaced.
    """
    (installed / f".{name}-wrapped").write_text(f"{STORE_SHEBANG}\n{body}")
    (installed / name).write_text(
        f"{STORE_SHEBANG} -e\n"
        "export QB_DASH_PYTHON=${QB_DASH_PYTHON-'/nix/store/py/bin/python'}\n"
        f'exec -a "$0" "{installed}/.{name}-wrapped"  "$@"\n')


def test_a_shebang_only_difference_is_patchshebangs_and_not_drift(tmp_path):
    """`postFixup` runs `patchShebangs` and says why: an installed harness must not
    depend on what is on the user's PATH. So every installed script differs from its
    source by its first line, permanently, and counting that made the row report 24
    files as behind a checkout that had just been rebuilt from."""
    src, installed = tmp_path / "src", tmp_path / "installed"
    src.mkdir(), installed.mkdir()
    (src / "qb-thing").write_text("#!/bin/sh\nexec qb \"$@\"\n")
    (installed / "qb-thing").write_text(f"{STORE_SHEBANG}\nexec qb \"$@\"\n")

    differ, missing = qd._harness_drift(src, installed)
    assert differ == set()
    assert missing == set()


def test_a_difference_below_the_shebang_is_still_drift(tmp_path):
    """The line is ignored only when the rest matches. Otherwise this would be a
    check that reads every file and asserts nothing about any of them."""
    src, installed = tmp_path / "src", tmp_path / "installed"
    src.mkdir(), installed.mkdir()
    (src / "qb-thing").write_text("#!/bin/sh\nexec qb --new \"$@\"\n")
    (installed / "qb-thing").write_text(f"{STORE_SHEBANG}\nexec qb \"$@\"\n")

    differ, _ = qd._harness_drift(src, installed)
    assert differ == {"qb-thing"}


def test_an_install_that_has_no_shebang_at_all_still_counts(tmp_path):
    """`patchShebangs` rewrites a shebang; it never removes one and never adds one.
    A file that gained or lost its first line has therefore changed by something
    other than the packaging, and dropping line one from each side unread would
    call two genuinely different files equal."""
    src, installed = tmp_path / "src", tmp_path / "installed"
    src.mkdir(), installed.mkdir()
    (src / "qb-thing").write_text("#!/bin/sh\nexec qb \"$@\"\n")
    (installed / "qb-thing").write_text("exec qb \"$@\"\n")

    differ, _ = qd._harness_drift(src, installed)
    assert differ == {"qb-thing"}


def test_a_wrapprogram_wrapper_is_compared_against_the_file_it_wraps(tmp_path):
    """`postInstall` wraps `qb-dash` to carry the dashboard's interpreter. The
    installed file at that name is then generated by nix and shares no bytes with
    the checkout's copy — so it differed forever, and no rebuild could ever resolve
    it. The honest comparison is against `.qb-dash-wrapped`, which is the script."""
    src, installed = tmp_path / "src", tmp_path / "installed"
    src.mkdir(), installed.mkdir()
    body = 'exec "$QB_DASH_PYTHON" qb-dash.py "$@"\n'
    (src / "qb-dash").write_text(f"#!/bin/sh\n{body}")
    _wrapper(installed, "qb-dash", body)

    differ, missing = qd._harness_drift(src, installed)
    assert differ == set(), "the wrapper was compared instead of the file it wraps"
    assert missing == set()


def test_a_stale_wrapped_script_is_still_reported_through_its_wrapper(tmp_path):
    """Following the wrapper must not be a way of not looking. A wrapped program
    goes stale like any other, and the whole row exists to say so."""
    src, installed = tmp_path / "src", tmp_path / "installed"
    src.mkdir(), installed.mkdir()
    (src / "qb-dash").write_text('#!/bin/sh\nexec "$QB_DASH_PYTHON" qb-dash.py --new "$@"\n')
    _wrapper(installed, "qb-dash", 'exec "$QB_DASH_PYTHON" qb-dash.py "$@"\n')

    differ, _ = qd._harness_drift(src, installed)
    assert differ == {"qb-dash"}


def test_a_dotfile_nothing_execs_does_not_make_a_file_a_wrapper(tmp_path):
    """Detection is structural — the sibling has to exist AND the installed file has
    to point at it. A `.qb-thing-wrapped` left beside a script nobody wrapped would
    otherwise redirect the comparison at a file the install does not run, and hide
    real drift in the file it does."""
    src, installed = tmp_path / "src", tmp_path / "installed"
    src.mkdir(), installed.mkdir()
    (src / "qb-thing").write_text("#!/bin/sh\nexec qb --new \"$@\"\n")
    (installed / "qb-thing").write_text("#!/bin/sh\nexec qb \"$@\"\n")
    (installed / ".qb-thing-wrapped").write_text("#!/bin/sh\nexec qb --new \"$@\"\n")

    differ, _ = qd._harness_drift(src, installed)
    assert differ == {"qb-thing"}


def test_merely_mentioning_the_wrapped_name_is_not_pointing_at_it(tmp_path):
    """Codex's finding on this change. A bare `.qb-thing-wrapped` is a substring any
    comment could hold, and matching on it lets a stale script redirect the check at
    a file that happens to agree with the checkout — real drift, reported as clean,
    which is the reading this row exists to deny. A wrapper carries the wrapped
    file's ABSOLUTE path, so that is what counts as pointing at it."""
    src, installed = tmp_path / "src", tmp_path / "installed"
    src.mkdir(), installed.mkdir()
    (src / "qb-thing").write_text("#!/bin/sh\nexec qb --new \"$@\"\n")
    (installed / "qb-thing").write_text(
        "#!/bin/sh\n# nix would call this .qb-thing-wrapped\nexec qb \"$@\"\n")
    (installed / ".qb-thing-wrapped").write_text("#!/bin/sh\nexec qb --new \"$@\"\n")

    differ, _ = qd._harness_drift(src, installed)
    assert differ == {"qb-thing"}


def test_an_install_that_matches_through_both_artefacts_is_ok_and_still_sees_absences(
        repo, tmp_path):
    """The row's two halves, on one fixture. Every differing file here is a packaging
    artefact and none of them is a finding, while `qb-new` is genuinely not installed
    — which is the state a host is in the moment it adds a script and before it
    rebuilds, and the half of this check that was working and had to stay working."""
    src = repo / "harness" / "bin"
    src.mkdir(parents=True)
    installed = tmp_path / "store" / "bin"
    installed.mkdir(parents=True)
    (src / "qb-thing").write_text("#!/bin/sh\nexec qb \"$@\"\n")
    (installed / "qb-thing").write_text(f"{STORE_SHEBANG}\nexec qb \"$@\"\n")
    body = 'exec "$QB_DASH_PYTHON" qb-dash.py "$@"\n'
    (src / "qb-dash").write_text(f"#!/bin/sh\n{body}")
    _wrapper(installed, "qb-dash", body)

    host = host_for(repo, harness_bin=installed, source_harness=installed.parent)
    assert qd.check_harness(host).verdict == "ok"

    (src / "qb-new").write_text("#!/bin/sh\nexec qb new \"$@\"\n")
    check = qd.check_harness(host)
    assert check.verdict == "fail"
    assert check.extra == {"differ": [], "missing": ["qb-new"]}
    assert "1 absent (qb-new)" in check.detail


# --------------------------------------------------------------------------- #
# harness — a list that is capped has to SAY it was capped (#358)
# --------------------------------------------------------------------------- #

#: `N absent (…)` / `N differ (…)`: the count the row states, and the names it offers
#: as that count. The regression is entirely the gap between the two.
COUNTED_LIST = re.compile(r"(\d+) (absent|differ) \(([^)]*)\)")

#: The elision marker, matched whole rather than by suffix — a file called `one-more`
#: is a name the row listed, not a claim about names it did not.
ELIDED = re.compile(r"^\+(\d+) more$")


def _drifted(repo: Path, tmp_path: Path, *, differ: int, absent: int) -> qd.Check:
    """An install behind the checkout by exactly this much GENUINE drift.

    Differing files differ in their body, not their shebang, so nothing here is a
    packaging artefact and #353's exclusions have nothing to take out.
    """
    src = repo / "harness" / "bin"
    src.mkdir(parents=True)
    installed = tmp_path / "store" / "bin"
    installed.mkdir(parents=True)
    for i in range(differ):
        (src / f"qb-differ-{i}").write_text(f"#!/bin/sh\nexec qb {i} new\n")
        (installed / f"qb-differ-{i}").write_text(f"#!/bin/sh\nexec qb {i} old\n")
    for i in range(absent):
        (src / f"qb-absent-{i}").write_text(f"#!/bin/sh\nexec qb {i}\n")
    return qd.check_harness(host_for(repo, harness_bin=installed,
                                     source_harness=installed.parent))


def _accounted(detail: str) -> list[tuple[str, int, int]]:
    """Each counted list in the row as `(label, the count it states, names it accounts
    for)` — where a trailing `+N more` accounts for N of them."""
    out = []
    for count, label, listed in COUNTED_LIST.findall(detail):
        names = [n.strip() for n in listed.split(",") if n.strip()]
        elided = 0
        tail = ELIDED.match(names[-1]) if names else None
        if tail:
            elided = int(tail.group(1))
            names.pop()
        out.append((label, int(count), len(names) + elided))
    return out


def test_a_capped_differ_list_says_the_cap_was_applied(repo, tmp_path):
    """The row that filed #358 read `5 differ (four names)` and dropped the fifth in
    silence. The count was the honest half, which is the confusing way round: a reader
    who counts the names concludes the COUNT is broken and stops trusting the row."""
    check = _drifted(repo, tmp_path, differ=6, absent=0)
    assert check.verdict == "fail"
    assert ("6 differ (qb-differ-0, qb-differ-1, qb-differ-2, qb-differ-3, +2 more)"
            in check.detail)
    assert len(check.extra["differ"]) == 6, "--json still carries every name"


def test_the_absent_list_is_capped_the_same_way_and_says_so(repo, tmp_path):
    """Both halves of the row had the same cap written the same way, so both halves
    get the same fix — a person reading one learns nothing about the other."""
    check = _drifted(repo, tmp_path, differ=0, absent=5)
    assert ("5 absent (qb-absent-0, qb-absent-1, qb-absent-2, qb-absent-3, +1 more)"
            in check.detail)
    assert len(check.extra["missing"]) == 5


def test_a_list_at_the_cap_names_everything_and_claims_no_elision(repo, tmp_path):
    """The boundary: exactly `NAME_CAP` names is a complete list, and appending
    `+0 more` to it would be a second way of lying about the same thing."""
    check = _drifted(repo, tmp_path, differ=qd.NAME_CAP, absent=1)
    assert "more" not in check.detail, check.detail
    assert f"{qd.NAME_CAP} differ (" in check.detail


@pytest.mark.parametrize("differ, absent", [(0, 9), (9, 0), (5, 5), (4, 4), (1, 30)])
def test_every_counted_list_in_the_harness_row_accounts_for_every_name_it_counts(
        repo, tmp_path, differ, absent):
    """The guard, and the reason it parses the row instead of asserting one string:
    the defect is not "the cap is 4", it is "a cap was applied and not mentioned". So
    this reads whatever the row prints and holds it to its own arithmetic — any list
    that starts eliding without saying so fails here, whatever the cap becomes and
    whichever of the two lists grows a third."""
    check = _drifted(repo, tmp_path, differ=differ, absent=absent)
    parsed = _accounted(check.detail)
    assert parsed, f"no counted list found in: {check.detail}"
    assert len(parsed) == bool(differ) + bool(absent)
    for label, count, accounted in parsed:
        assert accounted == count, (
            f"{label}: the row counts {count} and accounts for {accounted} — {check.detail}")


def test_the_content_comparison_says_in_the_code_that_it_is_a_proxy(tmp_path):
    """The question is "was this built from a commit at or after HEAD", and the
    truthful answer is the flake pin's rev — which needs the CONSUMING flake, which
    this tool cannot find and which some hosts do not have. Content stands in for it.
    A reader who does not know that reads a row that cannot tell ahead from behind as
    one that can, so the reasoning is pinned here rather than left to a commit
    message nobody will go looking for."""
    doc = qd.check_harness.__doc__
    assert "proxy" in doc.lower()
    assert "flake" in doc and "rev" in doc

# --------------------------------------------------------------------------- #
# --fix — what it may run, and what it may only print
# --------------------------------------------------------------------------- #

def test_fix_runs_only_what_the_check_marked_runnable(monkeypatch, repo):
    """`manual` is a string for a person and `fix` is an argv this tool executes.
    Two fields rather than one command plus a flag, so nothing can run a command
    the author did not mark runnable — the edge secret needs 1Password and a
    Portainer redeploy, and a doctor that tried would fail halfway through a deploy."""
    ran: list[list[str]] = []
    monkeypatch.setattr(qd, "run_cmd", lambda argv, **_kw: (ran.append(argv), (0, "", ""))[1])
    monkeypatch.setattr(qd, "run_checks", lambda _h, _o=None: [qd.Check("a", "-", "now on", "ok")])

    before = [qd.Check("a", "-", "off", "fail", manual="do it", fix=["installer"]),
              qd.Check("b", "-", "refused", "fail", manual="ask a person")]
    after = qd.apply_fixes(host_for(repo), before)
    assert ran == [["installer"]]
    assert after[0].detail.endswith("(fixed by qb-doctor --fix)")


def test_fix_rechecks_rather_than_trusting_the_installers_exit_code(monkeypatch, repo):
    """"The command succeeded" and "the guard is now installed" are the two sentences
    this whole tool exists to keep apart."""
    monkeypatch.setattr(qd, "run_cmd", lambda _a, **_kw: (0, "", ""))
    monkeypatch.setattr(qd, "run_checks",
                        lambda _h, _o=None: [qd.Check("a", "-", "still off", "fail")])
    after = qd.apply_fixes(host_for(repo),
                           [qd.Check("a", "-", "off", "fail", fix=["installer"])])
    assert after[0].verdict == "fail"
    assert "ran and did not fix it" in after[0].detail


def test_fix_rechecks_every_selected_row_because_one_guard_is_another_rows_input(
        repo, githooks, monkeypatch):
    """Installing hooks changes what `stash` reports. Re-checking only the fixed row
    left the stash line describing a repo that had stopped existing a second earlier."""
    _stash(repo, "one")
    host = host_for(repo, githooks)
    before = qd.run_checks(host, {"hooks", "stash"})
    assert {c.name: c.verdict for c in before} == {"hooks": "fail", "stash": "fail"}

    real = qd.run_cmd

    def install_instead(argv, **kw):
        """Stand in for `qb-hooks install`, which is not on PATH in the sandbox."""
        if argv[0] == "qb-hooks":
            install_hooks(repo, githooks)
            return 0, "stash guard installed", ""
        return real(argv, **kw)

    monkeypatch.setattr(qd, "run_cmd", install_instead)
    after = qd.apply_fixes(host, before, {"hooks", "stash"})
    assert {c.name: c.verdict for c in after} == {"hooks": "ok", "stash": "warn"}
    assert "1 pre-guard entry remain" in next(c for c in after if c.name == "stash").detail


def test_installer_chatter_goes_to_stderr_so_json_stays_parseable(monkeypatch, repo, capsys):
    """Found by Codex, on `--fix --json` together: an installer's output printed to
    stdout ahead of the report makes the whole run unparseable for the consumer the
    flag exists for."""
    monkeypatch.setattr(qd, "run_cmd", lambda _a, **_kw: (0, "stash guard installed", ""))
    monkeypatch.setattr(qd, "run_checks", lambda _h, _o=None: [qd.Check("a", "-", "on", "ok")])
    qd.apply_fixes(host_for(repo), [qd.Check("a", "-", "off", "fail", fix=["installer"])])
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "stash guard installed" in captured.err


def test_nothing_to_fix_leaves_the_report_untouched(monkeypatch, repo):
    monkeypatch.setattr(qd, "run_cmd", lambda *_a, **_kw: pytest.fail("must not run"))
    checks = [qd.Check("a", "-", "", "ok"), qd.Check("b", "-", "", "unknown", manual="look")]
    assert qd.apply_fixes(host_for(repo), checks) is checks


# --------------------------------------------------------------------------- #
# a check that blows up is a row, not a crash
# --------------------------------------------------------------------------- #

def test_a_check_that_raises_becomes_an_unknown_naming_the_exception(monkeypatch, repo):
    """A doctor is what you run when the host is already odd, so "one row could not
    be computed" has to be a row rather than a traceback over the other nine."""
    def boom(_host):
        raise RuntimeError("the host is very odd")

    monkeypatch.setattr(qd, "CHECKS", (("boom", "host", boom),
                                       ("fine", "host", lambda _h: qd.Check(
                                           "fine", "-", "", "ok"))))
    out = qd.run_checks(host_for(repo))
    assert [c.verdict for c in out] == ["unknown", "ok"]
    assert "RuntimeError: the host is very odd" in out[0].detail


# --------------------------------------------------------------------------- #
# the site config is DATA, not a script
# --------------------------------------------------------------------------- #

def test_the_site_config_is_parsed_and_never_sourced(monkeypatch, tmp_path):
    """`~/.config/quarterback/config` is machine-generated on some hosts and
    hand-written on others. A diagnostic must not be the thing that executes an
    unreviewed line in it, so the assignments are read as text."""
    marker = tmp_path / "executed"
    cfg = tmp_path / "config"
    cfg.write_text(
        f"# comment\nQUARTERBACK_BASE_URL=https://board.example\n"
        f"touch {marker}\n"
        "QUARTERBACK_TOKEN_CMD='echo hunter2'\n")
    monkeypatch.setenv("QUARTERBACK_CONFIG", str(cfg))
    loaded = qd.load_site_config()
    assert loaded["QUARTERBACK_BASE_URL"] == "https://board.example"
    assert loaded["QUARTERBACK_TOKEN_CMD"] == "echo hunter2"
    assert not marker.exists()


def test_a_config_path_is_expanded_the_way_a_shell_would_expand_it(monkeypatch, tmp_path):
    """The fleet's generated config writes `QUARTERBACK_REPO="$HOME/source/quarterback"`.
    Read literally, the client row looked for a venv under a directory with a dollar
    sign in its name and reported `no MCP venv` about one that was there — a check
    answering confidently about the wrong place."""
    monkeypatch.setenv("HOME", str(tmp_path))
    assert qd._as_path("$HOME/source/quarterback") == tmp_path / "source/quarterback"
    assert qd._as_path("~/mcp") == tmp_path / "mcp"
    assert qd._as_path("") is None
    assert qd._as_path(None) is None


def test_a_missing_site_config_is_an_empty_mapping_not_an_error(monkeypatch, tmp_path):
    monkeypatch.setenv("QUARTERBACK_CONFIG", str(tmp_path / "nope"))
    assert qd.load_site_config() == {}


# --------------------------------------------------------------------------- #
# argument handling
# --------------------------------------------------------------------------- #

def test_an_unknown_check_name_exits_three_rather_than_silently_checking_nothing(capsys):
    """`--only typo` running every check, or none, are both worse than refusing."""
    assert qd.main(["--only", "hoooks"]) == 3
    assert "no such check" in capsys.readouterr().err


def test_a_directory_that_is_not_a_repository_exits_three(tmp_path, capsys):
    with pytest.raises(SystemExit) as e:
        qd.survey(str(tmp_path), None, __file__)
    assert e.value.code == 3
    assert "not a git repository" in capsys.readouterr().err


def test_every_check_name_is_offered_by_only(capsys):
    """The `--only` help lists the registry, so a check added without a name in
    `CHECKS` cannot be selected and a name in the help with no check cannot run.
    Group names too, for the same reason: `--only landing` is only discoverable if
    the help says the word."""
    names = [n for n, _g, _fn in qd.CHECKS] + list(qd.GROUPS)
    assert len(set(names)) == len(names)
    with pytest.raises(SystemExit):
        qd.main(["--help"])
    # Name by name, not as one joined string: argparse rewraps the help to the
    # terminal width, so the list is split across lines on a narrow one and the
    # assertion would pass or fail on the size of the window running it.
    help_text = capsys.readouterr().out
    for name in names:
        assert name in help_text


# --------------------------------------------------------------------------- #
# the tool ships (#169's failure, in the cheapest place it happens)
# --------------------------------------------------------------------------- #

def test_qb_doctor_is_executable():
    """`install -m 0755 bin/*` preserves nothing that was not already there in git."""
    import os
    assert os.access(BIN / "qb-doctor", os.X_OK)


def test_package_nix_accounts_for_qb_doctor_in_its_own_reasoning():
    """`package.nix`'s installPhase comment is the argued list of what belongs on
    PATH and why. The glob installs this file either way — which is exactly the
    trap: a script can ship while the document that decides what ships has never
    heard of it, and the next person pruning that directory has no reason to keep
    it. This asserts the reasoning was updated, not just the directory."""
    nix = (HARNESS / "package.nix").read_text()
    assert "qb-doctor" in nix, (
        "package.nix does not mention qb-doctor — add it to the comment block above "
        "installPhase that argues which files belong in bin/")


def test_the_readme_documents_every_verdict_in_the_section_about_them():
    """A doctor whose `?` is undocumented gets read as a `warn` and then as an `ok`.

    Anchored on the SECTION heading rather than the first mention of the tool's name:
    it is listed in `bin/`'s bullet at the top of the file, so a search from the first
    occurrence measures the wrong six thousand characters and passes or fails on the
    length of an unrelated paragraph.
    """
    readme = (HARNESS / "README.md").read_text()
    assert "### `qb-doctor`" in readme, "harness/README.md has no qb-doctor section"
    section = readme.split("### `qb-doctor`", 1)[1].split("\n## ", 1)[0]
    for verdict in qd.VERDICTS:
        assert verdict in section, f"the README's qb-doctor section never mentions {verdict!r}"
    assert "could not be made" in section


# --------------------------------------------------------------------------- #
# merges — can a merge here rewrite the commit a release tag was reserved against?
# --------------------------------------------------------------------------- #

#: What GitHub answers for a repo configured the way #406 concluded this one should be.
MERGE_COMMITS_ONLY = {"allow_merge_commit": True, "allow_squash_merge": False,
                      "allow_rebase_merge": False, "push": True}


#: A tag allocator that RESERVES — the pre-#122 shape, and the only thing that puts a
#: repo in scope for this row. Written as argparse registers a subcommand rather than as
#: prose, because the whole correction is that the row asks what a tool DOES and not
#: whether a file of that name exists.
RESERVING_TAGGER = (
    "# release_tag.py — take refs/tags/vX.Y on the remote at push time.\n"
    'sub.add_parser("reserve", help="the compare-and-swap one lander wins")\n'
    'sub.add_parser("check")\n'
)

#: A tag allocator that does not. This is quarterback's own shape since #122: the file is
#: still there, with the same name and three subcommands, and `reserve` survives only in
#: the paragraph explaining that it is gone.
TAGGER_THAT_RESERVES_NOTHING = (
    "# release_tag.py — every release has a tag.\n"
    "#\n"
    "# `reserve` is deleted (#122). It existed to take refs/tags/vX.Y on the remote at\n"
    "# PUSH time; branches do not stamp, so there is nothing to reserve.\n"
    'sub.add_parser("backfill")\n'
    'sub.add_parser("taken")\n'
    'sub.add_parser("check")\n'
)


@pytest.fixture
def landing_repo(repo: Path) -> Path:
    """A repo in scope for the merges row: a GitHub remote and a tagger that reserves."""
    _git(repo, "remote", "add", "origin", "git@github.com:acme/thing.git")
    _git(repo, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
    (repo / "scripts").mkdir()
    (repo / "scripts" / "release_tag.py").write_text(RESERVING_TAGGER)
    return repo


def _gh_says(monkeypatch, *, out: str = "", rc: int = 0, err: str = "") -> None:
    """Answer for `gh` and let every other subprocess through.

    Real git, faked GitHub. The row's git half — which remote, is it GitHub, is there an
    allocator — is most of what can be wrong with it, and a fixture that stubbed that too
    would be asserting the stub.

    `which` is stubbed as well, and that is not tidiness: this suite runs inside
    flake.nix's `worktree-tests` sandbox, which carries git and NOT `gh`. Without it every
    test below short-circuits on "no gh on this host" and passes locally while turning the
    flake check red — the host-fact dependency #204's own comments are about, happening to
    the suite that checks for it.
    """
    real = qd.run_cmd
    real_which = qd.shutil.which

    def fake(argv, **kwargs):
        if argv and argv[0] == "gh":
            return rc, out, err
        return real(argv, **kwargs)

    monkeypatch.setattr(qd, "run_cmd", fake)
    monkeypatch.setattr(qd.shutil, "which",
                        lambda name: "/usr/bin/gh" if name == "gh" else real_which(name))


def test_a_repo_that_allows_squash_merges_fails_and_says_what_to_switch_off(
        monkeypatch, landing_repo):
    """#406 itself. `v3.8` was squash-merged, the squash discarded the `chore(release)`
    commit `refs/tags/v3.8` had been reserved against, and the tag spent the night
    addressing a commit that is not in main's history while every check was green."""
    _gh_says(monkeypatch, out=json.dumps({**MERGE_COMMITS_ONLY, "allow_squash_merge": True}))

    check = qd.check_merges(host_for(landing_repo))

    assert check.verdict == "fail"
    assert "allows squash merges" in check.detail
    assert "exposes a reserve subcommand" in check.detail
    assert "allow_squash_merge=false" in check.manual
    assert "acme/thing" in check.manual


def test_rebase_merges_are_the_same_defect_and_are_not_left_waiting(
        monkeypatch, landing_repo):
    """A rebase replays the branch onto new parents, so the sha that reserved the tag is
    not the sha that lands — the identical failure. Disabling only the one that happened
    to bite would leave the other in place for whoever clicks it next."""
    _gh_says(monkeypatch, out=json.dumps({**MERGE_COMMITS_ONLY, "allow_rebase_merge": True}))

    check = qd.check_merges(host_for(landing_repo))

    assert check.verdict == "fail"
    assert "allows rebase merges" in check.detail
    assert "allow_rebase_merge=false" in check.manual


def test_merge_commits_only_is_the_pass(monkeypatch, landing_repo):
    _gh_says(monkeypatch, out=json.dumps(MERGE_COMMITS_ONLY))

    check = qd.check_merges(host_for(landing_repo))

    assert check.verdict == "ok"
    assert "merge commits only" in check.detail


def test_a_repo_that_allows_no_preserving_strategy_fails_too(monkeypatch, landing_repo):
    """The property the row states is "every landing preserves the pushed commit", which
    needs both halves. Asserting only that squash is off would pass a repo where the sole
    remaining strategy also rewrites."""
    _gh_says(monkeypatch, out=json.dumps({**MERGE_COMMITS_ONLY, "allow_merge_commit": False}))

    check = qd.check_merges(host_for(landing_repo))

    assert check.verdict == "fail"
    assert "no merge strategy that preserves" in check.detail


def test_a_repo_with_no_tag_allocator_is_not_asked_and_is_not_failed(monkeypatch, repo):
    """The scope rule. A repo that reserves no release tags has nothing for a merge to
    orphan, and a FAIL a reader cannot act on is how a row gets ignored."""
    _git(repo, "remote", "add", "origin", "git@github.com:acme/thing.git")
    _gh_says(monkeypatch, out=json.dumps({**MERGE_COMMITS_ONLY, "allow_squash_merge": True}))

    check = qd.check_merges(host_for(repo))

    assert check.verdict == "ok"
    assert "nothing here reserves a release tag at push time" in check.detail


def test_a_tagger_that_exists_and_reserves_nothing_is_out_of_scope(monkeypatch, repo):
    """The defect this row landed with, and the reason the predicate had to change.

    #406 shipped a row that asked whether `scripts/release_tag.py` EXISTS, and #122
    removed push-time reservation twelve hours later while leaving that file in place
    with `backfill`, `taken` and `check`. The row went on firing and reporting `ok` for a
    reason that had stopped being true — right by accident, which is a check nobody
    notices going wrong — and its `fail` text would have told a reader to switch off
    squash merges to protect a reservation nothing takes.
    """
    _git(repo, "remote", "add", "origin", "git@github.com:acme/thing.git")
    (repo / "scripts").mkdir()
    (repo / "scripts" / "release_tag.py").write_text(TAGGER_THAT_RESERVES_NOTHING)
    _gh_says(monkeypatch, out=json.dumps({**MERGE_COMMITS_ONLY, "allow_squash_merge": True}))

    check = qd.check_merges(host_for(repo))

    assert check.verdict == "ok"
    assert "nothing here reserves" in check.detail
    assert "reserved at push time" not in check.detail


def test_the_word_reserve_in_the_prose_that_says_it_is_gone_is_not_a_reservation(repo):
    """Read against THIS repo's real files, not a fixture's paraphrase of them.

    `scripts/release_tag.py` opens with "`reserve` is deleted" and
    `harness/githooks/pre-push` explains the removed mechanism in a comment block naming
    `release_tag.py reserve` in full. A predicate that searched for the word would put
    the repository that REMOVED the reservation in scope on the strength of the
    paragraph saying so — so the two files that would break it are the two this asserts
    against.
    """
    tagger = HARNESS.parent / "scripts" / "release_tag.py"
    hook = HARNESS / "githooks" / "pre-push"
    assert tagger.is_file() and hook.is_file(), "this test is about this repo's own files"
    assert "reserve" in tagger.read_text() and "reserve" in hook.read_text()

    (repo / "scripts").mkdir()
    (repo / "scripts" / "release_tag.py").write_text(tagger.read_text())
    hooks = Path(_git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")) / "h"
    hooks.mkdir()
    (hooks / "pre-push").write_text(hook.read_text())
    _git(repo, "config", "core.hooksPath", str(hooks))

    assert qd.reservation_sites(host_for(repo)) == ([], [])


def test_a_pre_push_hook_that_reserves_puts_the_repo_in_scope_with_no_tagger_at_all(
        monkeypatch, repo):
    """The second half of "does anything here reserve a tag at push time".

    A reservation happens on a push, and the hook is where a push happens. A repo that
    reserves from the hook directly, or whose tagger was renamed out of the three places
    `release_tagger` looks, has the #406 hazard in full — and asking only about the
    tagger would report it as having nothing to orphan.
    """
    _git(repo, "remote", "add", "origin", "git@github.com:acme/thing.git")
    hooks = Path(_git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")) / "h"
    hooks.mkdir()
    (hooks / "pre-push").write_text('#!/bin/sh\n# a pre-push hook\nqb-tag reserve "$1"\n')
    _git(repo, "config", "core.hooksPath", str(hooks))
    _gh_says(monkeypatch, out=json.dumps({**MERGE_COMMITS_ONLY, "allow_squash_merge": True}))

    check = qd.check_merges(host_for(repo))

    assert check.verdict == "fail"
    assert "pre-push hook installed here has a reservation step" in check.detail


def test_the_hook_that_is_read_is_the_one_git_would_run(repo):
    """The rule that decides every check in this file: look where the mechanism RUNS.

    A `pre-push` sitting in a directory `core.hooksPath` does not name reserves nothing,
    because git never executes it — the same sentence as `ls
    harness/githooks/reference-transaction` succeeding on a host with no guard at all.
    """
    common = Path(_git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir"))
    (common / "elsewhere").mkdir()
    (common / "elsewhere" / "pre-push").write_text("#!/bin/sh\nqb-tag reserve\n")
    _git(repo, "config", "core.hooksPath", str(common / "hooks"))

    assert qd.reservation_sites(host_for(repo)) == ([], [])


def test_a_relative_hooks_path_is_resolved_against_the_worktree_git_runs_hooks_from(
        repo, tmp_path, monkeypatch):
    """`core.hooksPath=githooks` means the worktree's `githooks/`, not this process's.

    `qb-hooks`' own `effective_delegate` states the rule in full and had to, for the
    same reason: resolved against wherever the tool happened to be invoked from, the
    path names a directory that usually does not exist — and a hook nobody could find
    reads as a repo with no hook at all, which here would be a false `ok` on a repo
    that reserves on every push.
    """
    (repo / "githooks").mkdir()
    (repo / "githooks" / "pre-push").write_text("#!/bin/sh\nrelease_tag.py reserve\n")
    _git(repo, "config", "core.hooksPath", "githooks")
    monkeypatch.chdir(tmp_path)

    found, unreadable = qd.reservation_sites(host_for(repo))

    assert unreadable == []
    assert found == ["the pre-push hook installed here has a reservation step"]


def test_a_reservation_in_the_delegate_the_hook_chains_to_is_still_a_reservation(repo):
    """`qb-hooks install` leaves a `pre-push.delegate` symlink whenever the machine
    already had a `pre-push` to keep running, and the managed hook pipes the push into
    it. A reservation performed there happens on exactly the pushes this row is about,
    and reading only the top file would report the repo as having nothing to orphan."""
    hooks = Path(_git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")) / "h"
    hooks.mkdir()
    (hooks / "pre-push").write_text("#!/bin/sh\n# the managed hook, which chains\nexit 0\n")
    (hooks / "pre-push.delegate").write_text("#!/bin/sh\nqb-tag reserve \"$1\"\n")
    _git(repo, "config", "core.hooksPath", str(hooks))

    found, unreadable = qd.reservation_sites(host_for(repo))

    assert unreadable == []
    assert found == ["pre-push.delegate, which the pre-push hook here runs has a "
                     "reservation step"]


def test_a_hook_that_sources_a_file_this_can_find_is_read_through_to_it(repo):
    hooks = Path(_git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")) / "h"
    hooks.mkdir()
    (hooks / "pre-push").write_text("#!/bin/sh\n. ./release-hooks.sh\n")
    (repo / "release-hooks.sh").write_text("#!/bin/sh\nrelease_tag.py reserve\n")
    _git(repo, "config", "core.hooksPath", str(hooks))

    found, unreadable = qd.reservation_sites(host_for(repo))

    assert unreadable == []
    assert found == ["release-hooks.sh, which the pre-push hook here runs has a "
                     "reservation step"]


@pytest.mark.parametrize("body,phrase", [
    ('#!/bin/sh\nexec "$HOOK_DIR/pre-push.local" "$@"\n', "cannot resolve"),
    ("#!/bin/sh\nlefthook run pre-push\n", "lefthook"),
    ("#!/bin/sh\nhusky\n", "husky"),
])
def test_a_hook_that_hands_its_work_somewhere_unread_is_unknown_and_not_a_pass(
        monkeypatch, repo, body, phrase):
    """Codex's first finding, and the one that mattered most.

    A hook whose real work happens in a delegate spelled with a variable, or in a
    runner with its own configuration file, has NOT been read by reading it. Reporting
    "nothing here reserves" on the strength of the part that was read is the exact
    collapse this file forbids: a check that could not be made rendering as one that
    passed.
    """
    _git(repo, "remote", "add", "origin", "git@github.com:acme/thing.git")
    hooks = Path(_git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")) / "h"
    hooks.mkdir()
    (hooks / "pre-push").write_text(body)
    _git(repo, "config", "core.hooksPath", str(hooks))
    _gh_says(monkeypatch, out=json.dumps(MERGE_COMMITS_ONLY))

    check = qd.check_merges(host_for(repo))

    assert check.verdict == "unknown"
    assert phrase in check.detail


@pytest.mark.parametrize("noise", [
    'echo "reserve"\n',
    'reason="reserve"\n',
    'cat <<EOF\nrun release_tag.py reserve to take a number\nEOF\n',
    'git push origin main   # this used to reserve a tag\n',
])
def test_the_word_in_a_string_a_heredoc_or_a_trailing_comment_is_not_a_reservation(
        repo, noise):
    """A false positive here is not a wasted question — it is a `FAIL` recommending a
    change to a setting every contributor and every other machine in the fleet shares.
    So the word has to appear as a command argument: comments, heredoc bodies and the
    insides of quoted spans are all taken out before it is looked for."""
    hooks = Path(_git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")) / "h"
    hooks.mkdir()
    (hooks / "pre-push").write_text("#!/bin/sh\n" + noise)
    _git(repo, "config", "core.hooksPath", str(hooks))

    assert qd.reservation_sites(host_for(repo)) == ([], [])


def test_a_here_string_does_not_swallow_the_rest_of_the_hook(repo):
    """`<<<` is a here-STRING, not a heredoc, and this repo's own `pre-push` uses two of
    them. A heredoc scanner that took `<<<"$lines"` for an opener would skip everything
    after it, and every hook in the fleet would read as reserving nothing."""
    hooks = Path(_git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")) / "h"
    hooks.mkdir()
    (hooks / "pre-push").write_text(
        '#!/bin/bash\nread -r x <<<"$lines"\nqb-tag reserve\n')
    _git(repo, "config", "core.hooksPath", str(hooks))

    found, _unreadable = qd.reservation_sites(host_for(repo))

    assert found == ["the pre-push hook installed here has a reservation step"]


@pytest.mark.parametrize("source,reserves", [
    ('sub.add_parser("reserve")\n', True),
    ('@click.command()\ndef reserve():\n    pass\n', True),
    ('@cli.command("reserve")\ndef take():\n    pass\n', True),
    ('app.command()(reserve)\nsub.add_parser("check")\n', True),
    ('sub.add_parser("check")\nsub.add_parser("backfill")\n', False),
])
def test_the_command_set_is_enumerated_out_of_the_parse_tree(repo, source, reserves):
    """Every shape that genuinely registers a `reserve` command, and one that does not.

    Codex found four of these missing from the pattern that preceded the parse tree:
    click's default, which names the FUNCTION and not a string, and the applied-by-hand
    decorator among them. Each was a real `reserve` command reported as no reservation
    at all.
    """
    (repo / "scripts").mkdir()
    (repo / "scripts" / "release_tag.py").write_text("import x\n" + source)

    found, unreadable = qd.reservation_sites(host_for(repo))

    assert unreadable == []
    assert bool(found) is reserves


@pytest.mark.parametrize("source", [
    '"""Run `add_parser("reserve")` to take a number."""\nsub.add_parser("check")\n',
    '# sub.add_parser("reserve")  — deleted in #122\nsub.add_parser("check")\n',
    'HELP = \'sub.add_parser("reserve")\'\nsub.add_parser("check")\n',
])
def test_registration_shaped_text_that_is_not_a_registration_is_not_one(repo, source):
    """The reason the enumeration is a parse and not a search. A docstring, a comment
    and a string constant can all carry the exact call this looks for, and this repo's
    own tagger opens with a paragraph about the `reserve` it no longer has."""
    (repo / "scripts").mkdir()
    (repo / "scripts" / "release_tag.py").write_text(source)

    assert qd.reservation_sites(host_for(repo)) == ([], [])


@pytest.mark.parametrize("source,why", [
    ("sub = p.add_subparsers()\nsub.add_parser(COMMAND_RESERVE)\n", "a name in a variable"),
    ("sub = p.add_subparsers()\nregister(sub)\nsub.add_parser('check')\n",
     "the subparser filled in elsewhere"),
    ("def f(:\n", "a file that does not parse"),
    ("import fire\n\nclass Tag:\n    pass\n", "a CLI in no shape this reads"),
])
def test_a_command_set_that_was_not_enumerated_is_unknown_and_never_a_pass(
        monkeypatch, repo, source, why):
    """The contract :func:`_python_subcommands` keeps: a set means THESE are the
    subcommands, and None means somebody has to look. An empty result for a file whose
    commands are named by a variable, or registered by a function in another module, is
    a check that could not be made — and rendering it as `ok` is what this whole file
    exists to refuse."""
    _git(repo, "remote", "add", "origin", "git@github.com:acme/thing.git")
    (repo / "scripts").mkdir()
    (repo / "scripts" / "release_tag.py").write_text(source)
    _gh_says(monkeypatch, out=json.dumps(MERGE_COMMITS_ONLY))

    check = qd.check_merges(host_for(repo))

    assert check.verdict == "unknown", why
    assert "does not declare its subcommands" in check.detail


def test_a_tagger_that_is_not_python_is_unknown_unless_it_names_the_reservation(
        monkeypatch, repo):
    """A shell wrapper's command set is wherever it forwards to. Finding the word is
    evidence; not finding it is not evidence of the opposite, so the absent case is an
    `unknown` with a remedy rather than a pass. Codex's fifth finding: `exec python -m
    company.release_cli "$@"` reserves or does not, and this cannot tell which."""
    _git(repo, "remote", "add", "origin", "git@github.com:acme/thing.git")
    wrapper = repo / "tools" / "tagger"
    wrapper.parent.mkdir()
    wrapper.write_text('#!/bin/sh\nexec python -m company.release_cli "$@"\n')
    _git(repo, "config", "qb.releaseTag", "tools/tagger")
    _gh_says(monkeypatch, out=json.dumps(MERGE_COMMITS_ONLY))

    check = qd.check_merges(host_for(repo))

    assert check.verdict == "unknown"
    assert "is not Python" in check.detail

    wrapper.write_text('#!/bin/sh\nexec qb-release reserve "$@"\n')
    named = qd.check_merges(host_for(repo))
    assert named.verdict == "ok"                    # this repo allows merge commits only
    assert "tools/tagger has a reserve step" in named.detail


@pytest.mark.parametrize("key", ["qb.releaseTag", "core.hooksPath"])
def test_a_git_config_this_cannot_read_is_unknown_rather_than_unset(
        monkeypatch, repo, key):
    """"The key is not set" and "the configuration could not be read" are different
    answers, and `git config --get` gives them different exit codes for that reason.
    Collapsed together, an unparsable include reads as "no tagger configured here",
    which falls back to the conventional filename and answers confidently about the
    wrong file — or as "hooks live in .git/hooks", which reads a hook that never
    runs."""
    _git(repo, "remote", "add", "origin", "git@github.com:acme/thing.git")
    real = qd.run_cmd

    def fake(argv, **kw):
        if argv[:2] == ["git", "-C"] and "config" in argv and key in argv:
            return 128, "", "fatal: bad config line 3 in file .git/config"
        return real(argv, **kw)

    monkeypatch.setattr(qd, "run_cmd", fake)
    _gh_says(monkeypatch, out=json.dumps(MERGE_COMMITS_ONLY))
    monkeypatch.setattr(qd, "run_cmd", fake)

    check = qd.check_merges(host_for(repo))

    assert check.verdict == "unknown"
    assert f"`{key}`" in check.detail


def test_a_tagger_whose_cli_cannot_be_parsed_is_unknown_rather_than_out_of_scope(
        monkeypatch, repo):
    """The honest-unknown rule, applied to this row's own predicate.

    The pattern reads argparse, a dispatch table's key, an `argv[1]` compare and a named
    click command. A Python tagger written in some fifth style is a file this cannot
    parse, and "the pattern did not match" is not evidence about what the tool does. An
    `ok` there would be a check that could not be made rendering as one that passed.
    """
    _git(repo, "remote", "add", "origin", "git@github.com:acme/thing.git")
    (repo / "scripts").mkdir()
    (repo / "scripts" / "release_tag.py").write_text("import fire\n\nclass Tag:\n    pass\n")
    _gh_says(monkeypatch, out=json.dumps(MERGE_COMMITS_ONLY))

    check = qd.check_merges(host_for(repo))

    assert check.verdict == "unknown"
    assert "does not declare its subcommands" in check.detail
    assert check.manual


def test_the_allocator_is_found_the_way_the_pre_push_hook_finds_it(monkeypatch, repo):
    """`QB_RELEASE_TAG`, then `qb.releaseTag`, then `scripts/release_tag.py`. Read from the
    same three places as `harness/githooks/pre-push` rather than by a rule of qb-doctor's
    own: the two have to agree about which repos are in scope, and a second predicate would
    agree right up until somebody moved the script."""
    _git(repo, "remote", "add", "origin", "git@github.com:acme/thing.git")
    elsewhere = repo / "tools" / "tagger.py"
    elsewhere.parent.mkdir()
    elsewhere.write_text(RESERVING_TAGGER)
    _git(repo, "config", "qb.releaseTag", "tools/tagger.py")
    _gh_says(monkeypatch, out=json.dumps({**MERGE_COMMITS_ONLY, "allow_squash_merge": True}))

    assert qd.check_merges(host_for(repo)).verdict == "fail"

    monkeypatch.setenv("QB_RELEASE_TAG", str(elsewhere))
    _git(repo, "config", "--unset", "qb.releaseTag")
    assert qd.check_merges(host_for(repo)).verdict == "fail"


@pytest.mark.parametrize("url", [
    "git@gitlab.com:acme/thing.git",
    "https://git.example.com/acme/thing.git",
    "https://gitlab.com/github.com/acme/thing.git",
])
def test_a_remote_that_is_not_github_is_unknown_rather_than_ok(monkeypatch, repo, url):
    """The honest-unknown rule, and the third URL is why the pattern is anchored: a slug
    guessed off a host this tool cannot query would make the row an `unknown` about the
    wrong repository, which reads as an answer."""
    _git(repo, "remote", "add", "origin", url)
    (repo / "scripts").mkdir()
    (repo / "scripts" / "release_tag.py").write_text(RESERVING_TAGGER)
    _gh_says(monkeypatch, out=json.dumps(MERGE_COMMITS_ONLY))

    check = qd.check_merges(host_for(repo))

    assert check.verdict == "unknown"
    assert "not a github.com remote" in check.detail


@pytest.mark.parametrize("url", [
    "git@github.com:acme/thing.git",
    "https://github.com/acme/thing",
    "https://token@github.com/acme/thing.git",
    "ssh://git@github.com/acme/thing.git",
])
def test_every_shape_a_github_remote_is_written_in_resolves_to_the_same_slug(repo, url):
    _git(repo, "remote", "add", "origin", url)
    assert qd.github_slug(host_for(repo)) == "acme/thing"


def test_no_gh_on_this_host_is_unknown_and_never_a_pass(monkeypatch, landing_repo):
    monkeypatch.setattr(qd.shutil, "which", lambda _n: None)

    check = qd.check_merges(host_for(landing_repo))

    assert check.verdict == "unknown"
    assert "no gh on this host" in check.detail


def test_a_gh_that_cannot_answer_is_unknown_and_quotes_what_it_said(
        monkeypatch, landing_repo):
    _gh_says(monkeypatch, rc=1, err="gh: not authenticated\nrun gh auth login")

    check = qd.check_merges(host_for(landing_repo))

    assert check.verdict == "unknown"
    assert "gh: not authenticated" in check.detail


@pytest.mark.parametrize("body", ["<html>rate limited</html>", "null", "[]", '"nope"'])
def test_a_gh_that_answers_something_other_than_an_object_is_unknown(
        monkeypatch, landing_repo, body):
    """Codex's finding on this change. Parsing is not the test — `null` and `[]` are valid
    JSON, and reading `.get` off either raises. The row still came out `unknown`, but via
    `run_checks` catching an AttributeError, so it reported "the check itself raised"
    instead of the remedy this branch exists to print."""
    _gh_says(monkeypatch, out=body)

    check = qd.check_merges(host_for(landing_repo))

    assert check.verdict == "unknown"
    assert "did not answer an object" in check.detail
    assert check.manual


def test_a_token_with_no_push_access_is_unknown_and_says_which_silence_it_is(
        monkeypatch, landing_repo):
    """GitHub returns the merge-strategy fields ONLY to a token with push access; without
    one the answer is three nulls and no explanation. Reading those as `false` would report
    "merge commits only" about a repo nobody here can see the settings of — the #324
    collapse, in the row whose whole job is to notice a switch is on."""
    _gh_says(monkeypatch, out=json.dumps({"allow_merge_commit": None,
                                          "allow_squash_merge": None,
                                          "allow_rebase_merge": None, "push": False}))

    check = qd.check_merges(host_for(landing_repo))

    assert check.verdict == "unknown"
    assert "no push access" in check.detail
    assert "somebody with push access" in check.manual


def test_a_field_missing_despite_push_access_is_still_unknown(monkeypatch, landing_repo):
    """The other silence: a forge old enough to omit the field, or a `--jq` that stopped
    matching. Different remedy, same refusal to read absence as `false`."""
    _gh_says(monkeypatch, out=json.dumps({**MERGE_COMMITS_ONLY, "allow_squash_merge": None}))

    check = qd.check_merges(host_for(landing_repo))

    assert check.verdict == "unknown"
    assert "did not say whether" in check.detail
    assert "allow_squash_merge" in check.detail


@pytest.mark.parametrize("settings", [
    {**MERGE_COMMITS_ONLY, "allow_squash_merge": True},
    {**MERGE_COMMITS_ONLY, "allow_merge_commit": False},
])
def test_the_merges_row_never_offers_to_fix_itself(monkeypatch, landing_repo, settings):
    """DETECT, DO NOT SET. Every `fix` this tool runs writes to THIS host — a hooks
    directory, a systemd unit, a venv. This one would write a setting every contributor and
    every other machine shares, from whichever checkout somebody typed `--fix` in, and it
    needs admin rights a read-only token does not have. So it is `manual`, like the edge
    secret: the remedy is printed in full and run by a person."""
    _gh_says(monkeypatch, out=json.dumps(settings))

    check = qd.check_merges(host_for(landing_repo))

    assert check.verdict == "fail"
    assert check.fix is None, "a doctor must not silently change a shared repository setting"
    assert check.manual


# --------------------------------------------------------------------------- #
# groups — the category #407 extends, not a row it has to retrofit one around
# --------------------------------------------------------------------------- #

def test_every_check_belongs_to_a_group_this_file_declares():
    for name, group, _fn in qd.CHECKS:
        assert group in qd.GROUPS, f"{name} is filed under {group!r}, which is not a group"


def test_only_accepts_a_group_name_and_expands_it_to_its_rows(monkeypatch):
    """`--only landing` names a question rather than a list of rows, which is what made
    #407 an extension of a category instead of a retrofit around a single check."""
    seen: list[set[str] | None] = []
    monkeypatch.setattr(qd, "survey", lambda *_a, **_k: object())
    monkeypatch.setattr(qd, "run_checks",
                        lambda _h, only=None: (seen.append(only),
                                               [qd.Check("merges", "-", "x", "ok")])[1])

    qd.main(["--only", "landing"])

    assert seen == [{n for n, g, _fn in qd.CHECKS if g == "landing"}]
    assert len(seen[0]) > 1, "the point of a group is that it holds more than one row"


def test_a_group_name_that_does_not_exist_is_refused_before_anything_runs(capsys):
    assert qd.main(["--only", "nosuchgroup"]) == 3
    assert "no such check or group" in capsys.readouterr().err


def test_a_rows_group_comes_from_the_registration_not_from_the_check(monkeypatch, repo):
    """Assigned by `run_checks` rather than passed at every construction site, so a check
    function cannot file itself under a group its registration disagrees with — including
    the synthetic row `run_checks` builds when a check raises."""
    def boom(_host):
        raise RuntimeError("boom")

    monkeypatch.setattr(qd, "CHECKS", (("merges", "landing", boom),))
    rows = qd.run_checks(host_for(repo))

    assert rows[0].group == "landing"
    assert rows[0].verdict == "unknown"
    assert rows[0].as_dict()["group"] == "landing"


# --------------------------------------------------------------------------- #
# the landing group (#407) — is the line moving, is main moving, are the tags sound
# --------------------------------------------------------------------------- #

def _iso(minutes_ago: float) -> str:
    """An ISO-8601 instant that many minutes in the past."""
    return (datetime.now(UTC) - timedelta(minutes=minutes_ago)).isoformat()


def _queue(queued: int, ready: int, *, oldest: float = 1.0, pr: int = 7,
           holder: str = "zeus/jasper-moss") -> dict:
    """What `GET /merge-queue` answers, in the shape `app/api/merge_queue.py` sends it."""
    entries = [{"pr": pr + i, "position": i + 1, "holder": holder,
                "ready": i < ready, "entered": _iso(oldest - i)}
               for i in range(queued)]
    return {"repo": "acme/thing", "base": "main", "entries": entries,
            "head": entries[0] if entries else None,
            "counts": {"queued": queued, "ready": ready, "not_ready": queued - ready}}


def _board_says(monkeypatch, payload, *, status: int = 200, err: str | None = None) -> None:
    """Answer for the board and nothing else. `payload` may be a dict or raw text."""
    body = payload if isinstance(payload, str) else json.dumps(payload)
    monkeypatch.setattr(qd, "http_get", lambda url, headers=None: (status, body, err))


def _gh_answers(monkeypatch, answers: dict[str, tuple[int, str, str]], *,
                have_gh: bool = True) -> None:
    """Answer several DIFFERENT gh calls, chosen by a marker in the argv.

    `check_landed` makes two — `gh pr list` and `gh api repos/…/commits/…` — and a
    fixture that answered both with one body would have the row reading a list of pull
    requests as a commit date and still passing, which is a test asserting its own stub.
    """
    real = qd.run_cmd
    real_which = qd.shutil.which

    def fake(argv, **kwargs):
        if argv and argv[0] == "gh":
            joined = " ".join(argv)
            for marker, answer in answers.items():
                if marker in joined:
                    return answer
            return 1, "", f"no stub for: {joined}"
        return real(argv, **kwargs)

    monkeypatch.setattr(qd, "run_cmd", fake)
    monkeypatch.setattr(qd.shutil, "which",
                        lambda n: ("/usr/bin/gh" if have_gh else None) if n == "gh"
                        else real_which(n))


@pytest.fixture
def landing_host(landing_repo: Path) -> qd.Host:
    """A repo the whole landing group is in scope for: GitHub remote, board, token."""
    (landing_repo / "changelog.d").mkdir()
    return host_for(landing_repo, base_url="https://board.example", token="t")


# ----------------------------------------------------------------- queue

def test_an_empty_queue_is_not_a_fault(monkeypatch, landing_host):
    """"Do not fail on irrelevance". A repo with nothing waiting to land has an empty
    queue, and that is the healthy state, not an absent one."""
    _board_says(monkeypatch, _queue(0, 0))

    check = qd.check_queue(landing_host)

    assert check.verdict == "ok"
    assert "nothing is queued" in check.detail


def test_a_queue_with_something_ready_is_moving(monkeypatch, landing_host):
    _board_says(monkeypatch, _queue(3, 1))

    check = qd.check_queue(landing_host)

    assert check.verdict == "ok"
    assert "3 queued" in check.detail and "1 ready" in check.detail


def test_nothing_ready_for_a_few_minutes_is_a_landing_in_progress(monkeypatch, landing_host):
    """The other half of the predicate, and the reason it needs a clock. Nothing ready is
    the normal state of a queue whose head is mid-preland — PR #398 was timed at 5m37s
    and 12m59s from merge to green — so the pair alone is not a finding."""
    _board_says(monkeypatch, _queue(3, 0, oldest=4))

    check = qd.check_queue(landing_host)

    assert check.verdict == "ok"
    assert "inside a landing" in check.detail


def test_a_queue_with_nothing_ready_for_longer_than_a_landing_is_the_stall(
        monkeypatch, landing_host):
    """#405. Seven green pull requests, zero ready, main unmoved for three hours — and
    nine `ok` rows about it. This is the row that disagrees."""
    _board_says(monkeypatch, _queue(7, 0, oldest=190, pr=401, holder="zeus/opal-vermeil"))

    check = qd.check_queue(landing_host)

    assert check.verdict == "fail"
    assert "7 queued" in check.detail and "NONE ready" in check.detail
    assert "3h 10m" in check.detail
    # A stalled queue is somebody to talk to, which is the only remedy there is: nothing
    # here evicts an entry or merges anything (#405 — the queue stays advisory).
    assert "zeus/opal-vermeil" in check.manual and "#401" in check.manual


def test_a_stalled_queue_names_no_command_that_changes_it(monkeypatch, landing_host):
    _board_says(monkeypatch, _queue(7, 0, oldest=190))

    check = qd.check_queue(landing_host)

    assert check.fix is None, "the queue is advisory; nothing here may evict or merge (#405)"


@pytest.mark.parametrize("kw,phrase", [
    ({"base_url": None}, "no board is configured"),
    ({"token": None}, "resolved no board token"),
])
def test_a_queue_that_could_not_be_asked_about_is_unknown(landing_host, kw, phrase):
    """The rule this group is bound by. A landing group that went green having seen
    nothing would be a worse version of the problem it exists to solve."""
    host = qd.Host(**{**landing_host.__dict__, **kw})

    check = qd.check_queue(host)

    assert check.verdict == "unknown"
    assert phrase in check.detail


@pytest.mark.parametrize("payload,status,err,phrase", [
    (None, 0, "URLError: refused", "could not reach the board"),
    ({}, 503, None, "answered 503"),
    ("not json", 200, None, "did not answer JSON"),
    ({"counts": {"queued": "several"}}, 200, None, "usable pair of queue counts"),
    ({"counts": {"queued": 0, "ready": -1}}, 200, None, "usable pair of queue counts"),
    ({"counts": {"queued": 1, "ready": 2}}, 200, None, "usable pair of queue counts"),
    ({"counts": {"queued": True, "ready": False}}, 200, None, "usable pair of queue counts"),
])
def test_a_board_that_will_not_state_the_queue_is_unknown_and_never_ok(
        monkeypatch, landing_host, payload, status, err, phrase):
    _board_says(monkeypatch, payload if payload is not None else "", status=status, err=err)

    check = qd.check_queue(landing_host)

    assert check.verdict == "unknown"
    assert phrase in check.detail


def test_a_queue_whose_entries_carry_no_arrival_time_is_unknown_not_a_stall(
        monkeypatch, landing_host):
    """How long the pair has held is half the predicate. Without it the row can neither
    fail nor pass, and guessing either way is the collapse this file forbids."""
    payload = _queue(3, 0)
    for e in payload["entries"]:
        e.pop("entered")
    _board_says(monkeypatch, payload)

    check = qd.check_queue(landing_host)

    assert check.verdict == "unknown"
    assert "no usable arrival time" in check.detail


def test_a_queue_entry_that_arrived_in_the_future_is_not_evidence_of_health(
        monkeypatch, landing_host):
    """Clock skew, or a board answering nonsense. Read as an age it is negative, which
    lands in the "inside a landing" pass — so it is discarded before the clock is read,
    and a queue with nothing else to go on is an `unknown`."""
    _board_says(monkeypatch, _queue(3, 0, oldest=-90))

    check = qd.check_queue(landing_host)

    assert check.verdict == "unknown"
    assert "no usable arrival time" in check.detail


# ----------------------------------------------------------------- landed

_COMMIT = "repos/acme/thing/commits/main"


def test_green_pull_requests_and_a_main_that_has_not_moved_is_the_finding(
        monkeypatch, landing_host):
    """"Six green pull requests, main unchanged for 4h" — the single most legible
    statement of the night #407 was filed, which no row anywhere made."""
    _gh_answers(monkeypatch, {
        "pr list": (0, json.dumps([
            {"number": 401, "isDraft": False, "mergeStateStatus": "CLEAN"},
            {"number": 403, "isDraft": False, "mergeStateStatus": "CLEAN"},
            {"number": 404, "isDraft": False, "mergeStateStatus": "DIRTY"}]), ""),
        _COMMIT: (0, _iso(250), ""),
    })

    check = qd.check_landed(landing_host)

    assert check.verdict == "fail"
    assert "#401" in check.detail and "#403" in check.detail
    assert "#404" not in check.detail, "a conflicting PR is not ready to land"
    assert "4h 10m" in check.detail


def test_a_still_main_with_nothing_ready_is_not_a_stall(monkeypatch, landing_host):
    """Both halves, or the row fires every night on every quiet repo — which is how a row
    gets ignored."""
    _gh_answers(monkeypatch, {
        "pr list": (0, json.dumps([{"number": 401, "isDraft": False,
                                    "mergeStateStatus": "BLOCKED"}]), ""),
        _COMMIT: (0, _iso(600), ""),
    })

    check = qd.check_landed(landing_host)

    assert check.verdict == "ok"
    assert "not a stall" in check.detail


def test_a_ready_pull_request_and_a_main_that_moved_recently_is_fine(
        monkeypatch, landing_host):
    _gh_answers(monkeypatch, {
        "pr list": (0, json.dumps([{"number": 401, "isDraft": False,
                                    "mergeStateStatus": "CLEAN"}]), ""),
        _COMMIT: (0, _iso(9), ""),
    })

    check = qd.check_landed(landing_host)

    assert check.verdict == "ok"
    assert "9m ago" in check.detail


def test_a_draft_is_not_ready_to_land(monkeypatch, landing_host):
    _gh_answers(monkeypatch, {
        "pr list": (0, json.dumps([{"number": 401, "isDraft": True,
                                    "mergeStateStatus": "CLEAN"}]), ""),
        _COMMIT: (0, _iso(600), ""),
    })

    assert qd.check_landed(landing_host).verdict == "ok"


def test_a_merge_state_github_has_not_computed_is_unknown_and_not_unready(
        monkeypatch, landing_host):
    """GitHub resolves `mergeStateStatus` lazily, so a pull request nobody has asked
    about since its last push answers UNKNOWN. Read as "not ready" it would make a
    stalled repo look quiet — the absent signal rendering as the benign one, one more
    time."""
    _gh_answers(monkeypatch, {
        "pr list": (0, json.dumps([{"number": 401, "isDraft": False,
                                    "mergeStateStatus": "UNKNOWN"}]), ""),
        _COMMIT: (0, _iso(600), ""),
    })

    check = qd.check_landed(landing_host)

    assert check.verdict == "unknown"
    assert "#401" in check.detail


@pytest.mark.parametrize("answers,have_gh,phrase", [
    ({}, False, "no gh on this host"),
    ({"pr list": (1, "", "gh: HTTP 401")}, True, "could not list the pull requests"),
    ({"pr list": (0, "[]", ""), _COMMIT: (1, "", "gh: not found")}, True,
     "could not read when the tip of main was committed"),
])
def test_a_landing_row_that_could_not_ask_github_is_unknown(
        monkeypatch, landing_host, answers, have_gh, phrase):
    _gh_answers(monkeypatch, answers, have_gh=have_gh)

    check = qd.check_landed(landing_host)

    assert check.verdict == "unknown"
    assert phrase in check.detail


def test_a_truncated_read_cannot_report_that_nothing_is_ready(monkeypatch, landing_host):
    """The cap was disclosed in the detail and the verdict was `ok` anyway, which is a
    disclosure making an unmade check sound careful. The pull request beyond the cap is
    exactly where the ready one would be."""
    _gh_answers(monkeypatch, {
        "pr list": (0, json.dumps([{"number": n, "isDraft": False,
                                    "mergeStateStatus": "BLOCKED"}
                                   for n in range(qd.PR_SAMPLE)]), ""),
        _COMMIT: (0, _iso(600), ""),
    })

    check = qd.check_landed(landing_host)

    assert check.verdict == "unknown"
    assert f"first {qd.PR_SAMPLE} open pull requests" in check.detail


def test_a_truncated_read_can_still_report_what_it_did_see(monkeypatch, landing_host):
    """The other side of the same rule. Truncation cannot make a pull request that was
    read and is ready stop being ready, so the finding branches come first."""
    prs = [{"number": n, "isDraft": False, "mergeStateStatus": "BLOCKED"}
           for n in range(qd.PR_SAMPLE - 1)]
    prs.append({"number": 999, "isDraft": False, "mergeStateStatus": "CLEAN"})
    _gh_answers(monkeypatch, {"pr list": (0, json.dumps(prs), ""),
                              _COMMIT: (0, _iso(600), "")})

    check = qd.check_landed(landing_host)

    assert check.verdict == "fail"
    assert "#999" in check.detail


# ----------------------------------------------------------------- generated

def test_a_pull_request_editing_the_generated_changelog_is_the_finding(
        monkeypatch, landing_host):
    """#122's measured evidence: on 2026-08-23 the three open pull requests that had
    written a release entry were all CONFLICTING and the three that had not were all
    MERGEABLE. The edit is the fault; the conflict is its commonest consequence."""
    _gh_answers(monkeypatch, {"pr list": (0, json.dumps([
        {"number": 398, "isDraft": False, "mergeStateStatus": "DIRTY",
         "files": [{"path": "CHANGELOG.md"}, {"path": "app/main.py"}]},
        {"number": 401, "isDraft": False, "mergeStateStatus": "CLEAN",
         "files": [{"path": "changelog.d/401.fix.md"}]}]), "")})

    check = qd.check_generated(landing_host)

    assert check.verdict == "fail"
    assert "#398" in check.detail and "#401" not in check.detail
    assert "1 of them already conflicting" in check.detail
    assert "changelog.d/<issue>.<kind>.md" in check.manual


def test_a_pull_request_touching_the_readme_is_not_the_finding(monkeypatch, landing_host):
    """The guard exempts the rest of README.md so that documenting anything is not taxed,
    and a list of changed paths cannot tell an edit to the release list from an edit to
    the installation instructions. Failing a branch for improving its own docs is how a
    row gets ignored."""
    _gh_answers(monkeypatch, {"pr list": (0, json.dumps([
        {"number": 401, "isDraft": False, "mergeStateStatus": "CLEAN",
         "files": [{"path": "README.md"}, {"path": "changelog.d/401.docs.md"}]}]), "")})

    assert qd.check_generated(landing_host).verdict == "ok"


def test_a_truncated_read_cannot_report_that_no_pull_request_edits_it(
        monkeypatch, landing_host):
    """Codex's first finding. `check_generated` noticed the exact truncation, said so in
    its detail, and returned `ok` — the 51st pull request being precisely where the
    offender would be."""
    _gh_answers(monkeypatch, {"pr list": (0, json.dumps(
        [{"number": n, "isDraft": False, "mergeStateStatus": "CLEAN",
          "files": [{"path": "app/main.py"}]} for n in range(qd.PR_SAMPLE)]), "")})

    check = qd.check_generated(landing_host)

    assert check.verdict == "unknown"
    assert f"first {qd.PR_SAMPLE} open pull requests" in check.detail


def test_github_is_asked_about_the_pull_requests_once(monkeypatch, landing_host):
    """Two rows, one call. Two would be slower and, worse, a way for the two rows to come
    to describe the same pull requests differently."""
    calls: list[list[str]] = []
    real = qd.run_cmd
    real_which = qd.shutil.which

    def counting(argv, **kw):
        if argv and argv[0] == "gh":
            calls.append(argv)
            if "pr" in argv:
                return 0, "[]", ""
            return 0, _iso(5), ""
        return real(argv, **kw)

    monkeypatch.setattr(qd, "run_cmd", counting)
    monkeypatch.setattr(qd.shutil, "which",
                        lambda n: "/usr/bin/gh" if n == "gh" else real_which(n))

    qd.check_landed(landing_host)
    qd.check_generated(landing_host)

    assert sum(1 for c in calls if "pr" in c and "list" in c) == 1


def test_a_repo_that_does_not_generate_its_release_notes_is_not_asked(monkeypatch, repo):
    """Scope. A repo whose branches legitimately write their own release entry is not
    failing anything, and there is no `changelog.d/` to say otherwise."""
    _gh_answers(monkeypatch, {})

    check = qd.check_generated(host_for(repo))

    assert check.verdict == "ok"
    assert "no changelog.d/" in check.detail


def test_the_generated_row_is_unknown_when_it_cannot_reach_github(monkeypatch, landing_host):
    _gh_answers(monkeypatch, {}, have_gh=False)

    check = qd.check_generated(landing_host)

    assert check.verdict == "unknown"
    assert "no gh on this host" in check.detail


# ------------------------------------------------------- stamper and briefs


@pytest.fixture
def stamping_repo(landing_repo: Path) -> Path:
    """A repo that does releases out of fragments, which is what puts it in scope."""
    (landing_repo / "changelog.d").mkdir()
    return landing_repo


def _briefs(harness: Path, **files: str) -> Path:
    d = harness / "commands"
    d.mkdir(parents=True, exist_ok=True)
    for name, body in files.items():
        (d / f"{name}.md").write_text(body)
    return d


def test_a_brief_that_runs_a_removed_release_command_in_a_fence_is_the_finding(
        tmp_path, stamping_repo):
    """Five agents stamped on the night of 2026-08-23 and every one was following a
    document. A mechanism removed from the code and left in the brief has not been
    removed."""
    harness = tmp_path / "installed"
    _briefs(harness, **{"fix-and-land": "Step 4:\n\n```bash\n"
                        "python3 scripts/release_stamp.py apply --onto origin/main\n```\n"})

    check = qd.check_briefs(host_for(stamping_repo, harness_bin=harness / "bin"))

    assert check.verdict == "fail"
    assert "fix-and-land.md" in check.detail
    assert "qb-bump" in check.manual


def test_a_quoted_path_in_a_fence_is_still_the_command_it_runs(tmp_path, stamping_repo):
    """The regression that hid a real finding for one commit. Briefs quote the paths they
    run — `python3 "$WT_DIR/scripts/release_stamp.py" preflight` is the line the installed
    `fix-and-review.md` carries — so emptying quoted spans here, which is exactly right
    when reading a hook, stopped this seeing the very thing it exists for."""
    harness = tmp_path / "installed"
    _briefs(harness, **{"fix-and-review": '```bash\npython3 "$WT_DIR/scripts/'
                        'release_stamp.py" preflight --repo "$WT_DIR"\n```\n'})

    assert qd.check_briefs(host_for(stamping_repo, harness_bin=harness / "bin")).verdict \
        == "fail"


def test_prose_explaining_the_removed_command_is_not_an_instruction_to_run_it(
        tmp_path, stamping_repo):
    """The line `test_no_brief_tells_an_agent_to_stamp_a_release` draws, drawn the same
    way here: the paragraph explaining why a branch does not stamp has to name the
    command to explain anything at all."""
    harness = tmp_path / "installed"
    _briefs(harness, **{"fix-and-land": "A branch never runs `release_stamp.py apply` — the "
                        "number is applied on main after the merge.\n\n```bash\n"
                        "gh pr merge --merge\n```\n"})

    assert qd.check_briefs(host_for(stamping_repo, harness_bin=harness / "bin")).verdict == "ok"


def test_a_commented_out_command_inside_a_fence_is_not_an_instruction(
        tmp_path, stamping_repo):
    """A false FAIL here sends somebody to rebuild a harness that was fine."""
    harness = tmp_path / "installed"
    _briefs(harness, **{"fix-and-land": "```bash\n# we used to run release_stamp.py here\n"
                        "gh pr merge --merge\n```\n"})

    assert qd.check_briefs(host_for(stamping_repo, harness_bin=harness / "bin")).verdict == "ok"


@pytest.mark.parametrize("body", [
    "~~~bash\nscripts/release_stamp.py apply\n~~~\n",
    "````bash\n```\nscripts/release_stamp.py apply\n```\n````\n",
    "1. do it:\n\n   ```bash\n   scripts/release_stamp.py apply\n   ```\n",
])
def test_every_fence_markdown_has_is_read_as_one(tmp_path, stamping_repo, body):
    """Codex named all three. A `~~~` fence was missed entirely; a four-backtick block was
    closed by the first ``` inside it, so everything after read as prose; and the indented
    fence is the reason any of this matters — every fence in `fix-and-review.md` sits
    inside a numbered step, and "no forbidden command in any code block" over zero code
    blocks passes on every possible file."""
    harness = tmp_path / "installed"
    _briefs(harness, **{"fix-and-land": body})

    assert qd.check_briefs(host_for(stamping_repo, harness_bin=harness / "bin")).verdict \
        == "fail"


def test_text_outside_every_fence_is_never_read_as_a_command(tmp_path, stamping_repo):
    """The other direction of the same scanner: a closing fence has to close."""
    harness = tmp_path / "installed"
    _briefs(harness, **{"fix-and-land": "```bash\ngh pr merge\n```\n\n"
                        "Then never run scripts/release_stamp.py apply again.\n"})

    assert qd.check_briefs(host_for(stamping_repo, harness_bin=harness / "bin")).verdict == "ok"


def test_the_briefs_read_are_the_ones_this_host_would_open_not_the_checkouts(
        tmp_path, stamping_repo):
    """The site that makes this a doctor row rather than only a test. The briefs on a host
    are the ones the harness on PATH ships, which is a different set from the
    repository's the moment that harness falls behind — and the `harness` row exists
    precisely because it does."""
    stale = tmp_path / "store" / "quarterback-harness-0.1.0"
    _briefs(stale / "share" / "quarterback-harness",
            **{"fix-and-land": "```bash\nscripts/release_stamp.py apply\n```\n"})
    current = tmp_path / "checkout" / "harness"
    _briefs(current, **{"fix-and-land": "```bash\ngh pr merge --merge\n```\n"})

    on_path = qd.check_briefs(host_for(stamping_repo, harness_bin=stale / "bin",
                                       source_harness=current))
    assert on_path.verdict == "fail", "the stale harness on PATH is what an agent reads"

    checkout_only = qd.check_briefs(host_for(stamping_repo, source_harness=current))
    assert checkout_only.verdict == "ok"


def test_a_brief_that_could_not_be_read_is_unknown_rather_than_a_clean_one(
        tmp_path, stamping_repo):
    """Codex found this: the walk skipped an unreadable file and the row went green on the
    ones that were left, which is a check that could not be made rendering as a pass."""
    harness = tmp_path / "installed"
    briefs = _briefs(harness, **{"fix-and-land": "```bash\ngh pr merge\n```\n",
                                 "panel-review-pr": "```bash\ngh pr merge\n```\n"})
    (briefs / "panel-review-pr.md").chmod(0o000)
    try:
        check = qd.check_briefs(host_for(stamping_repo, harness_bin=harness / "bin"))
    finally:
        (briefs / "panel-review-pr.md").chmod(0o644)

    assert check.verdict == "unknown"
    assert "panel-review-pr.md" in check.detail


def test_a_harness_with_no_briefs_at_all_is_unknown(tmp_path, stamping_repo):
    """A row that could not find the documents is not a row that read clean ones."""
    check = qd.check_briefs(host_for(stamping_repo, harness_bin=tmp_path / "empty" / "bin",
                                     source_harness=tmp_path / "empty"))

    assert check.verdict == "unknown"
    assert "no commands/ directory" in check.detail


def test_a_branch_side_stamper_is_its_own_row(tmp_path, stamping_repo):
    """#122 deleted the file rather than documenting against it, because a branch that can
    stamp itself will. Its own row and not half of a stamping one: a stamper in the
    repository and a stale brief on this machine have two owners and two remedies, and one
    row with two premises is how the `merges` row drifted."""
    (stamping_repo / "scripts" / "release_stamp.py").write_text("# apply a version\n")

    check = qd.check_stamper(host_for(stamping_repo))

    assert check.verdict == "fail"
    assert "scripts/release_stamp.py" in check.detail
    assert "release.py run" in check.manual


def test_a_repo_with_no_stamper_says_so(stamping_repo):
    assert qd.check_stamper(host_for(stamping_repo)).verdict == "ok"


@pytest.mark.parametrize("row", ["check_stamper", "check_briefs"])
def test_a_repo_that_does_not_stamp_at_all_is_not_asked(tmp_path, landing_repo, row):
    """Scope. A repo with no `changelog.d/` never had the affordance removed, so there is
    nothing here to have come back."""
    harness = tmp_path / "installed"
    _briefs(harness, **{"fix-and-land": "```bash\nscripts/release_stamp.py apply\n```\n"})
    (landing_repo / "scripts" / "release_stamp.py").write_text("# apply a version\n")

    check = getattr(qd, row)(host_for(landing_repo, harness_bin=harness / "bin"))

    assert check.verdict == "ok"
    assert "no changelog.d/" in check.detail


@pytest.mark.parametrize("setup,phrase", [
    ("env", "QB_RELEASE_STAMP names"),
    ("config", "qb.releaseStamp names"),
    ("unreadable", "`qb.releaseStamp`"),
])
def test_a_stamper_that_is_declared_and_absent_is_unknown_not_gone(
        monkeypatch, stamping_repo, setup, phrase):
    """A DECLARED stamper that is not at the path it was declared at is not an absent
    affordance, and a `git config` that could not be read is not a key that is unset —
    the same three-way answer `release_tagger` makes, for the same reason."""
    if setup == "env":
        monkeypatch.setenv("QB_RELEASE_STAMP", str(stamping_repo / "gone.py"))
    elif setup == "config":
        _git(stamping_repo, "config", "qb.releaseStamp", "tools/gone.py")
    else:
        real = qd.run_cmd
        monkeypatch.setattr(qd, "run_cmd", lambda argv, **kw:
                            (128, "", "fatal: bad config line 3")
                            if "qb.releaseStamp" in argv else real(argv, **kw))

    check = qd.check_stamper(host_for(stamping_repo))

    assert check.verdict == "unknown"
    assert phrase in check.detail


# ----------------------------------------------------------------- tags

def _report(**over) -> str:
    """A tag reconciliation in the shape `release_tag.py check --json` prints one."""
    return json.dumps({"clean": True, "ref": "origin/main", "orphaned": {}, "reserved": {},
                       "misplaced": {}, "untagged": [], **over})


def _tagger(repo: Path, body: str) -> None:
    """A tagger the row is allowed to run: it declares a `check` subcommand, so the parse
    of the file says the thing being executed is the thing that was read."""
    (repo / "scripts" / "release_tag.py").write_text(
        "import argparse, json, sys\n"
        "sub = argparse.ArgumentParser().add_subparsers()\n"
        'sub.add_parser("check")\n'
        + body)


def _prints(payload: str, exit_code: int = 0) -> str:
    return f"print({payload!r})\nsys.exit({exit_code})\n"


def test_an_orphaned_release_tag_is_the_finding(landing_repo):
    """#406. `v3.8` was tagged at a commit a squash discarded, so the tag addressed
    history nobody could reach while the entry sat on main — and the guard called `every
    release on main has a tag` stayed green, because a tag of that name resolved."""
    _tagger(landing_repo, _prints(_report(clean=False, orphaned={"v3.8": "abc123"}), 2))

    check = qd.check_tags(host_for(landing_repo))

    assert check.verdict == "fail"
    assert "v3.8" in check.detail
    assert "nothing moves a tag for you" in check.manual


def test_a_reserved_tag_is_listed_and_is_never_a_finding(landing_repo):
    """A tag off the ref is two things wearing one face. Reporting the harmless one would
    train people to ignore the row, which is worse than not having it — so a reservation
    is named in the passing verdict and does not change it."""
    _tagger(landing_repo, _prints(_report(reserved={"v3.14": "def456"})))

    check = qd.check_tags(host_for(landing_repo))

    assert check.verdict == "ok"
    assert "1 tag(s) off" in check.detail and "not a finding" in check.detail


@pytest.mark.parametrize("over,phrase", [
    ({"untagged": ["v3.9"]}, "v3.9 shipped"),
    ({"misplaced": {"v3.9": "abc123"}}, "does not declare it"),
])
def test_the_other_two_ways_the_invariant_is_false_are_findings_too(
        landing_repo, over, phrase):
    """One row, because there is one invariant — a tag `vX.Y` points at a commit whose
    CHANGELOG declares `vX.Y`. `untagged`, `misplaced` and `orphaned` are the three ways
    it can be false, not three questions."""
    _tagger(landing_repo, _prints(_report(clean=False, **over), 2))

    check = qd.check_tags(host_for(landing_repo))

    assert check.verdict == "fail"
    assert phrase in check.detail


def test_clean_tags_are_ok(landing_repo):
    _tagger(landing_repo, _prints(_report()))

    check = qd.check_tags(host_for(landing_repo))

    assert check.verdict == "ok"
    assert "every tag is on the history it names" in check.detail


def test_a_repo_with_no_tagger_has_nothing_to_reconcile(repo):
    check = qd.check_tags(host_for(repo))

    assert check.verdict == "ok"
    assert "no release tagger" in check.detail


def test_a_tagger_without_a_check_subcommand_is_not_run_and_is_unknown(landing_repo):
    """It is run only when the parse of the file says it has a `check` subcommand, so an
    old or foreign tagger is never invoked with a subcommand it does not have. That is a
    compatibility gate and not a sandbox, and the docstring says so."""
    (landing_repo / "scripts" / "release_tag.py").write_text(
        'sub.add_parser("backfill")\nraise SystemExit("this must never run")\n')

    check = qd.check_tags(host_for(landing_repo))

    assert check.verdict == "unknown"
    assert "does not declare a `check` subcommand" in check.detail


def test_a_checkout_whose_integration_ref_is_unknown_does_not_answer_about_head(repo):
    """NOT a fallback. The question is about the ref work lands on, and asking it of
    whatever branch this worktree happens to be on would answer a different question in
    the same words — a feature branch could take a clean tag verdict that says nothing
    about landing."""
    _git(repo, "remote", "add", "origin", "git@github.com:acme/thing.git")
    (repo / "scripts").mkdir()
    _tagger(repo, _prints(_report()))

    check = qd.check_tags(host_for(repo))

    assert check.verdict == "unknown"
    assert "default branch" in check.detail


@pytest.mark.parametrize("body,phrase", [
    ('sys.stderr.write("limited: no remote\\n")\nsys.exit(1)\n', "exited 1"),
    ('print("clean, probably")\n', "did not answer a report"),
    (_prints("{}"), "no usable `orphaned`"),
    (_prints('{"clean": false}'), "no usable `orphaned`"),
    (_prints(json.dumps({"clean": True, "orphaned": {}, "reserved": {}, "misplaced": {},
                         "untagged": "none"})), "no usable `untagged`"),
    (_prints(_report(), 2), "disagree"),
    (_prints(_report(clean=False, orphaned={"v3.8": "abc"}), 0), "disagree"),
    (_prints(_report(), 7), "exited 7"),
])
def test_a_report_this_cannot_use_is_unknown_rather_than_clean(landing_repo, body, phrase):
    """Nothing is inferred from what is missing, and Codex is why: every absent field
    defaulted to an empty collection, so `{}`, `{"clean": false}` and any unrelated
    program that happened to print JSON all came out `ok`. Every key must be present with
    the right container type, the exit code must be one of the two this question has, and
    the two must agree — a `0` alongside findings is a program whose answer this cannot
    use, whichever half were believed."""
    _tagger(landing_repo, body)

    check = qd.check_tags(host_for(landing_repo))

    assert check.verdict == "unknown"
    assert phrase in check.detail


def test_the_real_tagger_answers_the_argv_this_row_sends_it(landing_repo):
    """Against the file this repo actually ships, not a stub that prints what the row
    wants. A stub proves the consumer's happy path; only the real tool proves the two
    halves of the contract — the subcommand, the flags, the exit codes and the field
    names — are still the same ones."""
    real = HARNESS.parent / "scripts" / "release_tag.py"
    assert real.is_file()
    (landing_repo / "scripts" / "release_tag.py").write_text(real.read_text())
    (landing_repo / "scripts" / "release.py").write_text(
        (HARNESS.parent / "scripts" / "release.py").read_text())
    (landing_repo / "CHANGELOG.md").write_text("# Changelog\n\n## v1.0 — the first one\n")
    _git(landing_repo, "add", "-A")
    _git(landing_repo, "-c", "core.hooksPath=/nonexistent", "commit", "-qm", "tagger")
    _git(landing_repo, "update-ref", "refs/remotes/origin/main", "HEAD")

    untagged = qd.check_tags(host_for(landing_repo))
    assert untagged.verdict == "fail", untagged.detail
    assert "v1 shipped on origin/main with no tag" in untagged.detail

    _git(landing_repo, "tag", "-a", "v1", "-m", "v1", "HEAD")
    tagged = qd.check_tags(host_for(landing_repo))

    assert tagged.verdict == "ok", tagged.detail


# ----------------------------------------------------------------- the group

def test_the_landing_group_holds_every_row_that_answers_can_work_land():
    names = {n for n, g, _fn in qd.CHECKS if g == "landing"}

    assert names == {"merges", "queue", "landed", "tags", "generated", "stamper", "briefs"}


def test_no_landing_row_goes_green_on_a_host_that_can_see_nothing(monkeypatch, tmp_path,
                                                                  landing_host):
    """The constraint the whole group is bound by, asserted OVER THE GROUP — because the
    failure it guards against is a row added later without it, and Codex was right that a
    hardcoded list of four names does not guard that at all.

    The repo here is in scope for every row: a GitHub remote, a tagger that reserves,
    `changelog.d/`, a declared stamper, a harness. What it does not have is anything that
    can answer — no board, no token, no `gh`, a tagger whose command set cannot be
    enumerated, a harness with no briefs, and a stamper declared at a path that is not
    there. A landing group that went green in that state would be a worse version of the
    problem it exists to solve.
    """
    (landing_host.repo / "scripts" / "release_tag.py").write_text("import fire\n")
    monkeypatch.setenv("QB_RELEASE_STAMP", str(landing_host.repo / "gone.py"))
    blind = qd.Host(**{**landing_host.__dict__, "base_url": None, "token": None,
                       "harness_bin": tmp_path / "empty" / "bin",
                       "source_harness": tmp_path / "empty", "gh_cache": {}})
    _gh_answers(monkeypatch, {}, have_gh=False)

    rows = [(name, fn) for name, group, fn in qd.CHECKS if group == "landing"]
    assert len(rows) >= 6, "this asserts over the group, so the group has to be read"
    for name, fn in rows:
        check = fn(blind)
        assert check.verdict == "unknown", (
            f"{name} answered {check.verdict} on a host that could see nothing: "
            f"{check.detail}")
        assert check.detail, f"{name} said unknown without saying why"


# --------------------------------------------------------------------------- #
# --announce: the caller #405 was missing (#274's door)
# --------------------------------------------------------------------------- #

class _FakeNeedsHuman:
    """A stand-in for `harness/loops/needs_human.py` that records what it was told.

    The point under test is that the escalation goes through #274's door and not
    through a second spelling of it, so the test asserts on the CALL rather than on
    a request body — a hand-rolled `POST /post` here would pass a test written the
    other way round while putting the fleet's escalations somewhere nobody watches.
    """

    NEEDS_HUMAN_CLASSES = ("decision", "taste", "ui", "environment", "auth", "other")

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def digest(self, *parts: str) -> str:
        return "|".join(parts)

    def announce(self, **kw) -> str:
        self.calls.append(kw)
        return f"needs-human announced as post {len(self.calls)}"


@pytest.fixture
def door(monkeypatch) -> _FakeNeedsHuman:
    fake = _FakeNeedsHuman()
    monkeypatch.setitem(sys.modules, "needs_human", fake)
    return fake


def _rows(*specs: tuple[str, str]) -> list[qd.Check]:
    return [qd.Check(name, "acme/repo@main", f"{name} says so", verdict)
            for name, verdict in specs]


def test_only_a_failing_row_is_announced(door, landing_host):
    """`unknown` is a check that could not be made, and its ordinary causes — no
    network, no `gh`, no token — hold for hours. On a timer that would announce
    forever, so the unattended door carries established findings only. A person
    typing `qb-doctor` still sees every unknown in the report."""
    checks = _rows(("queue", "fail"), ("landed", "unknown"), ("tags", "ok"),
                   ("merges", "warn"))

    said = qd.announce_failures(checks, landing_host, str(BIN / "qb-doctor"))

    assert [c["summary"].split(":")[0] for c in door.calls] == ["queue"]
    assert len(said) == 1


def test_the_escalation_carries_the_class_the_reason_and_the_row(door, landing_host):
    check = qd.Check("queue", "acme/repo@main",
                     "7 queued on main and NONE ready — the oldest has waited 3h 10m",
                     "fail", manual="ask zeus/opal-vermeil, who holds the head (PR #401)",
                     extra={"queued": 7, "ready": 0, "head_pr": 401})
    check.group = "landing"

    qd.announce_failures([check], landing_host, str(BIN / "qb-doctor"))

    (call,) = door.calls
    assert call["cls"] in _FakeNeedsHuman.NEEDS_HUMAN_CLASSES
    assert "NONE ready" in call["summary"]
    # The reason is the remedy, because #274 refuses an escalation that carries none.
    assert "zeus/opal-vermeil" in call["reason"]
    assert "head_pr" in call["detail"]
    assert {"kind": "pr", "value": "401", "repo": call["repo"]} in call["refs"]
    assert call["repo"], "the escalation names the repository it is about"


def test_a_row_whose_courier_raises_does_not_take_the_rest_of_the_report_down(
        monkeypatch, door, landing_host):
    """`announce` documents that it never raises, and this does not take its word for
    it. The harness on PATH goes stale — there is a row about exactly that — so the
    copy imported here can be older than the contract, and this runs on a timer where
    an escape would silence every row behind it."""
    def explode(**kw):
        if "queue" in kw["summary"]:
            raise RuntimeError("the board's client is from another century")
        return "needs-human announced"

    monkeypatch.setattr(door, "announce", explode)

    said = qd.announce_failures(_rows(("queue", "fail"), ("tags", "fail")),
                                landing_host, str(BIN / "qb-doctor"))

    assert any("NOT announced" in line and "RuntimeError" in line for line in said)
    assert any(line == "needs-human announced" for line in said), (
        "the row behind the failing courier still reached the board")


def test_a_row_with_no_head_pr_still_distinguishes_its_own_occurrences(door, landing_host):
    """The dedupe key is built from what a row says identifies its fault, not from one
    field the queue row happens to carry. `landed` names the pull requests that are
    ready and cannot land; two different sets of them are two different findings, and a
    key that only knew about `head_pr` would suppress the second for twelve hours."""
    first = qd.Check("landed", "acme/repo", "2 ready, main is 4h old", "fail",
                     extra={"open": 6, "ready": [401, 403], "tip_age_minutes": 240})
    second = qd.Check("landed", "acme/repo", "2 ready, main is 5h old", "fail",
                      extra={"open": 7, "ready": [418, 419], "tip_age_minutes": 300})

    qd.announce_failures([first, second], landing_host, str(BIN / "qb-doctor"))

    assert len({c["key"] for c in door.calls}) == 2


def test_the_same_fault_ticking_on_a_timer_keeps_one_key(door, landing_host):
    """The other direction, and the one that decides whether a timer is usable. A stall
    reported at 40 minutes and again at 55 is one fault, so how long it has run and how
    many are behind it are kept out of the key — otherwise every tick is a new question
    and #274's dedupe protects nobody."""
    at_40 = qd.Check("queue", "acme/repo@main", "7 queued, none ready", "fail",
                     extra={"queued": 7, "ready": 0, "waited_minutes": 40, "head_pr": 401})
    at_55 = qd.Check("queue", "acme/repo@main", "7 queued, none ready", "fail",
                     extra={"queued": 9, "ready": 0, "waited_minutes": 55, "head_pr": 401})

    qd.announce_failures([at_40, at_55], landing_host, str(BIN / "qb-doctor"))

    assert len({c["key"] for c in door.calls}) == 1


def test_a_second_stall_behind_a_different_head_is_not_swallowed_by_the_first(
        door, landing_host):
    """#274 dedupes on a key for twelve hours, which is what keeps a timer quiet. The
    key therefore has to carry what makes THIS stall this one — otherwise the second
    time the line jams, behind a different PR, the fleet's protection from noise
    hides the news."""
    first = qd.Check("queue", "acme/repo@main", "7 queued, none ready", "fail",
                     extra={"head_pr": 401})
    second = qd.Check("queue", "acme/repo@main", "4 queued, none ready", "fail",
                      extra={"head_pr": 419})

    qd.announce_failures([first, second], landing_host, str(BIN / "qb-doctor"))

    keys = [c["key"] for c in door.calls]
    assert len(set(keys)) == 2, "two different heads are two different questions"
    assert all("qb-doctor" in k and "queue" in k for k in keys)


def test_an_unreachable_door_is_reported_and_never_raised(monkeypatch, landing_host):
    """An escalation must survive its own courier: the finding stands whether or not
    the board took the post, and a doctor that crashed on a timer would be silent in
    exactly the way #405 is about."""
    monkeypatch.setattr(qd, "loops_dir", lambda _script: None)

    said = qd.announce_failures(_rows(("queue", "fail")), landing_host, "/nowhere/qb-doctor")

    assert said and "NOT announced" in said[0]


def test_announcing_changes_neither_the_report_nor_the_exit_code(
        monkeypatch, door, landing_host, capsys):
    """`--json` stays a document. A caller piping this into `jq` must not find a
    courier's progress note spliced into it, and the exit code is the report's."""
    monkeypatch.setattr(qd, "survey", lambda *_a, **_k: landing_host)
    monkeypatch.setattr(qd, "run_checks", lambda *_a, **_k: _rows(("queue", "fail")))

    code = qd.main(["--only", "queue", "--announce", "--json"])

    out = capsys.readouterr()
    assert code == 2
    assert json.loads(out.out)["checks"][0]["name"] == "queue"
    assert "needs-human announced" in out.err
    assert len(door.calls) == 1
