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

5. **Destroy uncommitted work while catching a tree up.** The pull is `fetch` plus
   `merge --ff-only` and must never become `rebase`, `reset` or `stash`. A diverged
   branch and a half-written file both have to come out the other side untouched.
6. **Switch onto a system nobody built.** The `rebuild` wrapper is used only when it can
   be SHOWN to target the flake that was just built — and it is read to find that out,
   never run.

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
import shutil
import subprocess
import sys
import time
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
    for var in (qb.FLAKE_KEY, qb.ATTR_KEY, qb.REBUILD_KEY, "QUARTERBACK_REPO"):
        monkeypatch.delenv(var, raising=False)
    # The consumer scan runs on every path now, the no-op included, and its default
    # roots are `~/source` and `~` — the developer's REAL ones. Left unset, this suite
    # found the actual nix-fleet on this machine and tried to `git fetch` it. Pointed
    # at tmp_path, which contains only what a test put there.
    monkeypatch.setenv("QUARTERBACK_CONSUMER_ROOTS", str(tmp_path / "no-consumers-here"))
    # The host running the suite has a real `rebuild` on PATH, and whether it is picked
    # up would otherwise depend on which machine ran the tests. Stubbed to "there isn't
    # one" so the fallback is the default everywhere; the wrapper's own tests restore
    # REAL_REBUILD_WRAPPER and put a wrapper of their own in front of it.
    monkeypatch.setattr(qb, "rebuild_wrapper",
                        lambda *a, **k: (None, "no `rebuild` on PATH", ""))
    # `main` sets NARRATE once and never puts it back — one process per invocation in
    # real life, but in a suite a single `--json` run silences every test after it.
    monkeypatch.setattr(qb, "NARRATE", True)


REAL_REBUILD_WRAPPER = qb.rebuild_wrapper


def _git(repo: Path, *args: str) -> str:
    out = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                         check=True)
    return out.stdout.strip()


def _lock(rev: str, owner: str = "prisonblues", repo: str = "quarterback",
          name: str = "quarterback", node: str | None = None, extra: dict | None = None) -> str:
    """A flake.lock in the shape nix writes one: a root node naming its inputs, and a
    node per input keyed by an id that is USUALLY but not always the input's name."""
    node = node or name
    nodes = {"root": {"inputs": {name: node}},
             node: {"locked": {"type": "github", "owner": owner, "repo": repo, "rev": rev}}}
    nodes.update(extra or {})
    return json.dumps({"nodes": nodes, "root": "root", "version": 7})


def _commit(repo: Path, msg: str = "base") -> str:
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "-c", "commit.gpgsign=false",
         "commit", "-q", "-m", msg)
    return _git(repo, "rev-parse", "HEAD")


def _paired(tmp_path: Path, name: str) -> tuple[Path, Path]:
    """``(clone, source)`` — a bare origin with one commit, a clone tracking it, and a
    second working copy to land things through.

    A real remote on disk rather than a doubled one: `fast_forward` IS `git fetch` plus
    `git merge --ff-only`, and the whole question these tests ask is what git does with a
    divergence and with a dirty file. Doubling either call would test the double.
    """
    bare = tmp_path / f"{name}.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
    source = tmp_path / f"{name}-source"
    source.mkdir()
    _git(source, "init", "-q", "-b", "main")
    _git(source, "remote", "add", "origin", str(bare))
    (source / "README").write_text("one\n")
    _commit(source)
    _git(source, "push", "-q", "-u", "origin", "main")
    clone = tmp_path / name
    subprocess.run(["git", "clone", "-q", str(bare), str(clone)], check=True)
    return clone, source


def _land(source: Path, text: str, msg: str) -> str:
    """Put a commit on the shared origin, the way a peer's push arrives."""
    (source / "README").write_text(text)
    rev = _commit(source, msg)
    _git(source, "push", "-q", "origin", "main")
    return rev


def _wrapper(tmp_path: Path, flake: str, name: str = "rebuild") -> Path:
    """A `rebuild` in the shape this fleet's is: one literal flakeref assigned into a
    variable, then several uses that name no directory at all."""
    binn = tmp_path / "wrapperbin"
    binn.mkdir(exist_ok=True)
    script = binn / name
    script.write_text(
        "#!/bin/sh\n"
        f'flake="path:{flake}#${{host}}"\n'
        'sudo nixos-rebuild "$action" --flake "$flake"\n'
        'nixos-rebuild "$action" --flake "$flake" --target-host "$target"\n')
    script.chmod(0o755)
    return binn


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
    """A checkout whose origin names `prisonblues/quarterback` — the slug a lock carries.

    It carries a `harness/bin` because that is what makes a directory a checkout
    this tool can compare anything against, and since #414 a directory without one
    is refused rather than silently reported as carrying no drift.
    """
    repo = tmp_path / "quarterback"
    (repo / "harness" / "bin").mkdir(parents=True)
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
    assert qb.pins_repo(_lock("b" * 40), "prisonblues/quarterback") == (
        "quarterback", "quarterback", "b" * 40)


def test_a_lock_that_pins_it_by_url_is_recognised_too():
    text = json.dumps({"nodes": {"root": {"inputs": {"qb": "qb"}}, "qb": {"locked": {
        "type": "git", "url": "https://github.com/prisonblues/quarterback.git",
        "rev": "c" * 40}}}, "root": "root"})
    assert qb.pins_repo(text, "prisonblues/quarterback") == ("qb", "qb", "c" * 40)


def test_a_lock_that_pins_something_else_is_not_a_consumer():
    assert qb.pins_repo(_lock("d" * 40, owner="nixos", repo="nixpkgs"),
                        "prisonblues/quarterback") is None


