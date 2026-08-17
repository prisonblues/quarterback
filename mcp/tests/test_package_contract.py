"""What this package promises about its own dependencies, checked rather than assumed.

`flake.nix`'s `mcp-tests` check builds an environment with pytest, httpx and
textual and deliberately **without** `mcp[cli]`. That is sound only because
nothing the suite imports reaches the MCP SDK — today `mcp_server/__init__.py`
is one docstring and imports nothing at all. The day somebody adds a convenience
re-export to it, `nix flake check` goes red on merge while the GitHub job, which
installs a fuller dependency set, stays green: the divergence surfaces first for
a consumer pinning a revision, which is the worst place to find it.

So the assumption is written down as a test. The same file pins the two version
constraints the store package is not otherwise checked against — `requires-python`
and `textual>=1.0` — because `pkgs.python3` and `ps.textual` float with nixpkgs
and nothing else compares them to `pyproject.toml`.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

MCP_DIR = Path(__file__).resolve().parents[1]
PYPROJECT = MCP_DIR / "pyproject.toml"


def _requires_python_floor() -> tuple[int, int]:
    spec = tomllib.loads(PYPROJECT.read_text())["project"]["requires-python"]
    major, _, minor = spec.lstrip(">=~^ ").partition(".")
    return int(major), int(minor.split(",")[0] or 0)


def _tui_floor() -> tuple[int, int]:
    extras = tomllib.loads(PYPROJECT.read_text())["project"]["optional-dependencies"]
    spec = next(d for d in extras["tui"] if d.startswith("textual"))
    _, _, version = spec.partition(">=")
    major, _, minor = version.partition(".")
    return int(major), int(minor or 0)


def _imports_cleanly(module: str) -> subprocess.CompletedProcess:
    """Import `module` in a fresh interpreter and report whether the MCP SDK came with it.

    A subprocess because the question is about what an import *pulls in*, and by
    the time this suite is running the answer has already been decided for this
    process — pytest imported half the package during collection.
    """
    return subprocess.run(
        [
            sys.executable,
            "-c",
            f"import {module}, sys; print('mcp' in sys.modules)",
        ],
        # The parent's environment with PYTHONPATH pointed at the package under
        # test, not a bare env: under nix the interpreter finds its own
        # site-packages through variables set around it, and stripping those
        # would make httpx unimportable and the assertion below vacuous.
        env={**os.environ, "PYTHONPATH": str(MCP_DIR)},
        capture_output=True,
        text=True,
        timeout=60,
    )


@pytest.mark.parametrize("module", ["mcp_server", "mcp_server.client", "mcp_server.board"])
def test_the_board_client_path_never_reaches_the_mcp_sdk(module):
    """The `server` extra exists so a tail-only host installs no SDK; this is why it can.

    Asserted for `mcp_server` itself as well as the two modules under it, because
    importing either executes the package `__init__` first — which is precisely
    the file the nix check's omission depends on staying empty.
    """
    result = _imports_cleanly(module)
    assert result.returncode == 0, f"{module} did not import: {result.stderr}"
    assert result.stdout.strip() == "False", (
        f"importing {module} pulled in the MCP SDK. flake.nix's mcp-tests check builds "
        "its environment without it, so this makes `nix flake check` red for a consumer "
        "while CI stays green — move the import, or add mcp[cli] to that check."
    )


def test_the_mcp_server_imports_when_its_extra_is_installed():
    """The other half of the split: `server` must still be a working install.

    Moving `mcp[cli]` out of the base dependencies is only safe if something
    notices when the server stops importing, and nothing did — this package had
    no CI at all before the board client landed in it. Skipped, not failed, on a
    tail-only install, which is the whole point of the extra.
    """
    pytest.importorskip("mcp")
    result = subprocess.run(
        [sys.executable, "-c", "import mcp_server.server"],
        env={**os.environ, "PYTHONPATH": str(MCP_DIR)},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr


def test_the_interpreter_satisfies_the_declared_requires_python():
    """`pkgs.python3` floats with nixpkgs and nothing else compares it to pyproject."""
    assert sys.version_info[:2] >= _requires_python_floor()


def test_the_installed_textual_satisfies_the_tui_extras_floor():
    """Same for `ps.textual`: the store package is never resolved against `textual>=1.0`."""
    pytest.importorskip("textual")
    from importlib.metadata import version

    installed = tuple(int(part) for part in version("textual").split(".")[:2])
    assert installed >= _tui_floor()
