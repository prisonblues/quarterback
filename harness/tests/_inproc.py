"""Run a `harness/bin` CLI inside this interpreter instead of spawning one (#785).

Seventeen suites under `harness/tests` drive a Python tool by handing its path to
`subprocess.run([sys.executable, ...])`. That is the honest way to test a CLI, and
it costs a fresh interpreter, `site`, and the tool's own imports once per test —
measured at 28ms for `qb-start`, which `test_qb_start.py` pays 125 times, and 20ms
of `harness_rules` for `qb-mode`, which is most of what a `qb-mode` test does at
all.

Worth being precise about what it is and is not, because #785 attributed more to
it than it deserves. A spawn is tens of milliseconds; it is not the half-second
the slowest files were spending per test. `test_qb_next.py`, `test_qb_line.py` and
`test_qb_backfill.py` were paying `ThreadingHTTPServer.shutdown()`'s default
half-second poll interval, and `test_qb_claim.py` was calling the developer's REAL
`gh` over the network. Both are fixed where they live. This module is the third of
the three, and on its own it is the smallest of them.

`run()` here calls the tool's `main()` in the interpreter already running, with
`sys.argv`, the environment and the working directory arranged to look exactly as
they would to a process, and hands back a `subprocess.CompletedProcess` so a
caller's `got.returncode` / `got.stdout` / `got.stderr` assertions do not change.

## What it deliberately does NOT prove

**That the file runs as a script.** The shebang, the executable bit, whether the
tool resolves its imports when started by `/usr/bin/env python3` rather than by a
venv — none of that is exercised by importing the file. Losing it would be a real
regression rather than a saving, and it is exactly what `test_runtime_stub_
shebangs.py` exists to notice. So every suite that adopts this keeps at least one
test that really spawns, marked with a comment saying so, and the converted tests
are the ones whose subject is the tool's *answer* rather than its packaging.

Anything asserting on the process boundary itself — an exit on a signal, stdout
buffering, two agents racing as real processes — stays a subprocess too. Threads
are the clearest case: `run()` mutates `os.environ`, `sys.argv` and the working
directory, all of which are per-process and not per-thread, so two concurrent
in-process calls would read each other's environment.

## The isolation the process boundary was providing for free

A subprocess starts from nothing every time. An imported module does not: its
globals, its caches, its argparse parser and any `logging` it configured would
outlive the test that created them, and a suite whose tests pass only in the order
they happened to run in is worse than a slow one.

Two mechanisms, both unconditional rather than opt-in, because the failure they
prevent is silent:

* **The tool is loaded fresh for every call.** Module-level state cannot cross a
  test, because the module object does not. The bytecode is cached under
  `__pycache__` after the first compile, so this costs an exec of the module body
  and not a parse.
* **Modules living beside the tool are purged before and after.** Several suites
  run a COPY of the script next to a stub `qbdata.py` and rely on the tool's own
  `sys.path.insert(0, dirname(__file__))` to find the stub rather than the real
  module. Without the purge the first test's stub would stay in `sys.modules` and
  answer for every test after it, including tests using a different stub — the
  one hazard this whole approach introduces, and the reason the purge is keyed on
  the script's own directory rather than on a name list.

`sys.path`, `sys.argv`, `os.environ` and the working directory are all restored
whatever happens, including when `main()` raises.
"""

from __future__ import annotations

import contextlib
import importlib.machinery
import importlib.util
import inspect
import io
import os
import subprocess
import sys
import types
from pathlib import Path


