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
import subprocess
import sys
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

    monkeypatch.setattr(qd, "CHECKS", (("boom", boom), ("fine", lambda _h: qd.Check(
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
    `CHECKS` cannot be selected and a name in the help with no check cannot run."""
    names = [n for n, _ in qd.CHECKS]
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
