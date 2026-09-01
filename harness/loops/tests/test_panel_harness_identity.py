"""Which harness produced this round, as the harness can actually answer it (#112).

The board half of this is `tests/test_review_harness_identity.py`; this is the
half that has to be TRUE. A payload key that says `harness_rev` and carries the
HEAD of whatever repository the panel happened to be sitting in is worse than no
key at all: it is a plausible forty hex digits, in the right column, belonging to
the wrong repository, and nothing downstream can tell.

So every test here is about a case where the honest answer is null, or about a
case where two answers must differ. Nothing asserts that a value merely exists —
a test that `digest` is a string passes against a digest of the wrong directory,
against one that ignores the shebang rewrite the packaging performs, and against
one computed over the test suite instead of the modules that run.

The three cases the fields exist to keep apart:

* a CHECKOUT — a rev, a cleanliness, a digest and a path;
* an INSTALLED harness in the nix store — no rev, no cleanliness, and a digest
  that is the round's only identity. The common case, not the exotic one;
* a SCRATCHPAD copy, which `panel-review-pr.md` explicitly tells you to make —
  no rev even when the copy is sitting inside somebody else's checkout.
"""

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import panel  # noqa: E402
import panel_core  # noqa: E402

LOOPS = Path(panel_core.__file__).resolve().parent


def git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(cwd), *args], check=True,
                   capture_output=True, text=True)


def a_repo(tmp_path: Path) -> Path:
    """A real git repository with a harness-shaped `loops/` directory in it.

    Real git rather than a stub, for `_git`'s reason one module over: every
    interesting answer here is a git REFUSAL — "not a checkout", "not tracked" —
    and a double that returns what the test told it to would be asserting the
    test's own beliefs about git's behaviour rather than git's.
    """
    repo = tmp_path / "repo"
    loops = repo / "harness" / "loops"
    loops.mkdir(parents=True)
    # An absolute interpreter and not `/usr/bin/env`, which #177's guard
    # (`harness/tests/test_runtime_stub_shebangs.py`) refuses in either test tree
    # and whose ALLOWED list is empty on purpose. Nothing here is executed and the
    # spelling does not matter: what is under test is that a first line beginning
    # `#!` is dropped from the digest, whichever interpreter it names.
    (loops / "panel_core.py").write_text("#!/usr/bin/python3\nX = 1\n")
    (loops / "panel.py").write_text("#!/usr/bin/python3\nY = 2\n")
    git(repo.parent, "init", "-q", "repo")
    git(repo, "config", "user.email", "t@example.com")
    git(repo, "config", "user.name", "t")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "harness")
    return repo


def test_a_checkout_reports_the_commit_it_is_on(tmp_path):
    """The AUTHORITATIVE field, when it is available at all.

    Asserted against `git rev-parse` rather than against a literal, because the
    claim is "this is the commit a reader can go and `git show`", and a hardcoded
    sha would pass against a function that returned any forty characters.
    """
    repo = a_repo(tmp_path)
    head = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                          capture_output=True, text=True, check=True).stdout.strip()
    ident = panel_core.harness_identity(repo / "harness" / "loops")
    assert ident["rev"] == head
    assert ident["dirty"] is False
    assert ident["path"] == str(repo / "harness" / "loops")


def test_an_edited_checkout_says_so_rather_than_reporting_a_clean_rev(tmp_path):
    """`dirty` is what makes the rev honest rather than merely present.

    `panel-review-pr.md` tells you to run the panel from a copy, and a developing
    panel is edited in place — this issue was found from exactly such a tree. A
    rev recorded without this flag would say a round ran code that it did not.
    """
    repo = a_repo(tmp_path)
    loops = repo / "harness" / "loops"
    clean = panel_core.harness_identity(loops)
    (loops / "panel.py").write_text("#!/usr/bin/python3\nY = 3\n")
    edited = panel_core.harness_identity(loops)
    assert clean["dirty"] is False and edited["dirty"] is True
    assert edited["rev"] == clean["rev"], "the rev did not move; only the tree did"
    assert edited["digest"] != clean["digest"], (
        "an edited module has to change the digest, or `dirty` is the only "
        "evidence and a consumer grouping on the digest merges two harnesses")


def test_an_untracked_module_counts_as_dirty(tmp_path):
    """...and untracked files count, because they are in the digest and they run.

    The scope of `dirty` and the scope of the digest are the same directory on
    purpose: `dirty` is exactly the statement "the digest above is not what that
    rev would produce".
    """
    repo = a_repo(tmp_path)
    loops = repo / "harness" / "loops"
    (loops / "panel_extra.py").write_text("Z = 1\n")
    assert panel_core.harness_identity(loops)["dirty"] is True


