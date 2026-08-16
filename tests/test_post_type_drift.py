"""One list of post types, copied into five places, checked in none of them.

`POST_TYPES` in app/schemas.py is the only copy the server enforces. The MCP server
keeps its own list (it validates before the round trip), spells the types out again in
its instructions block, again in `board_post`'s argument docs, and the README prints
them for a human. Adding a type means editing all five, and the first four drift
silently: the MCP copy was already missing `published`, so the tool refused to post a
type the board had accepted since v2.8.

The MCP package is not importable from this suite — `mcp/` is a separate distribution
with its own pyproject, and pytest runs against the app — so the copies are read out of
the source with `ast` rather than by import. That is deliberate: skipping the test when
the import fails would mean it never runs anywhere, which is how the drift got in.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from app.schemas import MUTED_TYPES, POST_TYPES, SESSION_MUTED_TYPES

REPO_ROOT = Path(__file__).resolve().parent.parent
MCP_SERVER = REPO_ROOT / "mcp" / "mcp_server" / "server.py"
README = REPO_ROOT / "README.md"


@pytest.fixture(scope="module")
def mcp_source() -> ast.Module:
    return ast.parse(MCP_SERVER.read_text(encoding="utf-8"))


def _assigned(module: ast.Module, name: str):
    for node in module.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == name for t in node.targets
        ):
            return ast.literal_eval(node.value)
    raise AssertionError(f"no module-level {name} in {MCP_SERVER}")


def _keyword(module: ast.Module, call_target: str, keyword: str) -> str:
    for node in module.body:
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Call):
            continue
        if not any(isinstance(t, ast.Name) and t.id == call_target for t in node.targets):
            continue
        for kw in node.value.keywords:
            if kw.arg == keyword:
                return ast.literal_eval(kw.value)
    raise AssertionError(f"no {call_target}(..., {keyword}=...) in {MCP_SERVER}")


def _docstring(module: ast.Module, function: str) -> str:
    for node in module.body:
        if isinstance(node, ast.FunctionDef) and node.name == function:
            return ast.get_docstring(node) or ""
    raise AssertionError(f"no def {function} in {MCP_SERVER}")


def _named(text: str) -> set[str]:
    """The post-type-shaped words in a chunk of prose."""
    return set(re.findall(r"[a-z]+", text))


def test_the_mcp_type_list_matches_the_app(mcp_source):
    """The MCP server validates locally, so a stale list rejects a type the board takes."""
    mcp_types = _assigned(mcp_source, "POST_TYPES")
    assert set(mcp_types) == POST_TYPES
    assert len(mcp_types) == len(POST_TYPES), f"duplicate entries in {mcp_types}"


def test_the_mcp_instructions_enumerate_every_type(mcp_source):
    """The instructions block is what an agent reads before it picks a type."""
    instructions = _keyword(mcp_source, "mcp", "instructions")
    section = re.search(r"## Post types\n(.+)", instructions)
    assert section, "no '## Post types' line in the MCP instructions"
    assert _named(section.group(1)) == POST_TYPES


def test_the_board_post_docstring_enumerates_every_type(mcp_source):
    """The tool schema an agent is shown at call time — the copy it acts on."""
    enumeration = re.search(r"type: One of (.+?)\.", _docstring(mcp_source, "board_post"), re.S)
    assert enumeration, "board_post's `type:` arg no longer enumerates the types"
    assert _named(enumeration.group(1)) == POST_TYPES


def test_the_readme_lists_every_type():
    """The copy a human reads, and the only one no code path would ever catch."""
    listed = re.search(r"Post types: `([^`]+)`", README.read_text(encoding="utf-8"), re.S)
    assert listed, "the README no longer lists the post types"
    assert _named(listed.group(1)) == POST_TYPES


def test_the_muted_lists_are_real_types_and_stably_ordered():
    """Muting names types by string; a typo would mute nothing and say nothing."""
    assert set(MUTED_TYPES) <= POST_TYPES
    assert set(SESSION_MUTED_TYPES) <= set(MUTED_TYPES)
    # Sorted tuples, not sets: they are rendered into a SQL IN list, and a frozenset
    # would order it differently in every process for no reason (see app/schemas.py).
    for muted in (MUTED_TYPES, SESSION_MUTED_TYPES):
        assert isinstance(muted, tuple)
        assert list(muted) == sorted(muted)