def test_the_scan_finds_a_consumer_one_level_down(tmp_path, consumer_repo, monkeypatch):
    monkeypatch.setenv("QUARTERBACK_CONSUMER_ROOTS", str(tmp_path))
    consumer, why = qb.resolve_consumer(None, {}, "prisonblues/quarterback")
    assert why == "" and consumer.flake == consumer_repo.resolve()
    assert consumer.input == "quarterback" and consumer.rev == "a" * 40
    assert consumer.node == "quarterback"


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
    consumer = qb.Consumer(flake=consumer_repo, input="quarterback", node="quarterback",
                           rev="a" * 40, found_by="--flake")
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
    qb.prepare(qb.Consumer(consumer_repo, "quarterback", "quarterback", "a" * 40, "--flake"), "desktop",
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
        qb.Consumer(consumer_repo, "quarterback", "quarterback", "a" * 40, "--flake"), "desktop",
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
    proposal, _, _ = qb.prepare(qb.Consumer(consumer_repo, "quarterback", "quarterback", "a" * 40, "--flake"),
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


def test_two_escalations_from_one_box_are_two_conditions(monkeypatch, tmp_path):
    """#576, and this file had the defect the issue is about. Both call sites pass
    `refs=[issue 267]` with class `environment`, so *"the flake bump does not build"* and
    *"N scripts are not on this box"* keyed to ONE blocker row — and so did zeus and
    hermes, because a fixed issue number says nothing about which machine cannot be
    rebuilt. Measured: hermes escalated on the 27th and has no row at all; zeus's later
    escalation is the row."""
    monkeypatch.setenv("QB_LOOPS_DIR", str(LOOPS))
    sys.path.insert(0, str(LOOPS))
    import needs_human  # noqa: PLC0415
    monkeypatch.setattr(needs_human.socket, "gethostname", lambda: "ZEUS.fo.ls")
    seen: list[dict] = []
    monkeypatch.setattr(needs_human, "announce", lambda **kw: seen.append(kw) or "said")
    for fault in ("build-failed", "rebuild-waiting"):
        qb.escalate(str(BIN / "qb-bump"), summary="s", reason="r", detail="d",
                    repo="prisonblues/quarterback", key_parts=[fault],
                    condition_parts=[fault, qb.machine_id(), "nix-fleet", "zeus"])
    assert [c["condition"] for c in seen] == ["build-failed@zeus@nix-fleet@zeus",
                                              "rebuild-waiting@zeus@nix-fleet@zeus"]


def test_this_file_and_the_door_spell_the_machine_the_same_way(monkeypatch):
    """`qb-bump` truncated at the first dot and `qb-doctor` did not, which is one box
    under two names across the two halves of one escalation. A `condition` is durable in
    a way a cache key is not, so two spellings there is a standing fault arriving as two
    rows — or, in the direction that loses news, two machines collapsing into one."""
    monkeypatch.setattr(qb.socket, "gethostname", lambda: " HERMES.fo.ls ")
    sys.path.insert(0, str(LOOPS))
    import needs_human  # noqa: PLC0415
    monkeypatch.setattr(needs_human.socket, "gethostname", lambda: " HERMES.fo.ls ")
    assert qb.machine_id() == needs_human.machine_id() == "hermes"


def test_a_door_too_old_for_the_condition_still_gets_the_escalation(monkeypatch):
    """A `needs_human` predating #576 has no such keyword, and this file's contract is
    that it never raises. Asking the door what it takes is what keeps a stale harness —
    the live case, and the thing the `harness` row exists to report — costing the field
    rather than the escalation."""
    monkeypatch.setenv("QB_LOOPS_DIR", str(LOOPS))
    sys.path.insert(0, str(LOOPS))
    import needs_human  # noqa: PLC0415
    seen: list[dict] = []

    def old_announce(*, cls, reason, summary, repo="", detail="", refs=None,
                     key="", cfg=None, session=None):
        seen.append({"summary": summary})
        return "announced by a door that predates #576"

    monkeypatch.setattr(needs_human, "announce", old_announce)
    said = qb.escalate(str(BIN / "qb-bump"), summary="s", reason="r", detail="d",
                       repo="r/r", key_parts=["a"], condition_parts=["build-failed"])
    assert len(seen) == 1 and "NOT announced" not in said


def test_a_door_that_raises_does_not_escape_a_function_that_never_raises(monkeypatch):
    """This returned `needs_human.announce(...)` directly until #576, so anything the
    door threw came straight back out through a docstring that promises it does not —
    and the caller is `main`, mid-run, on a machine somebody is waiting to rebuild."""
    monkeypatch.setenv("QB_LOOPS_DIR", str(LOOPS))
    sys.path.insert(0, str(LOOPS))
    import needs_human  # noqa: PLC0415

    def explode(**kw):
        raise RuntimeError("the board's client is from another century")

    monkeypatch.setattr(needs_human, "announce", explode)
    said = qb.escalate(str(BIN / "qb-bump"), summary="s", reason="r", detail="d",
                       repo="r/r", key_parts=["a"])
    assert "NOT announced" in said and "RuntimeError" in said


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
    for leaf in ("flake", "attr", "rebuild"):
        assert f"{leaf} = lib.mkOption {{" in text.replace("\n", "\n"), \
            f"no `consumer.{leaf}` option in hm-module.nix"
    for var in (qb.FLAKE_KEY, qb.ATTR_KEY, qb.REBUILD_KEY):
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


# --------------------------------------------------------------------------- #
# what Codex found on the first cut, each with the test that would have caught it
# --------------------------------------------------------------------------- #

def test_a_repo_pinned_only_by_a_transitive_dependency_is_not_a_consumer():
    """`nodes` is the whole graph. A flake that pins something that pins this repo is
    not a consumer of this harness, and `nix flake update` could not move that node
    anyway — so scanning every node finds consumers that are not consumers."""
    text = json.dumps({
        "root": "root",
        "nodes": {"root": {"inputs": {"other": "other"}},
                  "other": {"inputs": {"quarterback": "quarterback"},
                            "locked": {"type": "github", "owner": "someone",
                                       "repo": "other", "rev": "1" * 40}},
                  "quarterback": {"locked": {"type": "github", "owner": "prisonblues",
                                             "repo": "quarterback", "rev": "2" * 40}}}})
    assert qb.pins_repo(text, "prisonblues/quarterback") is None


def test_the_name_to_update_is_the_roots_name_not_the_locks_node_id():
    """A lock disambiguates a second node for one flake as `quarterback_2`, and
    `nix flake update quarterback_2` is not a command. The name updates; the id reads."""
    text = _lock("3" * 40, name="qb", node="quarterback_2")
    assert qb.pins_repo(text, "prisonblues/quarterback") == ("qb", "quarterback_2", "3" * 40)


def test_an_input_that_merely_follows_another_is_not_the_input_to_bump():
    """A `follows` is a path through the graph, spelled as a list — somebody else's
    node by definition, and not the one a bump of this repo would move."""
    text = json.dumps({
        "root": "root",
        "nodes": {"root": {"inputs": {"quarterback": ["other", "quarterback"]}},
                  "other": {"locked": {"type": "github", "owner": "prisonblues",
                                       "repo": "quarterback", "rev": "4" * 40}}}})
    assert qb.pins_repo(text, "prisonblues/quarterback") is None


def test_a_refusal_is_recorded_so_a_later_apply_of_an_older_bump_says_so(prepared,
                                                                        consumer_repo,
                                                                        monkeypatch, capsys):
    """The proposal from this morning still builds and is still worth applying. What must
    not happen is applying it in silence right after being told today's bump is broken."""
    monkeypatch.setattr(qb, "nix", _fake_nix("c" * 40, build_rc=1, build_err="error: nope"))
    qb.prepare(qb.Consumer(consumer_repo, "quarterback", "quarterback", "a" * 40, "--flake"),
               "desktop", str(BIN / "qb-bump"), qb.Drift("fail", "behind"))
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(qb.os, "execvp", lambda f, argv: None)
    assert qb.apply(qb.load(), dry_run=False) == 0
    out = capsys.readouterr().out
    assert "was refused" in out and "cccccccccccc" in out


def test_apply_refuses_a_cached_lock_that_is_not_the_one_that_was_built(prepared,
                                                                       consumer_repo,
                                                                       monkeypatch, capsys):
    """A second qb-bump wrote the cache between the build and this call, or a write was
    interrupted. Either way the lock on disk is not the lock that was proven."""
    (qb.CACHE / "flake.lock").write_text(_lock("e" * 40))
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(qb.os, "execvp", lambda *a: pytest.fail("switched onto an unbuilt lock"))
    assert qb.apply(prepared, dry_run=False) == 3
    assert "not the one this proposal was built from" in capsys.readouterr().err
    assert (consumer_repo / "flake.lock").read_text() == _lock("a" * 40)


def test_apply_refuses_when_the_consumer_has_committed_since_the_build(prepared,
                                                                      consumer_repo,
                                                                      monkeypatch, capsys):
    """`nixos-rebuild --flake <dir>` builds that directory as it is now. A commit landing
    after the build means the system that was proven is not the one a switch would make."""
    (consumer_repo / "modules.nix").write_text("{ }\n")
    _git(consumer_repo, "add", "-A")
    _git(consumer_repo, "-c", "user.email=t@t", "-c", "user.name=t",
         "-c", "commit.gpgsign=false", "commit", "-q", "-m", "later")
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(qb.os, "execvp", lambda *a: pytest.fail("switched onto a stale build"))
    assert qb.apply(prepared, dry_run=False) == 3
    assert "since this bump was built" in capsys.readouterr().err


def test_a_modified_file_in_the_consumer_is_named_before_the_switch(consumer_repo,
                                                                    monkeypatch, capsys):
    """Not refused — an uncommitted module is a normal state for somebody's own flake, and
    refusing would make this useless on the machine it was written for. But the switch
    builds the working tree while the proof was of HEAD, so it has to be said."""
    (consumer_repo / "flake.nix").write_text("{ outputs = _: { }; }  # edited\n")
    monkeypatch.setattr(qb, "nix", _fake_nix("b" * 40))
    proposal, _, why = qb.prepare(
        qb.Consumer(consumer_repo, "quarterback", "quarterback", "a" * 40, "--flake"),
        "desktop", str(BIN / "qb-bump"), qb.Drift("fail", "behind"))
    assert why == "" and proposal.dirty == ["flake.nix"]
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(qb.os, "execvp", lambda f, argv: None)
    assert qb.apply(proposal, dry_run=False) == 0
    assert "NOT part of what was built" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# #414 — which checkout it compared, and refusing when there is not one
# --------------------------------------------------------------------------- #

def _installed_harness(tmp_path: Path, stale: bool = True) -> Path:
    """A `bin/` in the shape home-manager puts one on PATH, optionally behind a checkout.

    A real copy of `harness/bin` rather than a stub, because the subject of the
    demonstration below is what the REAL `qb-doctor` concludes when it is asked
    about a directory that has no `harness/` in it — the fallback in
    `checkout_harness_bin` that made an installed harness compare itself with
    itself. A stub doctor would answer whatever the stub was told to answer,
    which is the one thing this test must not do.
    """
    root = tmp_path / "quarterback-harness-0.1.0"
    shutil.copytree(BIN, root / "bin", ignore=shutil.ignore_patterns("__pycache__"))
    if stale:
        for gone in ("qb-reconcile", "qb-admit", "qb-stage"):
            (root / "bin" / gone).unlink(missing_ok=True)
        cw = root / "bin" / "create-worktree"
        cw.write_text(cw.read_text(encoding="utf-8") + "\n# an older revision\n",
                      encoding="utf-8")
    return root / "bin"


def test_a_cwd_that_is_not_a_checkout_refuses_instead_of_reporting_nothing_to_carry(
        tmp_path, monkeypatch, capsys):
    """#414, reproduced end to end with the real `qb-doctor` and no stub anywhere.

    The morning this was found: a harness 74 commits and eleven releases behind on
    PATH, `qb-bump` run from `~/source/nix-fleet`, and the answer was *"nothing to
    carry: the harness on PATH IS this checkout"* — exit 0, a positive assertion of
    health, about a box `qb-doctor` called `FAIL — 10 differ` sixty seconds later.

    Both halves are asserted here because it is the disagreement that is the bug:
    pointed at a real checkout the drift comes back `fail`, and from a directory
    that is not a checkout the tool now says it cannot tell instead of saying
    there is nothing to do.
    """
    installed = _installed_harness(tmp_path)
    checkout = tmp_path / "quarterback"
    shutil.copytree(BIN, checkout / "harness" / "bin",
                    ignore=shutil.ignore_patterns("__pycache__"))
    _git(checkout, "init", "-q", "-b", "main")
    # A git repository that is not THIS one — `~/source/nix-fleet`, where the
    # command was actually typed. It has to be a real repository for the bug to
    # appear at all: a plain directory already refused, because `qb-doctor` exits 3
    # on one and `harness_drift` reads that as "could not be run". A repository with
    # no `harness/` in it is the case that got all the way to a green answer.
    elsewhere = tmp_path / "nix-fleet"
    elsewhere.mkdir()
    _git(elsewhere, "init", "-q", "-b", "main")

    # Prepended, not replaced: `qb-doctor` shells out to `git`, and a PATH with no
    # git in it would fail this for a reason that is not the one under test.
    monkeypatch.setenv("PATH", f"{installed}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(qb, "__file__", str(installed / "qb-bump"))
    monkeypatch.setattr(qb, "have_nix", lambda: False)
    monkeypatch.chdir(elsewhere)

    # Pointed at the checkout, the drift is real and `qb-doctor` says so.
    assert qb.main(["--repo", str(checkout), "--json", "--no-announce"]) == 1
    aimed = json.loads(capsys.readouterr().out)
    assert aimed["drift"]["verdict"] == "fail"
    assert aimed["repo"] == {"path": str(checkout), "found_by": "named by --repo"}

    # Same machine, same second, same PATH — from a directory that is not a
    # checkout. This used to be exit 0 and "nothing to carry".
    assert qb.main(["--json", "--no-announce"]) == 1
    blind = json.loads(capsys.readouterr().out)
    assert blind["outcome"] == "unknown"
    assert blind["detail"].startswith("cannot tell")
    assert str(elsewhere) in blind["detail"] and "not a quarterback checkout" in blind["detail"]
    assert "--repo" in blind["detail"] and "QUARTERBACK_REPO" in blind["detail"]


def test_the_refusal_goes_to_stderr_and_never_reads_as_an_all_clear(tmp_path, monkeypatch,
                                                                    capsys):
    monkeypatch.chdir(tmp_path)
    assert qb.main(["--no-announce"]) == 1
    out, err = capsys.readouterr()
    assert out == ""
    assert "cannot tell" in err and "not knowing" in err


def test_a_declared_checkout_makes_it_work_from_anywhere(tmp_path, quarterback_repo,
                                                         monkeypatch, capsys):
    """Door three: a fleet that says where its checkout is gets an answer from `~`."""
    elsewhere = tmp_path / "somewhere-else"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    monkeypatch.setenv("QUARTERBACK_REPO", str(quarterback_repo))
    monkeypatch.setattr(sys, "argv", ["qb-bump"])
    monkeypatch.setattr(qb, "__file__", _stub_doctor(tmp_path, CURRENT))
    assert qb.main(["--json", "--no-announce"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["repo"] == {"path": str(quarterback_repo),
                           "found_by": "declared by QUARTERBACK_REPO"}


def test_a_declared_checkout_that_is_not_one_is_refused_rather_than_fallen_back_from(
        tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("QUARTERBACK_REPO", str(tmp_path / "gone"))
    checkout, why = qb.resolve_repo(None, {})
    assert checkout is None and "QUARTERBACK_REPO" in why and "not a quarterback checkout" in why


def test_a_named_repo_that_is_not_a_checkout_is_a_typo_and_says_so(tmp_path, consumer_repo):
    """`--repo` is somebody saying it, so a wrong one is corrected, not worked around —
    the same reason `named_consumer` refuses a `--flake` that pins nothing."""
    checkout, why = qb.resolve_repo(str(consumer_repo), {})
    assert checkout is None
    assert str(consumer_repo) in why and "harness/bin" in why


def test_the_working_directory_wins_over_a_declared_one_and_resolves_to_the_root(
        tmp_path, quarterback_repo, monkeypatch):
    """A person standing in a checkout means that one — and standing in a subdirectory
    of it still means it, which is why the row names the root and not the cwd."""
    monkeypatch.setenv("QUARTERBACK_REPO", str(tmp_path / "elsewhere"))
    monkeypatch.chdir(quarterback_repo / "harness")
    checkout, why = qb.resolve_repo(None, {"QUARTERBACK_REPO": str(tmp_path / "elsewhere")})
    assert why == "" and checkout.found_by == "the working directory"
    assert checkout.path.resolve() == quarterback_repo.resolve()


def test_the_no_op_names_the_checkout_it_compared(tmp_path, quarterback_repo, capsys,
                                                  monkeypatch):
    """"Nothing to carry" is a claim about a specific pair of directories, and a reader
    who cannot see which pair cannot tell a true one from #414's false one."""
    monkeypatch.setattr(sys, "argv", ["qb-bump"])
    monkeypatch.setattr(qb, "__file__", _stub_doctor(tmp_path, CURRENT))
    assert qb.main(["--repo", str(quarterback_repo), "--no-announce"]) == 0
    said = capsys.readouterr().out
    assert "nothing to carry" in said
    assert f"compared against {quarterback_repo} (named by --repo)" in said


NO_HARNESS = {"checks": [{"name": "harness", "subject": "-", "verdict": "unknown",
                          "detail": "no harness on PATH (create-worktree not found), so "
                                    "nothing to compare this checkout against", "extra": {}}]}


def test_a_harness_row_that_is_not_ok_is_not_an_all_clear(tmp_path, quarterback_repo, capsys,
                                                          monkeypatch):
    """Found by Codex on this branch, one function from #414 and the same mistake: "nothing to
    carry" used to mean `not fail`, so a row of `unknown` — a machine with no harness on PATH
    AT ALL, which is the most carrying-needed state there is — came back as exit 0."""
    monkeypatch.setattr(sys, "argv", ["qb-bump"])
    monkeypatch.setattr(qb, "__file__", _stub_doctor(tmp_path, NO_HARNESS))
    assert qb.main(["--repo", str(quarterback_repo), "--json", "--no-announce"]) == 1
    out = json.loads(capsys.readouterr().out)
    assert out["outcome"] == "unknown"
    assert out["detail"].startswith("cannot tell") and "unknown, not ok" in out["detail"]
    assert out["drift"]["verdict"] == "unknown"


def test_only_ok_and_fail_are_answers_and_a_warn_is_neither(tmp_path, quarterback_repo,
                                                            monkeypatch):
    warned = {"checks": [{"name": "harness", "subject": "/nix/store/x/bin", "verdict": "warn",
                          "detail": "something the doctor is not sure about", "extra": {}}]}
    monkeypatch.setattr(sys, "argv", ["qb-bump"])
    monkeypatch.setattr(qb, "__file__", _stub_doctor(tmp_path, warned))
    assert qb.main(["--repo", str(quarterback_repo), "--no-announce"]) == 1


def test_the_no_op_names_the_installed_harness_as_well_as_the_checkout(tmp_path,
                                                                      quarterback_repo,
                                                                      capsys, monkeypatch):
    """A comparison has two sides, and the one that is actually in doubt is which `bin/` PATH
    resolved to. `qb-doctor` already reports it as the row's subject; this stops dropping it."""
    monkeypatch.setattr(sys, "argv", ["qb-bump"])
    monkeypatch.setattr(qb, "__file__", _stub_doctor(tmp_path, CURRENT))
    assert qb.main(["--repo", str(quarterback_repo), "--no-announce"]) == 0
    assert "/nix/store/x/bin, compared against" in capsys.readouterr().out


def test_the_environment_beats_the_site_config_for_the_declared_checkout(tmp_path,
                                                                        quarterback_repo,
                                                                        monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("QUARTERBACK_REPO", str(quarterback_repo))
    checkout, why = qb.resolve_repo(None, {"QUARTERBACK_REPO": str(tmp_path / "from-the-file")})
    assert why == "" and checkout.path.resolve() == quarterback_repo.resolve()


# --------------------------------------------------------------------------- #
# the pull — a fast-forward, and never anything that can lose work (#533)
# --------------------------------------------------------------------------- #

def test_a_tree_is_fast_forwarded_onto_what_a_peer_pushed(tmp_path):
    clone, source = _paired(tmp_path, "repo")
    landed = _land(source, "two\n", "landed")
    got = qb.fast_forward(clone)
    assert got.why == "" and got.blocked is False and got.moved
    assert got.after == landed
    assert _git(clone, "rev-parse", "HEAD") == landed


def test_a_tree_already_level_with_its_upstream_has_nothing_to_say(tmp_path):
    clone, _ = _paired(tmp_path, "repo")
    got = qb.fast_forward(clone)
    assert got.why == "" and not got.moved and not got.blocked


def test_a_tree_with_no_upstream_is_reported_and_is_not_doubt(consumer_repo):
    """`blocked` is False, and the distinction is the whole point of the field: a worktree
    tracking nothing cannot be behind anything, so it must not turn every `nothing to
    carry` on this fleet into `cannot tell`. A signal that fires on every run is ignored."""
    got = qb.fast_forward(consumer_repo)
    assert got.why and "nothing to pull it up to" in got.why
    assert got.blocked is False


def test_a_diverged_branch_is_reported_and_never_rebased_or_reset(tmp_path):
    """The refusal is the feature. Resolving somebody's divergence is not this file's
    business at any level of confidence, and the commands that would (`pull --rebase`,
    `reset --hard`) are the ones the harness refuses outright in a shared tree."""
    clone, source = _paired(tmp_path, "repo")
    _land(source, "theirs\n", "theirs")
    (clone / "README").write_text("mine\n")
    mine = _commit(clone, "mine")
    got = qb.fast_forward(clone)
    assert got.blocked and "will not fast-forward" in got.why
    assert _git(clone, "rev-parse", "HEAD") == mine, "a divergence is the person's to resolve"


def test_an_uncommitted_edit_survives_a_pull_that_would_have_overwritten_it(tmp_path):
    """The file class this fleet has lost five times. `merge --ff-only` refuses rather than
    clobbering, and nothing here is allowed to answer that refusal with a `checkout --`."""
    clone, source = _paired(tmp_path, "repo")
    _land(source, "theirs\n", "theirs")
    (clone / "README").write_text("half-written\n")
    got = qb.fast_forward(clone)
    assert got.blocked
    assert (clone / "README").read_text() == "half-written\n"


def test_a_fetch_that_fails_is_blocked_rather_than_a_traceback(tmp_path):
    clone, _ = _paired(tmp_path, "repo")
    _git(clone, "remote", "set-url", "origin", str(tmp_path / "no-such-remote.git"))
    got = qb.fast_forward(clone)
    assert got.blocked and "git fetch" in got.why


# --------------------------------------------------------------------------- #
# the pull is BEFORE the comparison, and a pull that failed is not an all-clear
# --------------------------------------------------------------------------- #

def test_the_pull_happens_before_the_comparison_and_not_after(quarterback_repo, monkeypatch):
    """Order, not presence. The verdict is *about* the checkout, so pulling afterwards
    would answer about the tree as it was found — which is the stale-checkout all-clear
    this closes, with an extra `git fetch` run for nothing."""
    order: list[str] = []

    def pulled(where, **kw):
        order.append("pull")
        return qb.Pulled(path=str(where))

    def compared(*a):
        order.append("compare")
        return qb.Drift("ok", "same"), ""

    monkeypatch.setattr(qb, "fast_forward", pulled)
    monkeypatch.setattr(qb, "harness_drift", compared)
    assert qb.main(["--repo", str(quarterback_repo), "--no-announce"]) == 0
    assert order[:2] == ["pull", "compare"]


def test_no_pull_touches_neither_tree(quarterback_repo, monkeypatch):
    monkeypatch.setattr(qb, "fast_forward",
                        lambda *a, **k: pytest.fail("--no-pull pulled something"))
    monkeypatch.setattr(qb, "harness_drift", lambda *a: (qb.Drift("ok", "same"), ""))
    assert qb.main(["--repo", str(quarterback_repo), "--no-pull", "--no-announce"]) == 0


def test_a_checkout_that_could_not_get_level_is_not_nothing_to_carry(quarterback_repo,
                                                                    monkeypatch, capsys):
    """#414's lesson at a different commit. A checkout that could not be brought up to date
    agrees with an installed harness that is equally behind, and "nothing to carry" is a
    positive assertion of health that a person who reads it stops looking after."""
    monkeypatch.setattr(qb, "fast_forward", lambda where, **kw: qb.Pulled(
        path=str(where), why="`git fetch` failed: no route to host", blocked=True))
    monkeypatch.setattr(qb, "harness_drift", lambda *a: (qb.Drift("ok", "same"), ""))
    assert qb.main(["--repo", str(quarterback_repo), "--no-announce"]) == 1
    err = capsys.readouterr().err
    assert "no route to host" in err and "level with what it tracks" in err


def test_a_tree_with_nothing_to_pull_from_still_allows_nothing_to_carry(quarterback_repo,
                                                                       monkeypatch):
    monkeypatch.setattr(qb, "fast_forward", lambda where, **kw: qb.Pulled(
        path=str(where), why="no upstream branch", blocked=False))
    monkeypatch.setattr(qb, "harness_drift", lambda *a: (qb.Drift("ok", "same"), ""))
    assert qb.main(["--repo", str(quarterback_repo), "--no-announce"]) == 0


def test_a_consumer_that_moved_on_the_pull_is_reason_enough_on_its_own(consumer_repo,
                                                                      quarterback_repo,
                                                                      monkeypatch, capsys):
    """Having pulled the consuming flake, act on it. A commit landed there from another box
    is a rebuild this machine owes even when the harness pin has not budged, and pulling it
    in and then printing "nothing to carry" leaves a silently-changed checkout and no
    follow-through — worse than never having pulled it."""
    monkeypatch.setenv("QUARTERBACK_CONSUMER_ROOTS", str(consumer_repo.parent))
    monkeypatch.setattr(qb, "have_nix", lambda: True)
    monkeypatch.setattr(qb, "nix", _fake_nix("b" * 40))
    monkeypatch.setattr(qb, "harness_drift",
                        lambda *a: (qb.Drift("ok", "the harness on PATH IS this checkout"), ""))
    monkeypatch.setattr(qb, "resolve_attr", lambda *a: ("desktop", ""))
    monkeypatch.setattr(qb, "fast_forward", lambda where, **kw: (
        qb.Pulled(path=str(where), upstream="origin/main", before="1" * 40, after="2" * 40)
        if Path(where) == consumer_repo else qb.Pulled(path=str(where))))
    assert qb.main(["--repo", str(quarterback_repo), "--no-announce"]) == 2
    out = capsys.readouterr().out
    assert "111111111111 -> 222222222222" in out
    assert "behind the flake it is built from" in out


def test_the_report_says_what_each_pull_did(consumer_repo, quarterback_repo, monkeypatch,
                                            capsys):
    monkeypatch.setenv("QUARTERBACK_CONSUMER_ROOTS", str(consumer_repo.parent))
    monkeypatch.setattr(qb, "have_nix", lambda: True)
    monkeypatch.setattr(qb, "nix", _fake_nix("b" * 40))
    monkeypatch.setattr(qb, "harness_drift", lambda *a: (qb.Drift("fail", "behind"), ""))
    monkeypatch.setattr(qb, "resolve_attr", lambda *a: ("desktop", ""))
    monkeypatch.setattr(qb, "fast_forward", lambda where, **kw: qb.Pulled(
        path=str(where), upstream="origin/main", before="1" * 40, after="2" * 40))
    assert qb.main(["--repo", str(quarterback_repo), "--no-announce"]) == 2
    out = capsys.readouterr().out
    assert "pulled     checkout: 111111111111 -> 222222222222" in out
    assert "pulled     consumer: 111111111111 -> 222222222222" in out


def test_a_consumer_pull_that_moved_the_lock_is_re_read_before_it_is_bumped(consumer_repo,
                                                                           quarterback_repo,
                                                                           monkeypatch):
    """The pull moves `flake.lock` too, and with it the rev this bump is FROM. Reporting the
    pre-pull rev would print a pin transition that never happened."""
    monkeypatch.setenv("QUARTERBACK_CONSUMER_ROOTS", str(consumer_repo.parent))
    monkeypatch.setattr(qb, "have_nix", lambda: True)
    monkeypatch.setattr(qb, "nix", _fake_nix("b" * 40))
    monkeypatch.setattr(qb, "harness_drift", lambda *a: (qb.Drift("fail", "behind"), ""))
    monkeypatch.setattr(qb, "resolve_attr", lambda *a: ("desktop", ""))

    def moved(where, **kw):
        if Path(where) == consumer_repo:
            (consumer_repo / "flake.lock").write_text(_lock("c" * 40))
            _commit(consumer_repo, "a lock that arrived with the pull")
            return qb.Pulled(path=str(where), upstream="origin/main",
                             before="1" * 40, after="2" * 40)
        return qb.Pulled(path=str(where))

    monkeypatch.setattr(qb, "fast_forward", moved)
    assert qb.main(["--repo", str(quarterback_repo), "--no-announce"]) == 2
    assert qb.load().old_rev == "c" * 40, "the bump is FROM what the pull left behind"


# --------------------------------------------------------------------------- #
# the rebuild wrapper — used only when it can be shown to build what was built
# --------------------------------------------------------------------------- #

def test_the_scan_reads_the_one_flake_a_wrapper_names(tmp_path):
    """This fleet's `rebuild` in the shape it is actually in: one literal `path:<dir>#$host`
    assignment, and several later `--flake "$flake"` uses that name no directory."""
    binn = _wrapper(tmp_path, "/home/rich/source/nix-fleet")
    found, why = qb.wrapper_targets(binn / "rebuild")
    assert why == "" and found == {"/home/rich/source/nix-fleet"}


def test_the_wrapper_is_used_when_it_builds_the_flake_that_was_just_built(prepared, tmp_path,
                                                                         monkeypatch):
    monkeypatch.setattr(qb, "rebuild_wrapper", REAL_REBUILD_WRAPPER)
    monkeypatch.setenv("PATH", f"{_wrapper(tmp_path, prepared.flake)}{os.pathsep}"
                               f"{os.environ['PATH']}")
    binn = _wrapper(tmp_path, prepared.flake)
    cmd, how, _typed = qb.switch_command(prepared, {})
    assert cmd == [str(binn / "rebuild"), "switch"]
    assert "home-manager" in how, "the report has to say why the wrapper is worth using"


def test_a_wrapper_that_builds_another_flake_is_not_this_flakes_wrapper(prepared, tmp_path,
                                                                       monkeypatch):
    """Switching onto a system nobody here built is the single outcome this whole file is
    arranged to prevent, and a wrapper pointed somewhere else does exactly that."""
    monkeypatch.setattr(qb, "rebuild_wrapper", REAL_REBUILD_WRAPPER)
    monkeypatch.setenv("PATH", f"{_wrapper(tmp_path, '/somewhere/else')}{os.pathsep}"
                               f"{os.environ['PATH']}")
    cmd, how, _typed = qb.switch_command(prepared, {})
    assert cmd[:2] == ["sudo", "nixos-rebuild"]
    assert "not this flake's wrapper" in how


def test_a_wrapper_naming_no_flake_falls_back_rather_than_guessing(prepared, tmp_path,
                                                                   monkeypatch):
    binn = tmp_path / "wrapperbin"
    binn.mkdir()
    (binn / "rebuild").write_text('#!/bin/sh\nsudo nixos-rebuild switch --flake "$f"\n')
    (binn / "rebuild").chmod(0o755)
    monkeypatch.setattr(qb, "rebuild_wrapper", REAL_REBUILD_WRAPPER)
    monkeypatch.setenv("PATH", f"{binn}{os.pathsep}{os.environ['PATH']}")
    cmd, how, _typed = qb.switch_command(prepared, {})
    assert cmd[:2] == ["sudo", "nixos-rebuild"]
    assert "names no flake directory" in how


def test_a_wrapper_with_two_flakes_in_it_cannot_be_shown_to_build_this_one(prepared, tmp_path,
                                                                          monkeypatch):
    binn = tmp_path / "wrapperbin"
    binn.mkdir()
    (binn / "rebuild").write_text(
        f'#!/bin/sh\nflake="path:{prepared.flake}#a"\nother="path:/somewhere/else#b"\n')
    (binn / "rebuild").chmod(0o755)
    monkeypatch.setattr(qb, "rebuild_wrapper", REAL_REBUILD_WRAPPER)
    monkeypatch.setenv("PATH", f"{binn}{os.pathsep}{os.environ['PATH']}")
    assert qb.switch_command(prepared, {})[0][:2] == ["sudo", "nixos-rebuild"]


def test_the_wrapper_is_read_and_never_run(prepared, tmp_path, monkeypatch):
    """Finding out what a wrapper targets by RUNNING it would mean executing an arbitrary
    script on an arbitrary host to answer a question about it. The read cannot do anything;
    its worst outcome is falling back to a command that was already correct."""
    binn = tmp_path / "wrapperbin"
    binn.mkdir()
    marker = tmp_path / "the-wrapper-ran"
    (binn / "rebuild").write_text(
        f'#!/bin/sh\ntouch {marker}\nflake="path:{prepared.flake}#desktop"\n')
    (binn / "rebuild").chmod(0o755)
    monkeypatch.setattr(qb, "rebuild_wrapper", REAL_REBUILD_WRAPPER)
    monkeypatch.setenv("PATH", f"{binn}{os.pathsep}{os.environ['PATH']}")
    assert qb.switch_command(prepared, {})[0] == [str(binn / "rebuild"), "switch"]
    assert not marker.exists()


def test_a_declared_rebuild_command_is_taken_as_consent(prepared, tmp_path, monkeypatch):
    """A declaration is somebody saying what their wrapper builds, which is the one input
    this file is not entitled to argue with — the same door `--flake` and `--repo` are."""
    monkeypatch.setattr(qb, "rebuild_wrapper", REAL_REBUILD_WRAPPER)
    binn = _wrapper(tmp_path, "/somewhere/else", name="fleet-rebuild")
    monkeypatch.setenv("PATH", f"{binn}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv(qb.REBUILD_KEY, "fleet-rebuild switch --fast")
    cmd, how, _typed = qb.switch_command(prepared, {})
    assert cmd == [str(binn / "fleet-rebuild"), "switch", "--fast"]
    assert qb.REBUILD_KEY in how


def test_a_declared_command_that_is_not_on_path_falls_back_rather_than_failing(prepared,
                                                                              monkeypatch):
    monkeypatch.setattr(qb, "rebuild_wrapper", REAL_REBUILD_WRAPPER)
    monkeypatch.setenv(qb.REBUILD_KEY, "no-such-rebuild switch")
    cmd, how, _typed = qb.switch_command(prepared, {})
    assert cmd[:2] == ["sudo", "nixos-rebuild"]
    assert "not on PATH" in how


def test_no_wrapper_asks_for_the_explicit_command_without_looking(prepared, monkeypatch):
    monkeypatch.setattr(qb, "rebuild_wrapper",
                        lambda *a, **k: pytest.fail("--no-wrapper went looking for one"))
    cmd, how, _typed = qb.switch_command(prepared, {}, wrapper=False)
    assert cmd[:2] == ["sudo", "nixos-rebuild"]
    assert "--no-wrapper" in how


def test_the_wrapper_and_not_sudo_is_what_gets_execd(prepared, tmp_path, monkeypatch):
    """`execvp` used to be hard-coded to `sudo`, which would have run the wrapper's argv
    under the wrong argv[0]. A wrapper is not sudo; it calls sudo itself. And what is
    exec'd is the resolved path that `wrapper_targets` read, not the bare name."""
    monkeypatch.setattr(qb, "rebuild_wrapper", REAL_REBUILD_WRAPPER)
    monkeypatch.setenv("PATH", f"{_wrapper(tmp_path, prepared.flake)}{os.pathsep}"
                               f"{os.environ['PATH']}")
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    execd: list = []
    monkeypatch.setattr(qb.os, "execvp", lambda file, argv: execd.append((file, argv)))
    assert qb.apply(prepared, dry_run=False) == 0
    wrapper = str(tmp_path / "wrapperbin" / "rebuild")
    assert execd == [(wrapper, [wrapper, "switch"])], \
        "the file that was READ has to be the file that is RUN — a second PATH lookup " \
        "at exec time reopens exactly the question wrapper_targets just answered"


# --------------------------------------------------------------------------- #
# --apply — one command that does the whole job (#533)
# --------------------------------------------------------------------------- #

def _whole_job(monkeypatch, consumer_repo, *, drift=None, nix=None):
    """The doubles every end-to-end `--apply` test needs, and nothing else."""
    monkeypatch.setenv("QUARTERBACK_CONSUMER_ROOTS", str(consumer_repo.parent))
    monkeypatch.setattr(qb, "have_nix", lambda: True)
    monkeypatch.setattr(qb, "nix", nix or _fake_nix("b" * 40))
    monkeypatch.setattr(qb, "harness_drift", lambda *a: (
        drift or qb.Drift("fail", "behind: 1 absent (qb-admit)", missing=["qb-admit"]), ""))
    monkeypatch.setattr(qb, "resolve_attr", lambda *a: ("desktop", ""))
    monkeypatch.setattr(qb, "fast_forward", lambda where, **kw: qb.Pulled(path=str(where)))
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)


def test_apply_pulls_bumps_builds_and_switches_in_one_command(consumer_repo, quarterback_repo,
                                                              monkeypatch):
    """What #533 is about. `--apply` used to refuse whenever the cached proposal had gone
    stale, which is the state it is in most times a person reaches for it — the agent
    prepared it an hour and three merges ago."""
    _whole_job(monkeypatch, consumer_repo)
    execd: list = []
    monkeypatch.setattr(qb.os, "execvp", lambda file, argv: execd.append(argv))
    assert qb.main(["--repo", str(quarterback_repo), "--apply", "--no-announce"]) == 0
    assert (consumer_repo / "flake.lock").read_text() == _lock("b" * 40)
    assert execd == [["sudo", "nixos-rebuild", "switch", "--flake",
                      f"{consumer_repo}#desktop"]]


def test_apply_switches_onto_what_it_just_proved_with_nothing_in_the_cache(consumer_repo,
                                                                          quarterback_repo,
                                                                          monkeypatch):
    _whole_job(monkeypatch, consumer_repo)
    assert qb.load() is None, "the premise: there is no proposal to fall back on"
    monkeypatch.setattr(qb.os, "execvp", lambda file, argv: None)
    assert qb.main(["--repo", str(quarterback_repo), "--apply", "--no-announce"]) == 0
    assert qb.load().new_rev == "b" * 40


def test_apply_never_switches_onto_a_bump_that_did_not_build(consumer_repo, quarterback_repo,
                                                             monkeypatch):
    _whole_job(monkeypatch, consumer_repo,
               nix=_fake_nix("b" * 40, build_rc=1, build_err="error: conflicting definition"))
    monkeypatch.setattr(qb.os, "execvp",
                        lambda *a: pytest.fail("switched onto a build that failed"))
    assert qb.main(["--repo", str(quarterback_repo), "--apply", "--no-announce"]) == 4
    assert (consumer_repo / "flake.lock").read_text() == _lock("a" * 40)


def test_apply_with_nothing_to_carry_does_not_rebuild_for_the_sake_of_it(consumer_repo,
                                                                        quarterback_repo,
                                                                        monkeypatch):
    _whole_job(monkeypatch, consumer_repo, drift=qb.Drift("ok", "the harness on PATH IS it"))
    monkeypatch.setattr(qb.os, "execvp", lambda *a: pytest.fail("switched with nothing to do"))
    assert qb.main(["--repo", str(quarterback_repo), "--apply", "--no-announce"]) == 0


def test_apply_does_not_escalate_because_the_person_is_already_at_the_keyboard(
        consumer_repo, quarterback_repo, monkeypatch):
    """#274's door is not a logbook. An escalation raised at the exact moment it is being
    answered is noise on a board somebody else has to read."""
    _whole_job(monkeypatch, consumer_repo)
    monkeypatch.setattr(qb, "escalate",
                        lambda *a, **k: pytest.fail("escalated while a person was applying"))
    monkeypatch.setattr(qb.os, "execvp", lambda file, argv: None)
    assert qb.main(["--repo", str(quarterback_repo), "--apply"]) == 0


def test_apply_dry_run_does_everything_except_the_switch(consumer_repo, quarterback_repo,
                                                         monkeypatch, capsys):
    """The build is not skipped, and that is deliberate: a dry run of "the whole job" that
    left out the part most likely to fail would be lying about the job."""
    _whole_job(monkeypatch, consumer_repo)
    monkeypatch.setattr(qb.os, "execvp", lambda *a: pytest.fail("a dry run switched"))
    assert qb.main(["--repo", str(quarterback_repo), "--apply", "--dry-run",
                    "--no-announce"]) == 0
    assert (consumer_repo / "flake.lock").read_text() == _lock("a" * 40)
    assert "sudo nixos-rebuild switch --flake" in capsys.readouterr().out


def test_apply_still_refuses_without_a_terminal_however_much_it_now_does(consumer_repo,
                                                                        quarterback_repo,
                                                                        monkeypatch, capsys):
    """The ceiling did not move. A timer, a CI job or an agent that reaches for `--apply`
    changes nothing, whatever else this command has learned to do."""
    _whole_job(monkeypatch, consumer_repo)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(qb.os, "execvp", lambda *a: pytest.fail("switched without a tty"))
    assert qb.main(["--repo", str(quarterback_repo), "--apply", "--no-announce"]) == 3
    assert (consumer_repo / "flake.lock").read_text() == _lock("a" * 40)
    assert "needs a terminal" in capsys.readouterr().err


def test_cached_applies_what_is_in_the_cache_and_prepares_nothing(prepared, consumer_repo,
                                                                  monkeypatch):
    """The door back, for a host that lost its network between the preparation and the
    person: `nix flake update` cannot run, and a proposal that was already proven is still
    worth applying."""
    monkeypatch.setattr(qb, "prepared_bump",
                        lambda *a: pytest.fail("--cached prepared something"))
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    execd: list = []
    monkeypatch.setattr(qb.os, "execvp", lambda file, argv: execd.append(argv))
    assert qb.main(["--apply", "--cached"]) == 0
    assert (consumer_repo / "flake.lock").read_text() == _lock("b" * 40)
    assert len(execd) == 1


def test_cached_without_apply_is_a_typo_and_says_so(capsys):
    assert qb.main(["--cached"]) == 3
    assert "about --apply" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# what a second reader found: the ways the new paths could still be wrong
# --------------------------------------------------------------------------- #

def test_a_detached_head_is_doubt_and_not_nothing_to_pull_from(tmp_path):
    """The two look identical to `@{u}` and mean opposite things. A branch tracking
    nothing cannot be behind anything; a detached HEAD can be sitting on a commit from
    three weeks ago, which is the entire state this pull exists to rule out."""
    clone, source = _paired(tmp_path, "repo")
    _land(source, "two\n", "landed")
    _git(clone, "checkout", "-q", "--detach", "HEAD")
    got = qb.fast_forward(clone)
    assert got.blocked, "a detached checkout can be stale, so it is not an all-clear"
    assert "detached" in got.why


def test_a_detached_checkout_is_not_reported_as_nothing_to_carry(tmp_path, quarterback_repo,
                                                                 monkeypatch, capsys):
    monkeypatch.setattr(qb, "fast_forward", lambda where, **kw: qb.Pulled(
        path=str(where), why=f"{where} is a detached HEAD", blocked=True))
    monkeypatch.setattr(qb, "harness_drift", lambda *a: (qb.Drift("ok", "same"), ""))
    assert qb.main(["--repo", str(quarterback_repo), "--no-announce"]) == 1
    assert "detached HEAD" in capsys.readouterr().err


def test_a_consumer_that_could_not_be_fetched_is_a_caveat_and_not_a_downgrade(
        consumer_repo, quarterback_repo, monkeypatch, capsys):
    """A consuming flake that cannot be reached does not weaken the harness comparison by
    one word — it only means the second reason to act was never looked for. Downgrading on
    it would make every agent run on a host whose remote wants a credential answer "cannot
    tell" forever, and a tool that always says that is one nobody reads."""
    monkeypatch.setenv("QUARTERBACK_CONSUMER_ROOTS", str(consumer_repo.parent))
    monkeypatch.setattr(qb, "have_nix", lambda: True)
    monkeypatch.setattr(qb, "harness_drift", lambda *a: (qb.Drift("ok", "same"), ""))
    monkeypatch.setattr(qb, "fast_forward", lambda where, **kw: (
        qb.Pulled(path=str(where), why="`git fetch` failed: could not read Username",
                  blocked=True)
        if Path(where) == consumer_repo else qb.Pulled(path=str(where))))
    assert qb.main(["--repo", str(quarterback_repo), "--no-announce"]) == 0
    out = capsys.readouterr().out
    assert "could not be brought up to date" in out
    assert "not accounted for" in out


def test_nothing_to_carry_says_when_it_never_looked_at_a_consumer(quarterback_repo,
                                                                  monkeypatch, capsys):
    """`current`/0 is a positive assertion of health, and since a moved consuming flake is
    now a reason to act on its own, a run that never found one must not imply it checked."""
    monkeypatch.setattr(qb, "have_nix", lambda: False)
    monkeypatch.setattr(qb, "fast_forward", lambda where, **kw: qb.Pulled(path=str(where)))
    monkeypatch.setattr(qb, "harness_drift", lambda *a: (qb.Drift("ok", "same"), ""))
    assert qb.main(["--repo", str(quarterback_repo), "--no-announce"]) == 0
    assert "no nix on this host" in capsys.readouterr().out


def test_no_pull_says_its_answer_is_not_about_any_upstream(quarterback_repo, monkeypatch,
                                                           capsys):
    monkeypatch.setattr(qb, "harness_drift", lambda *a: (qb.Drift("ok", "same"), ""))
    assert qb.main(["--repo", str(quarterback_repo), "--no-pull", "--no-announce"]) == 0
    assert "--no-pull" in capsys.readouterr().out


def test_a_named_host_refuses_the_wrapper_because_the_wrapper_names_its_own(prepared,
                                                                           tmp_path,
                                                                           monkeypatch):
    """`--host laptop` on a desktop builds `laptop` while the wrapper derives `desktop`
    from the hostname and switches THAT — a configuration this run never built. No reading
    of the wrapper can rule it out, because the attribute never appears in its text."""
    monkeypatch.setattr(qb, "rebuild_wrapper", REAL_REBUILD_WRAPPER)
    monkeypatch.setenv("PATH", f"{_wrapper(tmp_path, prepared.flake)}{os.pathsep}"
                               f"{os.environ['PATH']}")
    prepared.attr_named = True
    cmd, how, _typed = qb.switch_command(prepared, {})
    assert cmd[:2] == ["sudo", "nixos-rebuild"]
    assert "--host named" in how


def test_a_matched_host_is_what_lets_the_wrapper_be_used_at_all(prepared, tmp_path,
                                                                monkeypatch):
    monkeypatch.setattr(qb, "rebuild_wrapper", REAL_REBUILD_WRAPPER)
    binn = _wrapper(tmp_path, prepared.flake)
    monkeypatch.setenv("PATH", f"{binn}{os.pathsep}{os.environ['PATH']}")
    assert prepared.attr_named is False, "the fixture matched rather than named it"
    assert qb.switch_command(prepared, {})[0] == [str(binn / "rebuild"), "switch"]


def test_a_named_host_is_recorded_on_the_proposal_so_cached_still_refuses(consumer_repo,
                                                                         quarterback_repo,
                                                                         monkeypatch):
    """`--apply --cached` reads a proposal off disk with no argv to consult, so the fact
    has to travel with it rather than be re-derived."""
    monkeypatch.setenv("QUARTERBACK_CONSUMER_ROOTS", str(consumer_repo.parent))
    monkeypatch.setattr(qb, "have_nix", lambda: True)
    monkeypatch.setattr(qb, "nix", _fake_nix("b" * 40))
    monkeypatch.setattr(qb, "harness_drift", lambda *a: (qb.Drift("fail", "behind"), ""))
    monkeypatch.setattr(qb, "fast_forward", lambda where, **kw: qb.Pulled(path=str(where)))
    assert qb.main(["--repo", str(quarterback_repo), "--host", "laptop",
                    "--no-announce"]) == 2
    assert qb.load().attr_named is True


def test_a_commented_out_flakeref_is_not_evidence_of_anything(prepared, tmp_path,
                                                              monkeypatch):
    """The decoy this was found by: a commented-out `flake=` naming the right directory,
    above a live line that builds something else out of a variable. With the comment
    counting as evidence, the file "names one directory and it is ours" and the wrapper is
    trusted — while what it actually switches is `recovery-fleet#rescue`.

    With whole-line comments dropped the file names *nothing* this can read (the live
    target is assembled from a variable and is invisible to a regex either way), and
    nothing means the explicit command. That is the point: the scan's failures all have to
    land on the fall-back side, and a comment is not a statement about what a script does."""
    binn = tmp_path / "wrapperbin"
    binn.mkdir()
    (binn / "rebuild").write_text(
        "#!/bin/sh\n"
        f"# flake=path:{prepared.flake}#desktop\n"
        'root="${EMERGENCY_FLAKE:-/home/rich/source/recovery-fleet}"\n'
        'exec sudo nixos-rebuild switch --flake "path:$root#rescue"\n')
    (binn / "rebuild").chmod(0o755)
    monkeypatch.setattr(qb, "rebuild_wrapper", REAL_REBUILD_WRAPPER)
    monkeypatch.setenv("PATH", f"{binn}{os.pathsep}{os.environ['PATH']}")
    cmd, why, _typed = qb.switch_command(prepared, {})
    assert cmd[:2] == ["sudo", "nixos-rebuild"], \
        "a comment is not a statement about what the wrapper does"
    assert "names no flake directory" in why
    assert qb.wrapper_targets(binn / "rebuild")[0] == set(), \
        "with the comment counted, this would have been {the right directory} and trusted"


def test_a_malformed_declared_rebuild_command_is_a_sentence_not_a_traceback(prepared,
                                                                           monkeypatch):
    monkeypatch.setattr(qb, "rebuild_wrapper", REAL_REBUILD_WRAPPER)
    monkeypatch.setenv(qb.REBUILD_KEY, 'rebuild "switch')
    cmd, how, _typed = qb.switch_command(prepared, {})
    assert cmd[:2] == ["sudo", "nixos-rebuild"]
    assert "will not parse" in how


def test_head_is_recorded_from_before_the_archive_not_after_the_build(consumer_repo,
                                                                     monkeypatch):
    """An hour-long build is long enough for somebody to commit in the consumer. Recording
    HEAD afterwards recorded whatever it had BECOME, so `--apply`'s own "the consumer has
    committed since" guard waved through a commit that was never archived and never built."""
    before = _git(consumer_repo, "rev-parse", "HEAD")
    real_nix = _fake_nix("b" * 40)

    def commits_mid_build(*args, **kw):
        if args and args[0] == "build":
            (consumer_repo / "NEW").write_text("landed while we were compiling\n")
            _commit(consumer_repo, "a commit that arrived during the build")
        return real_nix(*args, **kw)

    monkeypatch.setattr(qb, "have_nix", lambda: True)
    monkeypatch.setattr(qb, "nix", commits_mid_build)
    proposal, _log, why = qb.prepare(
        qb.Consumer(consumer_repo, "quarterback", "quarterback", "a" * 40, "--flake"),
        "desktop", str(BIN / "qb-bump"), qb.Drift("fail", "behind"))
    assert proposal is None, "what was built is not what a switch would build now"
    assert "while this was building" in why
    assert _git(consumer_repo, "rev-parse", "HEAD") != before


def test_the_lock_is_installed_atomically(prepared, consumer_repo, monkeypatch):
    """`write_text` truncates before it writes, so a failure between the two leaves the
    consumer with an empty `flake.lock` and nothing saying it was this that did it."""
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    renamed: list = []
    real_replace = qb.os.replace
    monkeypatch.setattr(qb.os, "replace",
                        lambda a, b: (renamed.append((str(a), str(b))), real_replace(a, b))[1])
    monkeypatch.setattr(qb.os, "execvp", lambda file, argv: None)
    assert qb.apply(prepared, dry_run=False) == 0
    assert renamed and renamed[0][1] == str(consumer_repo / "flake.lock")
    assert (consumer_repo / "flake.lock").read_text() == _lock("b" * 40)
    assert not list(consumer_repo.glob(".flake.lock.qb-bump.*")), "no scratch left behind"


def test_a_pull_that_moved_the_consumer_re_reads_that_tree_and_does_not_rescan(
        consumer_repo, quarterback_repo, tmp_path, monkeypatch):
    """Rerunning discovery after the pull can land on a DIFFERENT flake — the pulled one
    stops pinning this repo, a sibling starts — and then the reason printed describes one
    tree while the bump was prepared in another."""
    monkeypatch.setenv("QUARTERBACK_CONSUMER_ROOTS", str(consumer_repo.parent))
    monkeypatch.setattr(qb, "have_nix", lambda: True)
    monkeypatch.setattr(qb, "nix", _fake_nix("b" * 40))
    monkeypatch.setattr(qb, "harness_drift", lambda *a: (qb.Drift("fail", "behind"), ""))
    monkeypatch.setattr(qb, "resolve_attr", lambda *a: ("desktop", ""))

    scans: list = []
    real_resolve = qb.resolve_consumer
    monkeypatch.setattr(qb, "resolve_consumer",
                        lambda *a: (scans.append(1), real_resolve(*a))[1])
    monkeypatch.setattr(qb, "fast_forward", lambda where, **kw: (
        qb.Pulled(path=str(where), upstream="origin/main", before="1" * 40, after="2" * 40)
        if Path(where) == consumer_repo else qb.Pulled(path=str(where))))

    assert qb.main(["--repo", str(quarterback_repo), "--no-announce"]) == 2
    assert len(scans) == 1, "discovery runs once; the re-read is of the path it found"
    assert qb.load().flake == str(consumer_repo)


# --------------------------------------------------------------------------- #
# it says what it is doing — a blank terminal is indistinguishable from a hang
# --------------------------------------------------------------------------- #

def test_each_slow_step_says_what_it_is_before_it_starts(consumer_repo, quarterback_repo,
                                                         monkeypatch, capsys):
    """Two fetches, a `qb-doctor`, a scan, a `nix flake update` and a whole NixOS build ran
    without printing one word until the last of them finished. Several minutes of a cursor
    and no output is indistinguishable from a hang, and the first thing anybody does with a
    hang is Ctrl-C it — which here means killing a build that was nearly done."""
    monkeypatch.setenv("QUARTERBACK_CONSUMER_ROOTS", str(consumer_repo.parent))
    monkeypatch.setattr(qb, "have_nix", lambda: True)
    monkeypatch.setattr(qb, "nix", _fake_nix("b" * 40))
    monkeypatch.setattr(qb, "harness_drift",
                        lambda *a: (qb.Drift("fail", "behind: 1 absent (qb-admit)",
                                             missing=["qb-admit"]), ""))
    monkeypatch.setattr(qb, "resolve_attr", lambda *a: ("desktop", ""))
    monkeypatch.setattr(qb, "fast_forward", lambda where, **kw: qb.Pulled(path=str(where)))
    assert qb.main(["--repo", str(quarterback_repo), "--no-announce"]) == 2
    said = capsys.readouterr().err
    for step in ("asking qb-doctor", "looking for the flake that pins",
                 "which nixosConfiguration this machine is", "updating the",
                 "building nixosConfigurations.desktop"):
        assert step in said, f"nothing said before {step!r}"


def test_the_build_is_followable_while_it_runs(consumer_repo, quarterback_repo, monkeypatch,
                                               capsys):
    """The forty-minute step. `run` hands both streams over when the process ends, so a
    build that compiles is forty minutes of nothing followed by everything; nix's output is
    written to the log AS IT HAPPENS, and the line above it says where."""
    monkeypatch.setenv("QUARTERBACK_CONSUMER_ROOTS", str(consumer_repo.parent))
    monkeypatch.setattr(qb, "have_nix", lambda: True)
    monkeypatch.setattr(qb, "nix", _fake_nix("b" * 40))
    monkeypatch.setattr(qb, "harness_drift", lambda *a: (qb.Drift("fail", "behind"), ""))
    monkeypatch.setattr(qb, "resolve_attr", lambda *a: ("desktop", ""))
    monkeypatch.setattr(qb, "fast_forward", lambda where, **kw: qb.Pulled(path=str(where)))
    assert qb.main(["--repo", str(quarterback_repo), "--no-announce"]) == 2
    assert f"tail -f {qb.CACHE / 'build.log'}" in capsys.readouterr().err


def test_nix_writes_the_build_log_while_it_is_still_running(tmp_path, monkeypatch):
    """A real subprocess, because the point is timing: the log has to be on disk and
    growing WHILE the command runs, or `tail -f` on it is a file that turns up once there
    is nothing left to follow.

    The `nix` here is a shell script on PATH — there is no nix inside `worktree-tests`,
    and a test that skipped there would be green about the thing it exists to check."""
    log = tmp_path / "deep" / "build.log"
    binn = tmp_path / "nixbin"
    binn.mkdir()
    (binn / "nix").write_text("#!/bin/sh\necho 'building …' >&2\ncat \"$QB_WAIT\" "
                              ">/dev/null\necho '/nix/store/out'\n")
    (binn / "nix").chmod(0o755)
    gate = tmp_path / "gate"
    monkeypatch.setenv("PATH", f"{binn}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("QB_WAIT", str(gate))

    mid: list = []
    real_popen = qb.subprocess.Popen

    def watching(argv, **kw):
        proc = real_popen(argv, **kw)
        # The child is blocked on a file that does not exist yet only in spirit; what
        # matters is that we look at the log before `communicate` reaps the process.
        for _ in range(200):
            if log.exists() and log.read_text():
                break
            time.sleep(0.01)
        mid.append(log.read_text() if log.exists() else "")
        gate.write_text("go\n")
        return proc

    monkeypatch.setattr(qb.subprocess, "Popen", watching)
    rc, out, err = qb.nix("build", log=log)
    assert rc == 0 and out == "/nix/store/out"
    assert "building" in mid[0], "the log was empty until the process ended"
    assert "building" in err, "and it is still readable afterwards, for the refusal"


def test_json_is_a_document_and_never_a_document_plus_a_commentary(quarterback_repo,
                                                                   monkeypatch, capsys):
    monkeypatch.setattr(qb, "fast_forward", lambda where, **kw: qb.Pulled(path=str(where)))
    monkeypatch.setattr(qb, "harness_drift", lambda *a: (qb.Drift("ok", "same"), ""))
    assert qb.main(["--repo", str(quarterback_repo), "--json", "--no-announce"]) == 0
    got = capsys.readouterr()
    assert json.loads(got.out)["outcome"] == "current"
    assert got.err == "", "a caller redirecting both streams into a parser gets a surprise"


# --------------------------------------------------------------------------- #
# a successful run must not wedge the next one (#537)
# --------------------------------------------------------------------------- #

def test_the_lock_this_installed_does_not_block_the_next_run(prepared, consumer_repo,
                                                             monkeypatch, capsys):
    """`--apply` writes the prepared lock and leaves it MODIFIED on purpose — committing it
    belongs to whoever owns that repository. Which meant a successful run created the exact
    state that refused the next one, and since #533 made `--apply` re-prepare every time,
    that fired on the second command rather than in some corner."""
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(qb.os, "execvp", lambda file, argv: None)
    assert qb.apply(prepared, dry_run=False) == 0
    assert _git(consumer_repo, "status", "--porcelain").split() == ["M", "flake.lock"], \
        "the premise: a successful apply leaves the lock uncommitted"
    assert qb.lock_is_committed(consumer_repo) == "", "and the next run is not wedged by it"
    assert "is the one this installed" in capsys.readouterr().err


def test_a_lock_somebody_else_touched_still_refuses(prepared, consumer_repo, monkeypatch):
    """The refusal's reasoning is untouched: an uncommitted lock is normally a nixpkgs bump
    somebody was part-way through, and preparing against HEAD would discard it."""
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(qb.os, "execvp", lambda file, argv: None)
    assert qb.apply(prepared, dry_run=False) == 0
    (consumer_repo / "flake.lock").write_text(_lock("f" * 40))  # edited since
    why = qb.lock_is_committed(consumer_repo)
    assert "did not write" in why


def test_an_uncommitted_lock_with_no_proposal_behind_it_still_refuses(consumer_repo):
    """A cleared cache, another machine, a lock that predates any run here. None of those
    are this tool's handwriting and none of them may be prepared over."""
    (consumer_repo / "flake.lock").write_text(_lock("f" * 40))
    assert qb.load() is None, "the premise: nothing in the cache to compare against"
    assert "did not write" in qb.lock_is_committed(consumer_repo)


def test_a_proposal_for_a_different_consumer_is_not_this_ones_handwriting(prepared,
                                                                         consumer_repo,
                                                                         tmp_path,
                                                                         monkeypatch):
    """The cached proposal names the flake it was prepared for. A second consumer whose
    lock happens to be dirty is not covered by it."""
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(qb.os, "execvp", lambda file, argv: None)
    assert qb.apply(prepared, dry_run=False) == 0
    other = tmp_path / "other-fleet"
    other.mkdir()
    _git(other, "init", "-q", "-b", "main")
    (other / "flake.lock").write_text(_lock("a" * 40))
    _commit(other)
    (other / "flake.lock").write_text((consumer_repo / "flake.lock").read_text())
    assert "did not write" in qb.lock_is_committed(other)


def test_apply_twice_in_a_row_is_the_ordinary_thing_it_looks_like(consumer_repo,
                                                                  quarterback_repo,
                                                                  monkeypatch):
    """End to end, because the wedge was only visible across two invocations: the second
    `--apply` has to prepare and switch, not refuse on the file the first one wrote."""
    _whole_job(monkeypatch, consumer_repo)
    execd: list = []
    monkeypatch.setattr(qb.os, "execvp", lambda file, argv: execd.append(argv))
    assert qb.main(["--repo", str(quarterback_repo), "--apply", "--no-announce"]) == 0
    assert _git(consumer_repo, "status", "--porcelain").split() == ["M", "flake.lock"]
    assert qb.main(["--repo", str(quarterback_repo), "--apply", "--no-announce"]) == 0
    assert len(execd) == 2, "the second run switched too, rather than refusing"
