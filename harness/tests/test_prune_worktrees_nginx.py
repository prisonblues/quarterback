"""A worktree marker is a WHOLE LINE, and matching it mid-line invents worktrees.

`create-worktree` brackets each block it writes with `# WORKTREE-START:<branch>` /
`# WORKTREE-END:<branch>`, and writes a header above them documenting that format:

    # Format: # WORKTREE-START:<branch-name> ... # WORKTREE-END:<branch-name>

The scan matched `# WORKTREE-START:` anywhere in a line, so it read that header as a
block and reported an orphan worktree literally named `<branch-name>`.

**The two halves disagreed about what a block is, and that is what made it permanent.**
The stripper (`--remove-nginx`) compares the WHOLE remainder after the tag against the
branch name; for the header that remainder is `<branch-name> ... # WORKTREE-END:...`,
which never equals `<branch-name>`. So the stripper matched nothing, removed nothing,
and reported success — and the next sweep offered the phantom again. Observed on
`prisonblues/lexray`, where it survived a sweep that really did strip three orphans
beside it. A category that edits a tracked config file is the wrong place for a finding
that can never be actioned: it trains the reader to skip the one report that mutates
their repo.

So the fix anchors both ends of the match, and these tests pin the anchoring from both
sides — a header that must NOT be read as a block, and real blocks that must still be.
`test_a_real_block_is_still_found` is the half that matters most: an anchor tightened
too far reports `none`, which is the failure that looks like success.

The block is extracted from the real script rather than copied, so a refactor that moves
or renames it fails here instead of leaving this suite green about code nobody runs.

Run: pytest harness/tests
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

# A sibling module, imported by bare name — see `_path_sandbox`'s own docstring for why a
# suite driving a stanza cannot build its PATH from the host's.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import _path_sandbox  # noqa: E402

SCRIPT = Path(__file__).resolve().parents[1] / "bin" / "prune-worktrees"

BASH = shutil.which("bash")
#: The stanza shells out to both, and `_path_sandbox` explains why a binary is the unit.
GREP = shutil.which("grep")
SED = shutil.which("sed")
SORT = shutil.which("sort")

pytestmark = pytest.mark.skipif(
    BASH is None or GREP is None or SED is None or SORT is None,
    reason="bash, grep, sed and sort must all be on PATH")


def nginx_scan_block() -> str:
    """The lines the markers bracket, out of the live script."""
    src = SCRIPT.read_text()
    start, end = "# >>> nginx-scan", "# <<< nginx-scan"
    assert start in src and end in src, (
        f"the {start} / {end} markers are gone from prune-worktrees, so this suite is "
        "asserting nothing — fix the markers rather than deleting the test")
    block = src.split(start, 1)[1].split("\n", 1)[1].split(end, 1)[0]
    assert "ORPHAN_NGINX+=" in block, "the markers no longer bracket the nginx scan"
    assert "WORKTREE-START" in block, "the markers no longer bracket the marker match"
    return block


#: What the scan is run AGAINST rather than what is under test: where the config is, and
#: which suffixes are live. `is_live_suffix` is stubbed to its ANSWER (a membership test
#: over LIVE) rather than reproduced — the "/" -> "-" normalisation it also does belongs
#: to the port/dir sweeps and has its own coverage; copying it here would assert a second
#: implementation of it instead of the marker parsing this suite is about.
PRELUDE = """
set -uo pipefail
MAIN_REPO='%(repo)s'
NGINX_CONFIG='nginx.conf'
LIVE='%(live)s'
is_live_suffix() {
    local s
    for s in $LIVE; do [[ "$s" == "$1" ]] && return 0; done
    return 1
}
ORPHAN_NGINX=()
NGINX_UNKNOWN=""
NGINX_CONF_PATH=""
"""

#: The header `create-worktree` writes above the blocks. Reproduced verbatim from a real
#: lexray nginx config, because its exact shape IS the bug: the format it documents is the
#: format the scan matches.
HEADER = """\
    # ========================================================================
    # WORKTREE BLOCKS - managed by create-worktree / remove-worktree
    # Format: # WORKTREE-START:<branch-name> ... # WORKTREE-END:<branch-name>
    # DO NOT manually edit these blocks - use those tools to manage them
    # ========================================================================
