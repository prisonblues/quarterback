"""`qb-bump`, tested against the four ways an automated bump can do harm.

1. **Propose something that has not been built.** The bump on 2026-08-22 failed on a
   `home.file` collision, and a proposal that has not been built is a proposal to
   break somebody's machine — so a failing build must REFUSE and carry its error.
2. **Sweep somebody's uncommitted work into what it builds.** The consuming flake is
   a person's repository with a half-edited secrets module in it; preparation reads
   `git archive HEAD` and must be blind to the working tree.
3. **Switch a machine.** `--apply` is a person's verb. Without a terminal it must
   change nothing, whatever else is true.
4. **Answer a question it was not asked.** The drift verdict is `qb-doctor`'s (#204);
   a second comparison here would be a second opinion about a fact that has one. The
   test for that is behavioural: a stub doctor reporting `ok` has to be believed.

Everything runs with no network, no nix and no board: `nix` and the escalation are
monkeypatched, and the repositories are real ones built in `tmp_path`. That is
deliberate rather than convenient — this suite runs inside flake.nix's
`worktree-tests` sandbox, which has git, no nix and no network at all, so a test
that needed one would SKIP there and be green about nothing.

Run: pytest harness/tests/test_qb_bump.py
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parent.parent / "bin"
LOOPS = Path(__file__).resolve().parent.parent / "loops"


def _load(name: str, filename: str):
    """A harness script has no `.py`, so it is loaded by path — registered in
    `sys.modules` before execution, because `@dataclass` resolves its own module
    through `sys.modules[cls.__module__]` (the same reason `test_qb_doctor.py` does)."""
    loader = importlib.machinery.SourceFileLoader(name, str(BIN / filename))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    loader.exec_module(module)
    return module


qb = _load("qb_bump", "qb-bump")


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #

@pytest.fixture(autouse=True)
def _hermetic(monkeypatch, tmp_path):
    """No global git config, no site config and no real cache dir reaches these tests.

    The cache one is not tidiness: `save()` and `load()` write a proposal to
    `$XDG_CACHE_HOME/quarterback/harness-bump`, and a suite that used the real one
    would overwrite the proposal a person on this machine is about to apply.
    """
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(tmp_path / "gitconfig-system"))
    monkeypatch.setenv("QUARTERBACK_CONFIG", str(tmp_path / "no-such-config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setattr(qb, "CACHE", tmp_path / "cache" / "quarterback" / "harness-bump")
    for var in (qb.FLAKE_KEY, qb.ATTR_KEY, "QUARTERBACK_CONSUMER_ROOTS"):
        monkeypatch.delenv(var, raising=False)


def _git(repo: Path, *args: str) -> str:
    out = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                         check=True)
    return out.stdout.strip()


def _lock(rev: str, owner: str = "prisonblues", repo: str = "quarterback",
          name: str = "quarterback") -> str:
    return json.dumps({"nodes": {name: {"locked": {
        "type": "github", "owner": owner, "repo": repo, "rev": rev}}}, "root": "root"})


@pytest.fixture
def consumer_repo(tmp_path) -> Path:
    """A real git repository that pins this repo, with a commit in it."""
    repo = tmp_path / "nix-fleet"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "flake.lock").write_text(_lock("a" * 40))
    (repo / "flake.nix").write_text("{ outputs = _: { }; }\n")
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "-c", "commit.gpgsign=false",
         "commit", "-q", "-m", "base")
    return repo


@pytest.fixture
def quarterback_repo(tmp_path) -> Path:
    """A checkout whose origin names `prisonblues/quarterback` — the slug a lock carries."""
    repo = tmp_path / "quarterback"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "remote", "add", "origin", "https://github.com/prisonblues/quarterback.git")
    return repo


def _stub_doctor(tmp_path: Path, payload: dict) -> str:
    """A `qb-doctor` beside a `qb-bump`, answering `--json` with `payload`.

    Returns the path to pass as `script`, which is how `doctor_path` finds a
    sibling. A stub rather than the real doctor because the subject here is what
    this file does with a verdict, not how that verdict is reached.

    The shebang is this interpreter's absolute path, not `/usr/bin/env python3`:
    there is no `/usr/bin/env` inside a nix build sandbox, and
    `test_runtime_stub_shebangs.py` exists because a stub that cannot exec fails
    for a reason that has nothing to do with the code under test.
    """
    binn = tmp_path / "stubbin"
    binn.mkdir(exist_ok=True)
    (binn / "qb-doctor").write_text(
        f"#!{sys.executable}\n"
        "import json, sys\n"
        f"print(json.dumps({payload!r}))\n")
    (binn / "qb-doctor").chmod(0o755)
    (binn / "qb-bump").write_text("#\n")
    return str(binn / "qb-bump")


STALE = {"checks": [{"name": "harness", "subject": "/nix/store/x/bin", "verdict": "fail",
                     "detail": "behind this checkout: 5 absent (qb-admit, +4 more)",
                     "extra": {"missing": ["qb-admit", "qb-start", "qb-status", "qb-release",
                                           "check-db-isolation"],
                               "differ": ["create-worktree"]}}]}
CURRENT = {"checks": [{"name": "harness", "subject": "/nix/store/x/bin", "verdict": "ok",
                       "detail": "the harness on PATH IS this checkout", "extra": {}}]}


# --------------------------------------------------------------------------- #
# the drift is qb-doctor's answer, and this file does not have a second one
# --------------------------------------------------------------------------- #

def test_the_drift_is_read_from_qb_doctors_report(tmp_path, quarterback_repo):
    drift, why = qb.harness_drift(quarterback_repo, _stub_doctor(tmp_path, STALE))
    assert why == ""
    assert drift.stale and drift.missing == ["qb-admit", "qb-start", "qb-status",
                                             "qb-release", "check-db-isolation"]


def test_a_doctor_that_says_ok_is_believed_even_on_a_stale_box(tmp_path, quarterback_repo,
                                                               capsys, monkeypatch):
    """The point of shelling out. If this file ever grows its own comparison, this test
    is what fails: `qb-doctor` says the harness is current, so there is nothing to carry,
    whatever any other measurement of the same directories might say."""
    monkeypatch.setattr(sys, "argv", ["qb-bump"])
    monkeypatch.setattr(qb, "__file__", _stub_doctor(tmp_path, CURRENT))
    code = qb.main(["--repo", str(quarterback_repo), "--no-announce"])
    assert code == 0
    assert "nothing to carry" in capsys.readouterr().out


def test_no_doctor_at_all_is_unknown_and_says_whose_answer_it_is(tmp_path, quarterback_repo,
                                                                 monkeypatch):
    """`unknown`, never `ok`. #204's own thesis: a check that could not be made must not
    be reported as one that passed — and here that error would switch a machine's worth
    of behaviour onto silence."""
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    drift, why = qb.harness_drift(quarterback_repo, str(tmp_path / "nowhere" / "qb-bump"))
    assert drift is None and "qb-doctor" in why and "#204" in why


def test_a_doctor_that_answers_rubbish_is_unknown(tmp_path, quarterback_repo):
    binn = tmp_path / "b"
    binn.mkdir()
    (binn / "qb-doctor").write_text(f"#!{sys.executable}\nprint('not json')\n")
    (binn / "qb-doctor").chmod(0o755)
    drift, why = qb.harness_drift(quarterback_repo, str(binn / "qb-bump"))
    assert drift is None and "report this can read" in why


# --------------------------------------------------------------------------- #
# finding the consuming flake — and refusing to guess between two
# --------------------------------------------------------------------------- #

def test_a_lock_that_pins_the_repo_is_recognised_by_owner_and_name():
    assert qb.pins_repo(_lock("b" * 40), "prisonblues/quarterback") == ("quarterback",
                                                                       "b" * 40)


def test_a_lock_that_pins_it_by_url_is_recognised_too():
    text = json.dumps({"nodes": {"qb": {"locked": {
        "type": "git", "url": "https://github.com/prisonblues/quarterback.git",
        "rev": "c" * 40}}}})
    assert qb.pins_repo(text, "prisonblues/quarterback") == ("qb", "c" * 40)


def test_a_lock_that_pins_something_else_is_not_a_consumer():
    assert qb.pins_repo(_lock("d" * 40, owner="nixos", repo="nixpkgs"),
                        "prisonblues/quarterback") is None


def test_the_scan_finds_a_consumer_one_level_down(tmp_path, consumer_repo, monkeypatch):
    monkeypatch.setenv("QUARTERBACK_CONSUMER_ROOTS", str(tmp_path))
    consumer, why = qb.resolve_consumer(None, {}, "prisonblues/quarterback")
    assert why == "" and consumer.flake == consumer_repo.resolve()
    assert consumer.input == "quarterback" and consumer.rev == "a" * 40


def test_two_consumers_refuse_rather_than_pick_one(tmp_path, consumer_repo, monkeypatch):
    """The failure this prevents is a proposal against a directory nobody rebuilds from
    — which looks exactly like a good proposal and does nothing at all."""
    other = tmp_path / "nix-fleet-copy"
    other.mkdir()
    (other / "flake.lock").write_text(_lock("e" * 40))
    monkeypatch.setenv("QUARTERBACK_CONSUMER_ROOTS", str(tmp_path))
    consumer, why = qb.resolve_consumer(None, {}, "prisonblues/quarterback")
    assert consumer is None
    assert "2 flakes pin" in why and str(other) in why and str(consumer_repo) in why


def test_nothing_pinning_this_repo_is_unknown_and_names_the_two_ways_to_say_so(tmp_path,
                                                                              monkeypatch):
    monkeypatch.setenv("QUARTERBACK_CONSUMER_ROOTS", str(tmp_path / "empty"))
    consumer, why = qb.resolve_consumer(None, {}, "prisonblues/quarterback")
    assert consumer is None and "--flake" in why and qb.FLAKE_KEY in why


def test_an_explicit_flake_beats_the_scan(tmp_path, consumer_repo, monkeypatch):
    decoy = tmp_path / "decoy"
    decoy.mkdir()
    (decoy / "flake.lock").write_text(_lock("f" * 40))
    monkeypatch.setenv("QUARTERBACK_CONSUMER_ROOTS", str(tmp_path))
    consumer, why = qb.resolve_consumer(str(consumer_repo), {}, "prisonblues/quarterback")
    assert why == "" and consumer.flake == consumer_repo.resolve()
    assert consumer.found_by == "--flake"


def test_a_named_flake_that_pins_nothing_is_refused_not_bumped(tmp_path):
    """A typo'd path is the likely case, and `nix flake update` in a flake that does not
    pin this repo would succeed, build, and propose a change that carries no harness."""
    empty = tmp_path / "unrelated"
    empty.mkdir()
    (empty / "flake.lock").write_text(_lock("0" * 40, owner="nixos", repo="nixpkgs"))
    consumer, why = qb.resolve_consumer(str(empty), {}, "prisonblues/quarterback")
    assert consumer is None and "does not pin" in why


def test_the_site_config_can_declare_the_consumer(tmp_path, consumer_repo, monkeypatch):
    cfg = tmp_path / "config"
    cfg.write_text(f'{qb.FLAKE_KEY}="{consumer_repo}"\n')
    monkeypatch.setenv("QUARTERBACK_CONFIG", str(cfg))
    consumer, why = qb.resolve_consumer(None, qb.site_config(), "prisonblues/quarterback")
    assert why == "" and consumer.flake == consumer_repo.resolve()


def test_the_config_parser_agrees_with_qb_doctors(tmp_path):
    """Second implementation of one parser, held against the first — the guard
    `test_in_flight_drift.py` puts between `qb-admit` and `harness_rules.DEFAULTS`.
    A bin script cannot import a loops module and will not import `qb-doctor` to read
    two keys, so the duplication is deliberate and this is its price."""
    doctor = _load("qb_doctor_for_bump", "qb-doctor")
    cfg = tmp_path / "config"
    cfg.write_text(
        "# a comment\n"
        "QUARTERBACK_BASE_URL='https://qb.example/x?a=1&b=2'\n"
        'export QUARTERBACK_REPO="$HOME/source/quarterback"\n'
        f"{qb.FLAKE_KEY}=/home/someone/nix-fleet\n"
        "not a config line\n")
    os.environ["QUARTERBACK_CONFIG"] = str(cfg)
    try:
        assert qb.site_config(cfg) == doctor.load_site_config()
    finally:
        del os.environ["QUARTERBACK_CONFIG"]


# --------------------------------------------------------------------------- #
# which nixosConfiguration is this machine
# --------------------------------------------------------------------------- #

def test_the_attribute_is_matched_by_hostname_not_assumed_to_be_it(monkeypatch, tmp_path):
    """This fleet's `zeus` is `nixosConfigurations.desktop`. Assuming the attribute is the
    hostname fails on the machine this was written for, and assuming the wrong one hands a
    person a command that switches their desktop onto a laptop's configuration."""
    monkeypatch.setattr(qb.socket, "gethostname", lambda: "zeus.local")

    def fake_nix(*args, **kw):
        if args[0] == "eval" and "--apply" in args:
            return 0, json.dumps(["daedalus", "desktop", "hermes"]), ""
        attr = args[1].split("#nixosConfigurations.")[1].split(".")[0]
        return 0, {"desktop": "zeus", "hermes": "hermes", "daedalus": "daedalus"}[attr], ""

    monkeypatch.setattr(qb, "nix", fake_nix)
    assert qb.resolve_attr(tmp_path, None, {}) == ("desktop", "")