def load(script: Path, name: str | None = None) -> types.ModuleType:
    """Import `script` as a module and hand it back, without running `main()`.

    The tools have no `.py` extension — they are installed as commands — so the
    extension-driven finders cannot see them and `SourceFileLoader` has to be
    named explicitly. This is the same three lines a dozen suites in this
    directory already carry inline; it is here so the reload discipline in `run()`
    has one place to live.

    The module is NOT registered in `sys.modules` under `name`. Nothing imports
    these by name, and leaving them out is what makes "fresh every call" true
    rather than aspirational.
    """
    name = name or script.name.replace("-", "_").removesuffix(".py")
    loader = importlib.machinery.SourceFileLoader(name, str(script))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def run(script: Path, args, *, env: dict | None = None, cwd: Path | str | None = None,
        entry: str = "main", fresh: tuple[str, ...] = ()) -> subprocess.CompletedProcess:
    """Call `script`'s `main()` here, and report it as a finished process.

    `env` replaces the environment for the duration of the call, exactly as it
    would for `subprocess.run(env=...)` — a suite that builds its environment from
    scratch so a developer's own `QUARTERBACK_*` cannot reach the tool keeps that
    property. `cwd` likewise: the tools ask `os.getcwd()` and run `git` in it.

    `fresh` names modules that must be re-imported for every call because they
    read the environment AT IMPORT TIME and freeze the answer in a module
    constant. `harness_rules` is the one this repo has — `REPO_ROOT` off
    `HARNESS_REPO_ROOT` and `QB_CONFIG` off `QUARTERBACK_CONFIG`, both evaluated
    while the module body runs. A subprocess recomputed those per call for free;
    left cached here, the first test in a file would decide them for every test
    after it, and a suite that redirects `XDG_CONFIG_HOME` per test would be
    asserting against the previous test's config. Naming them is cheap — the
    bytecode is already compiled, so a re-import is under a millisecond.

    The exit code is `main()`'s return, or the code carried by a `SystemExit` the
    tool raised on its way out — both are how these tools finish, and a caller
    cannot tell which was used from the outside. `SystemExit(None)` is 0 and
    `SystemExit("a message")` is 1 with the message on stderr, which is what the
    interpreter itself does with one.

    An entry point that takes an argument is handed `sys.argv[1:]`, which is what
    its own `__main__` block passes it (`check-db-isolation` is spelled that way);
    one that takes none reads `sys.argv` itself.
    """
    argv = [str(script), *(str(a) for a in args)]
    with _as_a_process(argv, env, cwd) as (out, err):
        _purge_beside(script)
        _purge_named(fresh)
        try:
            module = load(script)
            main = getattr(module, entry)
            code = main(argv[1:]) if inspect.signature(main).parameters else main()
        except SystemExit as exit_:
            code = exit_.code
            if isinstance(code, str):                 # `sys.exit("boom")` prints and exits 1
                print(code, file=sys.stderr)
                code = 1
        finally:
            _purge_beside(script)
            _purge_named(fresh)
    return subprocess.CompletedProcess(
        argv, 0 if code is None else int(code), out.getvalue(), err.getvalue())


@contextlib.contextmanager
def _as_a_process(argv: list[str], env: dict | None, cwd: Path | str | None):
    """`sys.argv`, the environment, the working directory and the streams, restored.

    Restoration is in `finally` and covers the failure paths as well: a tool that
    raises partway through must not leave the next test with its `os.chdir()` or
    with an environment holding one suite's `QUARTERBACK_TOKEN`.

    `sys.path` is saved too, because every one of these tools inserts its own
    directory at position 0 on import. Left alone that would accumulate one entry
    per call, and — worse — a stub directory from a finished test would still be
    ahead of the real `harness/bin` on the path when the next one imported.
    """
    was = (sys.argv, os.environ.copy(), os.getcwd(), list(sys.path))
    out, err = io.StringIO(), io.StringIO()
    sys.argv = list(argv)
    if env is not None:
        os.environ.clear()
        os.environ.update(env)
    if cwd is not None:
        os.chdir(cwd)
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            yield out, err
    finally:
        sys.argv, environ, back, path = was
        os.chdir(back)
        os.environ.clear()
        os.environ.update(environ)
        sys.path[:] = path


def _purge_named(names: tuple[str, ...]) -> None:
    """Drop these modules from the cache so the next import re-runs their body."""
    for name in names:
        sys.modules.pop(name, None)


def _purge_beside(script: Path) -> None:
    """Drop every cached module that `script`'s own directory could supply.

    That directory is either `harness/bin` — where the real `qbdata.py` sits — or
    a per-test stub directory holding a doubled `qbdata.py`. The tools reach it by
    `sys.path.insert(0, dirname(__file__))`, and `sys.path` is only consulted when
    `sys.modules` misses. So a stale entry under the name is not a slow path, it
    is a WRONG one, and it goes both ways: a suite that ran first leaves the real
    `qbdata` cached and the next suite's stub is never imported at all, while a
    stub left behind answers for the real module afterwards. Under a subprocess
    neither could happen, and this is the single hazard that swapping the process
    for an import introduces.

    Hence two tests rather than one. A module is dropped if it was loaded FROM
    this directory, and also if this directory holds a file that could shadow it —
    the second is the one that catches a real `qbdata` standing in front of a stub
    that has not been imported yet, where there is no loaded module to inspect.

    The directory is listed ONCE and the answers come out of a set. Asking the
    filesystem per cached module instead — `(directory / f"{name}.py").exists()` —
    is two stat calls for each of the thousand-odd modules pytest has loaded, on
    every call, and measured at more than the interpreter start this is here to
    avoid. `os.path.dirname` rather than `Path.resolve()` for the same reason: a
    module's `__file__` is already absolute, and resolving it walks the tree.
    """
    directory = str(script.resolve().parent)
    try:
        shadows = {name.removesuffix(".py") for name in os.listdir(directory)
                   if name.endswith(".py")}
    except OSError:                                    # a directory a test removed
        shadows = set()
    for name, module in list(sys.modules.items()):
        origin = getattr(module, "__file__", None)
        if name in shadows or (origin and os.path.dirname(origin) == directory):
            del sys.modules[name]