def test_an_edited_test_file_does_not_make_the_harness_dirty(tmp_path):
    """`dirty` and the digest read the same files, or `dirty` means something else.

    The first cut asked `git status` about the whole directory, so editing
    `loops/tests/` — which the digest deliberately ignores, because a test does not
    run a round — reported a dirty harness whose digest had not moved (found by
    Codex). Two fields describing different scopes under one heading is how a
    reader ends up believing the wrong one.
    """
    repo = a_repo(tmp_path)
    loops = repo / "harness" / "loops"
    (loops / "tests").mkdir()
    (loops / "tests" / "test_thing.py").write_text("def test_x(): pass\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "tests")
    ident = panel_core.harness_identity(loops)
    assert ident["dirty"] is False
    (loops / "tests" / "test_thing.py").write_text("def test_x(): assert True\n")
    after = panel_core.harness_identity(loops)
    assert after["dirty"] is False, "a test edit is not a change to what runs"
    assert after["digest"] == ident["digest"]


def test_an_installed_harness_has_no_rev_and_still_has_an_identity(tmp_path):
    """The COMMON case: the nix store is not a checkout.

    Two nulls and a digest, rather than four nulls. This is the case a
    version-string field would have had nothing to say about at all, and it is
    most rounds.
    """
    store = tmp_path / "nix" / "store" / "abc-quarterback-harness" / "loops"
    store.mkdir(parents=True)
    (store / "panel_core.py").write_text("X = 1\n")
    ident = panel_core.harness_identity(store)
    assert ident["rev"] is None and ident["dirty"] is None
    assert ident["digest"] and ident["path"] == str(store)


def test_a_copy_inside_someone_elses_checkout_reports_no_rev(tmp_path):
    """The field that would otherwise be a LIE, and the reason for the tracked test.

    `panel-review-pr.md` tells you to run the panel from a scratchpad copy. Drop
    one inside any other checkout — the repository under review, say — and a naive
    `git rev-parse HEAD` answers with that repository's HEAD: a plausible commit
    id, in the harness's column, naming a commit in the wrong repository. There is
    no downstream check that could catch it, which is why the panel asks whether
    the containing repository actually TRACKS this file before believing it.
    """
    other = a_repo(tmp_path)
    copy = other / "scratch" / "loops"
    copy.mkdir(parents=True)
    (copy / "panel_core.py").write_text("X = 1\n")
    ident = panel_core.harness_identity(copy)
    assert ident["rev"] is None, "an untracked copy must not borrow a repo's HEAD"
    assert ident["dirty"] is None
    assert ident["digest"], "the digest is what this case still has to offer"


def test_the_digest_ignores_the_shebang_the_packaging_rewrites(tmp_path):
    """`package.nix`'s `postFixup` runs `patchShebangs` on every installed module.

    Counting the first line would give every DEPLOYED harness a digest matching no
    checkout anywhere — the field would still be internally consistent and would
    have lost the one comparison it exists to support. `qb-doctor`'s
    `_same_but_for_shebang` learnt this the expensive way: it reported 24 files as
    drift on a host that had just rebuilt from the checkout in front of it.
    """
    src = tmp_path / "src"
    installed = tmp_path / "installed"
    for d, shebang in ((src, "#!/usr/bin/python3"),
                       (installed, "#!/nix/store/xxxx-python3-3.13/bin/python3")):
        d.mkdir()
        (d / "panel_core.py").write_text(f"{shebang}\nX = 1\n")
    assert (panel_core.harness_identity(src)["digest"]
            == panel_core.harness_identity(installed)["digest"])


def test_the_digest_ignores_the_test_suite_beside_the_modules(tmp_path):
    """`loops/tests/` ships with the package and does not run a round.

    A release that only changed tests must not read as different machinery: that
    is a false "these two rounds were read by different code", which costs a
    consumer a group it should not have had.
    """
    loops = tmp_path / "loops"
    (loops / "tests").mkdir(parents=True)
    (loops / "panel_core.py").write_text("X = 1\n")
    before = panel_core.harness_identity(loops)["digest"]
    (loops / "tests" / "test_thing.py").write_text("def test_x(): pass\n")
    assert panel_core.harness_identity(loops)["digest"] == before


def test_a_renamed_module_changes_the_digest(tmp_path):
    """Names are hashed beside the bodies, which is not decoration.

    Without the name and the length in the stream, two modules can trade contents
    — or a boundary can shift between them — with the concatenation unchanged, and
    the digest would call two different harnesses one.
    """
    loops = tmp_path / "loops"
    loops.mkdir()
    (loops / "panel_core.py").write_text("X = 1\n")
    (loops / "panel_a.py").write_text("A = 1\n")
    before = panel_core.harness_identity(loops)["digest"]
    (loops / "panel_a.py").rename(loops / "panel_b.py")
    assert panel_core.harness_identity(loops)["digest"] != before


def test_a_directory_that_is_not_a_loops_directory_has_no_digest(tmp_path):
    """The guard against digesting the wrong directory entirely.

    home-manager links some harness files in individually, and a `__file__`
    resolved through a flat symlink would leave `parent` at `/nix/store` — where
    this would otherwise cheerfully hash a few thousand unrelated packages and
    call it a harness. A digest of the wrong directory is worse than none, because
    nothing downstream can tell it is wrong.
    """
    stray = tmp_path / "store"
    stray.mkdir()
    (stray / "something.py").write_text("X = 1\n")
    ident = panel_core.harness_identity(stray)
    assert ident["digest"] is None
    assert ident["path"] == str(stray), "the locator is still true"


def test_the_scheme_is_part_of_the_value(tmp_path):
    """A bare hex digest would go on comparing equal to itself after somebody
    changed what goes INTO it, silently splitting or merging harness versions —
    this issue's own bug one layer down. The tag rides on the value so a consumer
    that groups on the whole string cannot compare two schemes by accident."""
    loops = tmp_path / "loops"
    loops.mkdir()
    (loops / "panel_core.py").write_text("X = 1\n")
    digest = panel_core.harness_identity(loops)["digest"]
    assert digest.startswith(panel_core.HARNESS_DIGEST_SCHEME + ":")
    assert len(digest.split(":", 1)[1]) == 64


def test_a_git_that_will_not_answer_leaves_the_rev_null(tmp_path, monkeypatch):
    """No git on PATH, a hung call, a repository this process cannot read — one
    null for all of them, on `panel_scope._git`'s contract.

    Never a raise. This is bookkeeping on a payload nothing gates on, and a round
    that died because `git` was missing would be a strictly worse outcome than a
    round recorded without a rev.
    """
    def no_git(*a, **kw):
        raise OSError("git: not found")

    monkeypatch.setattr(subprocess, "run", no_git)
    ident = panel_core.harness_identity(LOOPS)
    assert ident["rev"] is None and ident["dirty"] is None
    assert ident["digest"], "the digest does not depend on git and must survive"


def test_the_real_harness_answers_for_itself():
    """The default call — this checkout's own loops directory, through `__file__`.

    Pinned because every test above hands the function a path, and the argument is
    for the tests: the production call takes none, and a `__file__` that resolved
    somewhere unexpected would leave every one of those tests green and the panel
    recording nothing.
    """
    ident = panel_core.harness_identity()
    assert ident["path"] == str(LOOPS)
    assert ident["digest"], "a checkout of the harness must digest"


def test_the_identity_is_resolved_at_import_and_never_again():
    """One process, one answer, resolved as near to load time as a program can get.

    Not an optimisation. A harness rebuilt underneath a running panel is the event
    #112 is about, and a value resolved when the payload is WRITTEN — which is
    after the review — would record the new harness as the producer of a round the
    old one produced, hiding the event inside the field that exists to expose it.
    The rebuild lands on the `~/.claude/loops` symlink, so even resolving
    `__file__` late would follow it to the new store path.
    """
    assert panel_core.harness_identity() is panel_core.harness_identity()
    assert panel_core.harness_identity() is panel_core._HARNESS_IDENTITY
    assert panel_core._HARNESS_IDENTITY["path"] == str(LOOPS)


def test_every_payload_exit_carries_the_identity():
    """`_payload_defaults` is what makes this true on the skips and the refusals.

    A refusal was still produced by a version of this code, and #39 and #40 are
    about reviewers reporting harness defects from other repositories — where the
    harness version is least knowable and most likely to be old. `rules` is on
    every exit for the same shape of reason.
    """
    defaults = panel._payload_defaults()
    for key in ("harness_rev", "harness_dirty", "harness_digest", "harness_path"):
        assert key in defaults, key
    assert defaults["harness_path"] == str(LOOPS)


@pytest.mark.parametrize("field", ["rev", "dirty", "digest", "path"])
def test_the_identity_has_exactly_these_four_fields(field):
    """The set is closed, and the board binds it key by key.

    A fifth field added here without a column would be dropped by
    `ReviewIn`'s `extra="ignore"` — which is #93, #626, #643 and #647 — and
    `tests/test_payload_key_drift.py` is what fails when it happens. This is the
    same statement from the producer's side.
    """
    ident = panel_core.harness_identity()
    assert field in ident
    assert set(ident) == {"rev", "dirty", "digest", "path"}