def test_an_attribute_named_after_this_host_is_used_without_evaluating_anything(monkeypatch,
                                                                               tmp_path):
    monkeypatch.setattr(qb.socket, "gethostname", lambda: "hermes")
    calls: list = []

    def fake_nix(*args, **kw):
        calls.append(args)
        return 0, json.dumps(["desktop", "hermes"]), ""

    monkeypatch.setattr(qb, "nix", fake_nix)
    assert qb.resolve_attr(tmp_path, None, {}) == ("hermes", "")
    assert len(calls) == 1, "the cheap name lookup should be the only evaluation"


def test_no_configuration_claiming_this_hostname_refuses_and_says_how_to_say_so(monkeypatch,
                                                                               tmp_path):
    monkeypatch.setattr(qb.socket, "gethostname", lambda: "atlas")
    monkeypatch.setattr(qb, "nix", lambda *a, **k: (
        (0, json.dumps(["desktop"]), "") if "--apply" in a else (0, "zeus", "")))
    attr, why = qb.resolve_attr(tmp_path, None, {})
    assert attr == "" and "--host" in why and "atlas" in why


def test_two_configurations_claiming_this_hostname_refuse(monkeypatch, tmp_path):
    monkeypatch.setattr(qb.socket, "gethostname", lambda: "zeus")
    monkeypatch.setattr(qb, "nix", lambda *a, **k: (
        (0, json.dumps(["desktop", "desktop-vm"]), "") if "--apply" in a else (0, "zeus", "")))
    attr, why = qb.resolve_attr(tmp_path, None, {})
    assert attr == "" and "2 of" in why and "--host" in why