"""


def block(name: str, *, indent: str = "    ", trailing: str = "") -> str:
    return (f"{indent}# WORKTREE-START:{name}{trailing}\n"
            f"{indent}location /{name}/ {{ proxy_pass http://{name}:5005; }}\n"
            f"{indent}# WORKTREE-END:{name}{trailing}\n")


def run_scan(config_text, *, live=(), tmp_path, unreadable=False):
    """Run the real scan over an nginx config, returning the orphans it named."""
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    conf = repo / "nginx.conf"
    conf.write_text(config_text)
    if unreadable:
        # Present but unreadable is a THIRD answer, distinct from "no blocks" (#735).
        conf.chmod(0o000)

    script = _path_sandbox.sibling_dir(tmp_path) / "scan.sh"
    script.write_text(
        PRELUDE % {"repo": str(repo), "live": " ".join(live)}
        + nginx_scan_block()
        + '\nprintf "ORPHAN|%s\\n" "${ORPHAN_NGINX[@]:-}"\n'
        # `:-` so this reads the same against a version with no such variable: under
        # `set -u` a bare expansion aborts, and the red half of a red/green run would
        # be a bash error rather than the assertion that names the defect.
        + 'printf "UNKNOWN|%s\\n" "${NGINX_UNKNOWN:-}"\n')

    got = subprocess.run(
        [BASH, str(script)], capture_output=True, text=True,
        env={"PATH": _path_sandbox.sandbox_path(tmp_path, tools=("grep", "sed", "sort")),
             "HOME": str(tmp_path)})
    if unreadable:
        conf.chmod(0o644)
    assert got.returncode == 0, got.stderr
    got.orphans = [ln.split("|", 1)[1] for ln in got.stdout.splitlines()
                   if ln.startswith("ORPHAN|") and ln != "ORPHAN|"]
    got.unknown = next((ln.split("|", 1)[1] for ln in got.stdout.splitlines()
                        if ln.startswith("UNKNOWN|")), "")
    return got


# ------------------------------------------------------------- the documentation header

def test_the_format_header_is_not_a_worktree(tmp_path):
    """THE regression. The header documents the marker format; it is not a block."""
    got = run_scan(HEADER, tmp_path=tmp_path)
    assert got.orphans == [], (
        f"the documentation header was read as a worktree named {got.orphans!r} — a "
        "phantom the stripper can never remove, so it returns on every sweep")


def test_the_header_beside_real_blocks_names_only_the_real_ones(tmp_path):
    """How it actually presents: three genuine orphans and the phantom, together.

    The phantom outlived a sweep that really did strip the other three, which is what
    made it look like a stubborn block rather than a parsing bug.
    """
    got = run_scan(HEADER + block("formal-logic") + block("omnibus") + block("live-one"),
                   live=("live-one",), tmp_path=tmp_path)
    assert sorted(got.orphans) == ["formal-logic", "omnibus"]


# ----------------------------------------------------- the other half: still finding them

def test_a_real_block_is_still_found(tmp_path):
    """An anchor tightened too far reports `none`, which is the failure that looks
    like success — nothing to action, and nothing to say it went unchecked."""
    got = run_scan(HEADER + block("gone"), tmp_path=tmp_path)
    assert got.orphans == ["gone"]


def test_a_live_worktrees_block_is_not_an_orphan(tmp_path):
    got = run_scan(HEADER + block("here"), live=("here",), tmp_path=tmp_path)
    assert got.orphans == []


def test_markers_are_found_whatever_their_indent(tmp_path):
    """The anchor allows leading whitespace, so it must not depend on how much.

    Real configs indent these inside a `server { }`; a hand-edited one may not.
    """
    got = run_scan(HEADER + block("deep", indent="        ") + block("flush", indent=""),
                   tmp_path=tmp_path)
    assert sorted(got.orphans) == ["deep", "flush"]


def test_a_marker_with_trailing_whitespace_is_still_a_marker(tmp_path):
    """Anchoring to end-of-line is what excludes the header, and a stray trailing
    space is the way that anchoring could exclude a real block instead. An editor
    that strips or adds one must not change which worktrees exist."""
    got = run_scan(HEADER + block("spaced", trailing="  "), tmp_path=tmp_path)
    assert got.orphans == ["spaced"]


def test_a_name_is_not_confused_with_a_longer_one(tmp_path):
    """`issue-5` and `issue-50` are different worktrees; the stripper is careful about
    this and the scan has to agree, or it hands the stripper a name it will over-match."""
    got = run_scan(HEADER + block("issue-5") + block("issue-50"),
                   live=("issue-50",), tmp_path=tmp_path)
    assert got.orphans == ["issue-5"]


# --------------------------------------------------------------- inert and unreadable

def test_prose_mentioning_a_marker_is_not_a_block(tmp_path):
    """A comment ABOUT the markers — a note, a disabled block, a commented-out
    example — is the same class of input as the header and must stay inert."""
    got = run_scan(HEADER + "    # see # WORKTREE-START:example for the shape\n",
                   tmp_path=tmp_path)
    assert got.orphans == []


def test_a_config_with_no_blocks_is_empty_not_unknown(tmp_path):
    got = run_scan("server { listen 80; }\n", tmp_path=tmp_path)
    assert got.orphans == []
    assert not got.unknown, "a config with no blocks is a definite answer, not an unknown"


def test_a_config_that_cannot_be_read_is_unknown(tmp_path):
    """Preserved from #735: grep's exit 2 is "I could not read it", and folding that
    into "no blocks" let `--remove-nginx` rewrite a file it had just failed to read."""
    if os.geteuid() == 0:
        pytest.skip("root reads a 0000 file, so the unreadable case cannot be staged")
    got = run_scan(HEADER + block("gone"), tmp_path=tmp_path, unreadable=True)
    assert got.orphans == []
    assert got.unknown, "an unreadable config was reported as one with no orphans"
