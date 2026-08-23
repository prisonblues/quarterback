"""Tests for `scripts/readme_releases.py`.

Nothing here reads this checkout's own README or CHANGELOG, for the reason
`tests/test_release.py` gives about the release tool: a suite asserting about the real files
goes red on the day somebody lands a release, which is the day it is needed. The real files
ARE asserted, once, in
`harness/tests/test_release_numbers.py::test_the_readme_release_list_is_in_changelog_order` —
that is the drift check. This file is about whether the renderer under it works.

The tests that matter are the ones where it could be wrong and silently so:

  * `test_the_prose_of_every_bullet_survives_a_reorder` — the whole design rests on bullets
    being MOVED rather than rendered, since a bullet is a summary somebody wrote and not a
    copy of the CHANGELOG heading. A renderer that regenerated the text would look right on
    the day it landed and have quietly deleted fifty summaries.
  * `test_a_release_with_no_bullet_is_refused_not_invented` — the one thing this tool must
    never do is write prose.
  * `test_a_fenced_example_is_not_part_of_the_list` — the README teaches this convention by
    showing it, and a renderer that read its own documentation as data would reorder the
    lesson into the list.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

# `scripts/` is a directory of standalone tools, not an importable package, so the module is
# loaded by path — and registered in sys.modules before it executes, because @dataclass
# resolves annotations through sys.modules[cls.__module__].
_SPEC = importlib.util.spec_from_file_location(
    "readme_releases",
    Path(__file__).resolve().parent.parent / "scripts" / "readme_releases.py",
)
rr = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = rr
_SPEC.loader.exec_module(rr)


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

def changelog(*names: str) -> str:
    """A CHANGELOG carrying `names` as entries, in the order given (newest first)."""
    entries = "".join(f"## {n} — what {n} did\n\nThe body of {n}.\n\n" for n in names)
    return "# Version history\n\nEntries are newest first.\n\n" + entries.rstrip("\n") + "\n"


def readme(bullets: str) -> str:
    """A README whose release list is exactly `bullets`, with prose either side of it."""
    return (
        "# Board\n\n"
        "Some prose, and a bullet list that is NOT the release list:\n\n"
        "- **Datastore: Postgres** — the one the board runs on.\n\n"
        f"{rr.LIST_HEADING}\n\n"
        "Ending with what is next:\n\n"
        f"{bullets}\n"
        "**[CHANGELOG.md](CHANGELOG.md)** has each release in full.\n"
    )


#: Three releases, listed oldest first, the way a correct README lists them.
IN_ORDER = (
    "- **v1** — the board.\n"
    "- **v2** — presence.\n"
    "- **v2.1** — dev context, which wraps\n"
    "  onto a second line the way this list does.\n"
)

#: The same three with the last two transposed — the drift #296 was opened about.
DRIFTED = (
    "- **v1** — the board.\n"
    "- **v2.1** — dev context, which wraps\n"
    "  onto a second line the way this list does.\n"
    "- **v2** — presence.\n"
)

THREE = changelog("v2.1", "v2", "v1")

#: The en dash joining a range's endpoints, as an escape. The separator between a bullet's
#: LABEL and its prose is an EM dash, they sit one line apart in every bullet, and a test
#: written with the wrong one would assert about a bullet the renderer does not see as a
#: range while looking, in the diff, exactly like the right one.
EN = "\N{EN DASH}"


# ---------------------------------------------------------------------------
# the reorder
# ---------------------------------------------------------------------------

def test_a_list_already_in_changelog_order_is_left_byte_for_byte():
    """The renderer has to be a noop on a correct file, or the check can never be green."""
    text = readme(IN_ORDER)
    assert rr.render(text, THREE) == text


def test_a_transposed_pair_is_put_back_in_changelog_order():
    assert rr.render(readme(DRIFTED), THREE) == readme(IN_ORDER)


def test_the_prose_of_every_bullet_survives_a_reorder():
    """Bullets are moved, never rewritten. Asserted as a multiset of lines rather than by
    eyeballing the result: a renderer that regenerated each bullet from its CHANGELOG heading
    would produce a plausible-looking list with fifty hand-written summaries deleted."""
    before, after = readme(DRIFTED), rr.render(readme(DRIFTED), THREE)
    assert sorted(before.splitlines()) == sorted(after.splitlines())


def test_rendering_twice_changes_nothing_the_second_time():
    once = rr.render(readme(DRIFTED), THREE)
    assert rr.render(once, THREE) == once


def test_the_order_comes_from_the_changelog_and_not_from_the_numbers():
    """A CHANGELOG that is not sorted renders a README that is not sorted either.

    Deliberate. `test_the_changelog_is_newest_first` is what asserts the CHANGELOG is in
    order; sorting numerically here as well would render a tidy README over a broken
    CHANGELOG and hide the failure that other test exists to report."""
    scrambled = changelog("v2", "v2.1", "v1")  # v2 above v2.1: wrong, and not this tool's job
    listed = (
        "- **v1** — the board.\n"
        "- **v2.1** — dev context.\n"
        "- **v2** — presence.\n"
    )
    assert rr.render(readme(listed), scrambled) == readme(listed)


# ---------------------------------------------------------------------------
# what is not a plain release bullet
# ---------------------------------------------------------------------------

def test_a_range_bullet_sorts_by_the_oldest_release_it_covers():
    """A bullet covering v1 to v2 belongs where v1 belongs, not where v2 does."""
    listed = f"- **v2.1** — dev context.\n- **v1{EN}v2** — the board, then presence.\n"
    expected = f"- **v1{EN}v2** — the board, then presence.\n- **v2.1** — dev context.\n"
    assert rr.render(readme(listed), THREE) == readme(expected)


def test_a_range_covers_the_entries_the_changelog_has_between_its_endpoints():
    """Resolved against the file, not by arithmetic: v2 is inside a v1-to-v2.1 range because
    the CHANGELOG puts it there, and a release that does not exist could not be."""
    listed = f"- **v1{EN}v2.1** — the first three.\n"
    assert rr.render(readme(listed), THREE) == readme(listed)


def test_a_bullet_naming_no_release_is_rendered_last():
    """`- **Not yet numbered** — …` has no position in the CHANGELOG, so it gets the one
    position that cannot go stale."""
    listed = "- **Not yet numbered** — a roadmap item.\n" + IN_ORDER
    assert rr.render(readme(listed), THREE) == readme(
        IN_ORDER + "- **Not yet numbered** — a roadmap item.\n")


def test_the_newest_release_is_rendered_last_among_the_releases():
    """The CHANGELOG is newest first, so oldest-first puts the release just cut at the END of
    the README list. Nothing special-cases it: it is an entry name like any other."""
    listed = IN_ORDER + "- **v2.2** — the one just cut.\n- **Not yet numbered** — later.\n"
    scrambled = ("- **v2.2** — the one just cut.\n" + IN_ORDER
                 + "- **Not yet numbered** — later.\n")
    assert rr.render(readme(scrambled), changelog("v2.2", "v2.1", "v2", "v1")) == readme(listed)


def test_bold_bullets_outside_the_release_list_are_not_touched():
    """The README has other `- **…** — ` bullets. A global scan would sweep them into the
    release list and then reorder the list around them."""
    out = rr.render(readme(DRIFTED), THREE)
    assert "- **Datastore: Postgres** — the one the board runs on." in out


def test_a_fenced_example_is_not_part_of_the_list():
    """The README documents this convention by showing it, and the example sits above the
    list. Read as data, the fenced `- **v9.9** — …` would be a bullet for a release the
    CHANGELOG does not have, and the render would refuse a correct file."""
    text = readme(IN_ORDER).replace(
        f"{rr.LIST_HEADING}\n",
        "```md\n- **v9.9** — <title>\n```\n\n" + rr.LIST_HEADING + "\n")
    assert rr.render(text, THREE) == text


# ---------------------------------------------------------------------------
# refusals
# ---------------------------------------------------------------------------

def test_a_release_with_no_bullet_is_refused_not_invented():
    """The one thing this tool must never do. There is no sentence to write: a bullet is a
    summary somebody chose, and deriving one from the heading would put text in the README
    that no author wrote."""
    with pytest.raises(rr.ListError) as e:
        rr.render(readme(IN_ORDER), changelog("v2.2", "v2.1", "v2", "v1"))
    assert "v2.2" in str(e.value) and "never invents prose" in str(e.value)


def test_a_bullet_for_a_release_the_changelog_does_not_have_is_refused():
    with pytest.raises(rr.ListError) as e:
        rr.render(readme(IN_ORDER + "- **v9.9** — never shipped.\n"), THREE)
    assert "v9.9" in str(e.value)


def test_a_release_covered_twice_is_refused_rather_than_reordered():
    """What a merge keeping both sides of this list leaves behind. Reordering it would put
    the two copies next to each other and look like a successful render."""
    with pytest.raises(rr.ListError) as e:
        rr.render(readme(IN_ORDER + "- **v2** — presence, again.\n"), THREE)
    assert "covers v2 twice" in str(e.value)


def test_a_range_written_backwards_is_refused():
    with pytest.raises(rr.ListError) as e:
        rr.render(readme(f"- **v2.1{EN}v1** — backwards.\n"), THREE)
    assert "runs backwards" in str(e.value)


def test_a_blank_line_splitting_the_list_is_refused_by_name():
    """The list ends at the first line that is not a bullet, so a gap hides everything below
    it — and what came back before this refusal existed was "these releases have no bullet",
    naming bullets that were sitting in the file six lines further down."""
    listed = "- **v1** — the board.\n\n- **v2** — presence.\n- **v2.1** — dev context.\n"
    with pytest.raises(rr.ListError) as e:
        rr.render(readme(listed), THREE)
    assert "blank line" in str(e.value) and "v2" in str(e.value)


def test_a_missing_list_heading_is_refused_rather_than_guessed():
    text = readme(IN_ORDER).replace(rr.LIST_HEADING, "### Releases")
    with pytest.raises(rr.ListError) as e:
        rr.render(text, THREE)
    assert rr.LIST_HEADING in str(e.value)


def test_an_unclosed_fence_is_the_stampers_refusal_and_not_a_silent_empty_list():
    """`mask_code` is shared with the stamper, and it refuses an unterminated fence rather
    than blanking the rest of the file — which here would blank the whole release list."""
    text = readme(IN_ORDER).replace(rr.LIST_HEADING, "```\n" + rr.LIST_HEADING)
    with pytest.raises(rr.rs.ReleaseError):
        rr.render(text, THREE)


# ---------------------------------------------------------------------------
# the command line
# ---------------------------------------------------------------------------

def _repo(tmp_path: Path, bullets: str, entries: str) -> Path:
    (tmp_path / "README.md").write_text(readme(bullets), encoding="utf-8")
    (tmp_path / "CHANGELOG.md").write_text(entries, encoding="utf-8")
    return tmp_path


def test_check_exits_one_on_drift_and_zero_on_a_rendered_list(tmp_path):
    repo = _repo(tmp_path, DRIFTED, THREE)
    assert rr.main(["check", "--repo", str(repo)]) == 1
    assert rr.main(["write", "--repo", str(repo)]) == 0
    assert rr.main(["check", "--repo", str(repo)]) == 0
    assert (repo / "README.md").read_text(encoding="utf-8") == readme(IN_ORDER)


def test_a_refusal_exits_two_rather_than_one(tmp_path):
    """Exit 1 is "the list has drifted, run write"; exit 2 is "this needs a human". A tool
    that answered 1 to both would have `write` advertised as the fix for a missing bullet,
    which `write` cannot do."""
    repo = _repo(tmp_path, IN_ORDER, changelog("v2.2", "v2.1", "v2", "v1"))
    assert rr.main(["check", "--repo", str(repo)]) == 2