def test_an_explicit_host_is_taken_without_asking_nix(monkeypatch, tmp_path):
    monkeypatch.setattr(qb, "nix", lambda *a, **k: pytest.fail("nix was consulted"))
    assert qb.resolve_attr(tmp_path, "desktop", {}) == ("desktop", "")


# --------------------------------------------------------------------------- #
# preparation reads HEAD, and only HEAD
# --------------------------------------------------------------------------- #

def test_the_export_is_head_and_never_the_working_tree(consumer_repo, tmp_path):
    """The consuming flake is somebody's repository with work in progress in it — on
    2026-08-22 an uncommitted `modules/op-secrets.nix`. A preparation that read the
    working tree would build a tree nobody wrote and could sweep it into a proposal."""
    (consumer_repo / "flake.nix").write_text("{ outputs = _: { BROKEN }; }\n")
    (consumer_repo / "untracked-secret.nix").write_text("secret\n")
    into = tmp_path / "export"
    into.mkdir()
    assert qb.export_head(consumer_repo, into) == ""
    assert (into / "flake.nix").read_text() == "{ outputs = _: { }; }\n"
    assert not (into / "untracked-secret.nix").exists()


def test_a_directory_that_is_not_a_repository_is_a_reason_not_a_traceback(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    assert "git archive failed" in qb.export_head(plain, tmp_path / "out")


def _fake_nix(new_rev: str, build_rc: int = 0, build_err: str = ""):
    """A `nix` that updates a lock the way the real one does, and builds (or does not)."""
    def fake(*args, **kw):
        if args[0] == "flake":
            tmp = Path(args[args.index("--flake") + 1].removeprefix("path:"))
            (tmp / "flake.lock").write_text(_lock(new_rev))
            return 0, "", ""
        if args[0] == "build":
            if build_rc:
                return build_rc, "", build_err
            return 0, "/nix/store/deadbeef-nixos-system-zeus", ""
        raise AssertionError(f"unexpected nix call {args}")
    return fake


def test_a_prepared_bump_records_what_was_built_and_from_what(consumer_repo, monkeypatch):
    monkeypatch.setattr(qb, "have_nix", lambda: True)
    monkeypatch.setattr(qb, "nix", _fake_nix("b" * 40))
    consumer = qb.Consumer(flake=consumer_repo, input="quarterback", rev="a" * 40,
                           found_by="--flake")
    drift = qb.Drift(verdict="fail", detail="behind", missing=["qb-admit"], differ=[])
    proposal, log, why = qb.prepare(consumer, "desktop", str(BIN / "qb-bump"), drift)
    assert why == "" and proposal is not None
    assert proposal.old_rev == "a" * 40 and proposal.new_rev == "b" * 40
    assert proposal.out_path == "/nix/store/deadbeef-nixos-system-zeus"
    assert proposal.attr == "desktop" and proposal.moved
    assert qb.load().new_rev == "b" * 40, "the proposal must survive for the person"
    assert (qb.CACHE / "flake.lock").read_text() == _lock("b" * 40)


def test_the_consumers_working_tree_is_untouched_by_a_preparation(consumer_repo, monkeypatch):
    """Preparation happens in a temporary directory. Nothing is written into the
    consumer until a person runs `--apply`, and this is what says so."""
    before = (consumer_repo / "flake.lock").read_text()
    monkeypatch.setattr(qb, "have_nix", lambda: True)
    monkeypatch.setattr(qb, "nix", _fake_nix("b" * 40))
    qb.prepare(qb.Consumer(consumer_repo, "quarterback", "a" * 40, "--flake"), "desktop",
               str(BIN / "qb-bump"), qb.Drift("fail", "behind"))
    assert (consumer_repo / "flake.lock").read_text() == before
    assert _git(consumer_repo, "status", "--porcelain") == ""


def test_a_bump_that_does_not_build_is_refused_and_carries_its_error(consumer_repo,
                                                                    monkeypatch):
    """2026-08-22's collision, in the shape it arrived in: two definitions of one
    `home.file` path is an eval error, and the whole value of the refusal is the error."""
    collision = ("error: The option `home.file..claude/quarterback-workflow.md.source' "
                 "has conflicting definition values")
    monkeypatch.setattr(qb, "have_nix", lambda: True)
    monkeypatch.setattr(qb, "nix", _fake_nix("b" * 40, build_rc=1, build_err=collision))
    proposal, log, why = qb.prepare(
        qb.Consumer(consumer_repo, "quarterback", "a" * 40, "--flake"), "desktop",
        str(BIN / "qb-bump"), qb.Drift("fail", "behind"))
    assert proposal is None
    assert "does not build" in why and "not proposing it" in why
    assert "conflicting definition values" in log
    assert qb.load() is None, "a refused bump must not be left where --apply can find it"


def test_a_refusal_exits_four_and_prints_the_error(consumer_repo, quarterback_repo,
                                                   monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("QUARTERBACK_CONSUMER_ROOTS", str(consumer_repo.parent))
    monkeypatch.setattr(qb, "have_nix", lambda: True)
    monkeypatch.setattr(qb, "nix", _fake_nix("b" * 40, build_rc=1,
                                             build_err="error: conflicting definition"))
    monkeypatch.setattr(qb, "harness_drift",
                        lambda *a: (qb.Drift("fail", "behind", missing=["qb-admit"]), ""))
    monkeypatch.setattr(qb, "resolve_attr", lambda *a: ("desktop", ""))
    code = qb.main(["--repo", str(quarterback_repo), "--no-announce"])
    assert code == 4
    assert "conflicting definition" in capsys.readouterr().err


def test_a_prepared_bump_exits_two_and_names_the_one_command(consumer_repo, quarterback_repo,
                                                             monkeypatch, capsys):
    monkeypatch.setenv("QUARTERBACK_CONSUMER_ROOTS", str(consumer_repo.parent))
    monkeypatch.setattr(qb, "have_nix", lambda: True)
    monkeypatch.setattr(qb, "nix", _fake_nix("b" * 40))
    monkeypatch.setattr(qb, "harness_drift",
                        lambda *a: (qb.Drift("fail", "behind: 1 absent (qb-admit)",
                                             missing=["qb-admit"]), ""))
    monkeypatch.setattr(qb, "resolve_attr", lambda *a: ("desktop", ""))
    code = qb.main(["--repo", str(quarterback_repo), "--no-announce"])
    out = capsys.readouterr().out
    assert code == 2
    assert "--apply" in out and str(consumer_repo) in out
    assert "needs a password" in out, "the ceiling has to be stated where it is reached"


def test_nothing_in_the_agent_path_ever_runs_nixos_rebuild(consumer_repo, quarterback_repo,
                                                           monkeypatch):
    """The design ceiling, asserted rather than described. Preparation shells out to git
    and nix; `sudo` and `nixos-rebuild` belong to `--apply`, which is a person's verb."""
    monkeypatch.setenv("QUARTERBACK_CONSUMER_ROOTS", str(consumer_repo.parent))
    monkeypatch.setattr(qb, "have_nix", lambda: True)
    monkeypatch.setattr(qb, "nix", _fake_nix("b" * 40))
    monkeypatch.setattr(qb, "harness_drift", lambda *a: (qb.Drift("fail", "behind"), ""))
    monkeypatch.setattr(qb, "resolve_attr", lambda *a: ("desktop", ""))
    monkeypatch.setattr(qb.os, "execvp",
                        lambda *a: pytest.fail("the preparation path exec'd something"))
    seen: list = []
    real_run = qb.run
    monkeypatch.setattr(qb, "run", lambda argv, **kw: (seen.append(argv[0]),
                                                       real_run(argv, **kw))[1])
    assert qb.main(["--repo", str(quarterback_repo), "--no-announce"]) == 2
    assert "sudo" not in seen and "nixos-rebuild" not in seen


# --------------------------------------------------------------------------- #
# --apply — a person's verb
# --------------------------------------------------------------------------- #

@pytest.fixture
def prepared(consumer_repo, monkeypatch) -> "qb.Proposal":
    monkeypatch.setattr(qb, "have_nix", lambda: True)
    monkeypatch.setattr(qb, "nix", _fake_nix("b" * 40))
    proposal, _, _ = qb.prepare(qb.Consumer(consumer_repo, "quarterback", "a" * 40, "--flake"),
                                "desktop", str(BIN / "qb-bump"), qb.Drift("fail", "behind"))
    return proposal


def test_apply_without_a_terminal_changes_nothing(prepared, consumer_repo, monkeypatch,
                                                  capsys):
    """The refusal that matters most. An agent invoking `--apply` — or a timer, or a CI
    job — must not switch a live machine, whether or not somebody's sudo is still warm."""
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(qb.os, "execvp", lambda *a: pytest.fail("switched without a tty"))
    assert qb.apply(prepared, dry_run=False) == 3
    assert (consumer_repo / "flake.lock").read_text() == _lock("a" * 40)
    assert "needs a terminal" in capsys.readouterr().err


def test_apply_with_a_terminal_writes_only_the_lock_and_then_execs_sudo(prepared,
                                                                       consumer_repo,
                                                                       monkeypatch):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    execd: list = []
    monkeypatch.setattr(qb.os, "execvp", lambda f, argv: execd.append(argv))
    assert qb.apply(prepared, dry_run=False) == 0
    assert (consumer_repo / "flake.lock").read_text() == _lock("b" * 40)
    assert execd == [["sudo", "nixos-rebuild", "switch", "--flake",
                      f"{consumer_repo}#desktop"]]
    assert _git(consumer_repo, "status", "--porcelain").split() == ["M", "flake.lock"], \
        "the lock is left MODIFIED, not committed — committing it is the consumer's"


def test_apply_dry_run_prints_the_command_and_writes_nothing(prepared, consumer_repo,
                                                             monkeypatch, capsys):
    monkeypatch.setattr(qb.os, "execvp", lambda *a: pytest.fail("dry run switched"))
    assert qb.apply(prepared, dry_run=True) == 0
    assert (consumer_repo / "flake.lock").read_text() == _lock("a" * 40)
    assert "sudo nixos-rebuild switch --flake" in capsys.readouterr().out


def test_apply_refuses_when_the_lock_moved_since_the_bump_was_built(prepared, consumer_repo,
                                                                   monkeypatch, capsys):
    """The build that was proven is a build of a tree that no longer exists. Re-preparing
    costs minutes; switching onto something nobody built costs a machine."""
    (consumer_repo / "flake.lock").write_text(_lock("f" * 40))
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(qb.os, "execvp", lambda *a: pytest.fail("switched onto a stale build"))
    assert qb.apply(prepared, dry_run=False) == 3
    assert "has changed since this bump was prepared" in capsys.readouterr().err


def test_apply_is_idempotent_when_the_lock_is_already_the_prepared_one(prepared,
                                                                      consumer_repo,
                                                                      monkeypatch):
    """A switch that failed halfway leaves the lock applied; running the command again is
    the obvious response and must not be read as "the tree moved"."""
    (consumer_repo / "flake.lock").write_text(_lock("b" * 40))
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    execd: list = []
    monkeypatch.setattr(qb.os, "execvp", lambda f, argv: execd.append(argv))
    assert qb.apply(prepared, dry_run=False) == 0
    assert len(execd) == 1


def test_apply_with_nothing_prepared_says_so(capsys):
    assert qb.apply(None, dry_run=False) == 3
    assert "no prepared bump" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# the escalation, and the bootstrap
# --------------------------------------------------------------------------- #

def test_the_escalation_class_is_one_the_vocabulary_defines():
    """#279's vocabulary, imported rather than trusted. A class this file spells wrong is
    announced as `other` and the board loses the one word that says what kind of help is
    needed."""
    sys.path.insert(0, str(LOOPS))
    import needs_human  # noqa: PLC0415 — the module under comparison
    assert qb.NEEDS_HUMAN_CLASS in needs_human.NEEDS_HUMAN_CLASSES


def test_the_escalation_goes_through_the_one_door(monkeypatch, tmp_path):
    """`needs_human.announce` and not a board post written here — #274 landed one door for
    "a human must do this", and a second spelling of it is two places to watch."""
    monkeypatch.setenv("QB_LOOPS_DIR", str(LOOPS))
    sys.path.insert(0, str(LOOPS))
    import needs_human  # noqa: PLC0415
    seen: dict = {}
    monkeypatch.setattr(needs_human, "announce",
                        lambda **kw: seen.update(kw) or "announced as post 1")
    said = qb.escalate(str(BIN / "qb-bump"), summary="s", reason="r", detail="d",
                       repo="prisonblues/quarterback", key_parts=["a", "b"])
    assert said == "announced as post 1"
    assert seen["cls"] == "environment" and seen["reason"] == "r"
    assert {"kind": "issue", "value": "267", "repo": "prisonblues/quarterback"} in seen["refs"]


def test_an_escalation_that_cannot_be_announced_is_still_reported(monkeypatch, tmp_path):
    """Never an exception and never silence: an escalation that cannot reach the board is
    still an escalation, and the printed report is what a person reads either way."""
    monkeypatch.setenv("QB_LOOPS_DIR", str(tmp_path / "nowhere"))
    said = qb.escalate(str(tmp_path / "elsewhere" / "qb-bump"), summary="s", reason="r",
                       detail="d", repo="r/r", key_parts=["a"])
    assert "NOT announced" in said


def test_the_command_it_tells_a_person_to_type_is_one_that_exists(monkeypatch, tmp_path):
    """The bootstrap, and it is the normal case rather than the corner: the harness
    carrying `qb-bump` is by definition the one NOT installed on the host it is
    diagnosing, so `qb-bump --apply` would be a `command not found` on first run."""
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    assert qb.invocation(str(BIN / "qb-bump")) == str(BIN / "qb-bump")


def test_an_installed_qb_bump_is_named_rather_than_pathed(monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", str(BIN))
    assert qb.invocation(str(BIN / "qb-bump")) == "qb-bump"


# --------------------------------------------------------------------------- #
# the consumer declares itself — the option a fleet sets once
# --------------------------------------------------------------------------- #

def test_the_module_declares_the_consumer_options():
    """`qb-doctor` records that it cannot find the consuming flake, which is why its
    comparison is a content proxy. This is the door that answers it: a consumer says
    where it is, once, in the config file every board client already reads."""
    text = (Path(__file__).resolve().parent.parent / "hm-module.nix").read_text()
    assert "consumer = {" in text, "no `consumer` block in hm-module.nix"
    for leaf in ("flake", "attr"):
        assert f"{leaf} = lib.mkOption {{" in text.replace("\n", "\n"), \
            f"no `consumer.{leaf}` option in hm-module.nix"
    for var in (qb.FLAKE_KEY, qb.ATTR_KEY):
        assert var in text, f"{var} is never emitted into the site config"


def test_an_uncommitted_lock_in_the_consumer_stops_the_preparation(consumer_repo,
                                                                   quarterback_repo,
                                                                   monkeypatch, capsys):
    """The consumer was part-way through a nixpkgs bump of their own. Preparation builds
    HEAD plus one moved input, and `--apply` writes that over the file in the working
    tree — so carrying on would discard their change, and reading it would build
    uncommitted work. Neither: it stops and names the file."""
    (consumer_repo / "flake.lock").write_text(_lock("9" * 40))
    monkeypatch.setenv("QUARTERBACK_CONSUMER_ROOTS", str(consumer_repo.parent))
    monkeypatch.setattr(qb, "have_nix", lambda: True)
    monkeypatch.setattr(qb, "nix", lambda *a, **k: pytest.fail("prepared over a dirty lock"))
    monkeypatch.setattr(qb, "harness_drift", lambda *a: (qb.Drift("fail", "behind"), ""))
    assert qb.main(["--repo", str(quarterback_repo), "--no-announce"]) == 1
    assert "uncommitted changes" in capsys.readouterr().err


def test_a_committed_lock_is_not_mistaken_for_a_dirty_one(consumer_repo):
    assert qb.lock_is_committed(consumer_repo) == ""


def test_a_host_with_no_nix_says_that_rather_than_something_downstream(quarterback_repo,
                                                                      monkeypatch, capsys):
    monkeypatch.setattr(qb, "have_nix", lambda: False)
    monkeypatch.setattr(qb, "harness_drift", lambda *a: (qb.Drift("fail", "behind"), ""))
    assert qb.main(["--repo", str(quarterback_repo), "--no-announce"]) == 1
    assert "no nix on this host" in capsys.readouterr().err
